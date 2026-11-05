"""Closing a month: lock it, keep what the close used, and book later revisions as restatements.

`period lock` freezes a month that has ended. It writes a close package, close-YYYY-MM-vN/,
with the journal, why spend moved (flux), the restatements booked into the month and a
manifest of every file's SHA-256 and a digest of the month's ledger entries. The lock keeps the
manifest's hash, so `period verify` can show a package and the ledger still agree.

After the lock nothing lands in the month. A vendor that revises a day of a locked month (connect
re-reads its cost report) is booked in the first open month after it, dated its first instant,
and listed in `restatements` with the day it belongs to. The locked journal stands; the
correction posts with the next close, which is how a controller wants it. `period reopen`, with
a reason, unlocks a month; locking it again writes the next version, whose journal number
carries the version so an import of the same month twice is refused by the books.
"""

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from .pricing import PriceBook
from .store import Ledger, month_range, now

SCHEMA = """
create table if not exists locks (
  month text not null,
  version integer not null,
  at text not null,
  by text not null,
  manifest text not null,  -- SHA-256 of manifest.json
  reopened_at text, reopened_by text, reason text,
  primary key (month, version)
);
create table if not exists restatements (
  id integer primary key,
  call_id text not null,
  spent_at text not null,  -- when the spend happened, in a locked month
  booked_at text not null,  -- where it was booked: the first open month after
  amount integer not null
);
"""


def setup(ledger: Ledger) -> None:
    ledger.db.executescript(SCHEMA)


def _month(at: str) -> str:
    return at[:7]


def is_locked(ledger: Ledger, month: str) -> bool:
    """Safe inside a write: it reads only, and a ledger that never locked has no locks table."""
    if not ledger.db.execute("select 1 from sqlite_master where name = 'locks'").fetchone():
        return False
    return ledger.db.execute(
        "select 1 from locks where month = ? and reopened_at is null", (month,)
    ).fetchone() is not None


def booking_time(ledger: Ledger, at: str) -> str:
    """Where spend dated `at` is booked: `at` itself, or the first instant of the first open
    month after it when its month is locked."""
    month = _month(at)
    if not is_locked(ledger, month):
        return at
    while is_locked(ledger, month):
        y, m = map(int, month.split("-"))
        month = f"{y + m // 12}-{m % 12 + 1:02d}"
    return month_range(month)[0].isoformat()


def note_restated(ledger: Ledger, call_id: str, spent_at: str, booked_at: str, amount: int) -> None:
    """Called inside the booking's own transaction."""
    ledger.db.execute(
        "insert into restatements (call_id, spent_at, booked_at, amount) values (?, ?, ?, ?)",
        (call_id, spent_at, booked_at, amount),
    )


def restated_into(ledger: Ledger, month: str) -> list[dict]:
    """Revisions of earlier, locked months that were booked in `month`."""
    setup(ledger)
    start, end = month_range(month)
    rows = ledger.db.execute(
        "select call_id, spent_at, booked_at, amount from restatements"
        " where booked_at >= ? and booked_at < ? order by id", (start.isoformat(), end.isoformat()),
    )
    return [dict(zip(("call_id", "spent_at", "booked_at", "amount"), r, strict=True)) for r in rows]


def entries_digest(ledger: Ledger, month: str) -> tuple[int, int, str]:
    """(entries, sum of charges, SHA-256 over their chain hashes in order) for the month."""
    start, end = month_range(month)
    rows = ledger.db.execute(
        "select kind, amount, coalesce(hash, '') from entries where at >= ? and at < ? order by id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    digest = hashlib.sha256("\n".join(h for _, _, h in rows).encode()).hexdigest()
    return len(rows), sum(a for k, a, _ in rows if k == "charge"), digest


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _restatements_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["call_id", "spent_at", "booked_at", "amount_usd"])
    for r in rows:
        w.writerow([r["call_id"], r["spent_at"], r["booked_at"], f"{r['amount'] / 1e6:.6f}"])
    return out.getvalue()


def lock(ledger: Ledger, month: str, by: str, out: Path, book: PriceBook, software: str,
         at: datetime | None = None) -> Path:
    """Write the close package for a month that has ended and lock the month. Returns its folder."""
    from . import flux  # flux and export import connect, which imports this module
    from .export import journal

    setup(ledger)
    if not by.strip():
        raise ValueError("say who is locking it (--by)")
    _, end = month_range(month)
    if end > (at or datetime.now(UTC)):
        raise ValueError(f"{month} has not ended yet")
    if is_locked(ledger, month):
        raise ValueError(f"{month} is locked already; reopen it first")
    version = ledger.db.execute("select count(*) from locks where month = ?", (month,)).fetchone()[0] + 1
    folder = out / f"close-{month}-v{version}"
    if folder.exists():
        raise ValueError(f"{folder} exists already")
    report = flux.report(ledger, month, book)
    files = {
        "journal.csv": journal(ledger, month, number=f"AGENT-{month}-V{version}"),
        "flux.md": flux.markdown(report),
        "flux.json": json.dumps(report, indent=1) + "\n",
        "restatements.csv": _restatements_csv(restated_into(ledger, month)),
    }
    count, charged, digest = entries_digest(ledger, month)
    manifest = {
        "month": month, "version": version, "locked_at": now(), "locked_by": by.strip(),
        "software": f"quarter-ledger {software}",
        "entries": {"count": count, "charged_micros": charged, "sha256": digest},
        "files": {name: _sha(text.encode()) for name, text in files.items()},
    }
    body = json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    folder.mkdir(parents=True)
    for name, text in files.items():
        (folder / name).write_text(text)
    (folder / "manifest.json").write_text(body)
    with ledger._write():
        ledger.db.execute("insert into locks (month, version, at, by, manifest) values (?, ?, ?, ?, ?)",
                          (month, version, manifest["locked_at"], manifest["locked_by"], _sha(body.encode())))
        ledger._audit_row("period_lock", None, json.dumps({"month": month, "version": version, "by": by.strip(),
                                                            "manifest": _sha(body.encode())}, sort_keys=True))
    return folder


def reopen(ledger: Ledger, month: str, by: str, reason: str) -> int:
    """Unlock a month, with who and why. Returns the version reopened."""
    setup(ledger)
    if not by.strip() or not reason.strip():
        raise ValueError("say who is reopening it (--by) and why (--reason)")
    row = ledger.db.execute(
        "select version from locks where month = ? and reopened_at is null", (month,)
    ).fetchone()
    if row is None:
        raise ValueError(f"{month} is not locked")
    with ledger._write():
        ledger.db.execute("update locks set reopened_at = ?, reopened_by = ?, reason = ? where month = ? and version = ?",
                          (now(), by.strip(), reason.strip(), month, row[0]))
        ledger._audit_row("period_reopen", None, json.dumps({"month": month, "version": row[0], "by": by.strip(),
                                                              "reason": reason.strip()}, sort_keys=True))
    return row[0]


def locks(ledger: Ledger) -> list[dict]:
    setup(ledger)
    cols = ("month", "version", "at", "by", "manifest", "reopened_at", "reopened_by", "reason")
    rows = ledger.db.execute(f"select {', '.join(cols)} from locks order by month, version")
    return [dict(zip(cols, r, strict=True)) for r in rows]


def verify(ledger: Ledger, folder: Path) -> list[str]:
    """What no longer agrees between a close package and the ledger; empty when all does."""
    setup(ledger)
    body = (folder / "manifest.json").read_bytes()
    manifest = json.loads(body)
    problems = []
    for name, want in manifest["files"].items():
        path = folder / name
        if not path.exists():
            problems.append(f"{name} is missing")
        elif _sha(path.read_bytes()) != want:
            problems.append(f"{name} changed since the lock")
    row = ledger.db.execute("select manifest from locks where month = ? and version = ?",
                            (manifest["month"], manifest["version"])).fetchone()
    if row is None:
        problems.append("this ledger has no record of the lock")
    elif row[0] != _sha(body):
        problems.append("manifest.json is not the one this ledger locked")
    count, charged, digest = entries_digest(ledger, manifest["month"])
    if digest != manifest["entries"]["sha256"]:
        problems.append(f"the month's entries changed since the lock ({manifest['entries']['count']} then,"
                        f" {count} now; ${manifest['entries']['charged_micros'] / 1e6:,.2f} then,"
                        f" ${charged / 1e6:,.2f} now)")
    return problems
