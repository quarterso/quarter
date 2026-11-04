"""Evidence export: the month's audit pack.

Audit pack (a zip) holds what an auditor asks for in a walkthrough of controls over
agent spend: the caps in force, every ledger entry with its chain hash, approvals and
who decided them (with signed receipts when a signing key is set), the audit log,
pauses, the journal, and a fresh check of both hash chains.
"""

import csv
import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime

from . import approval_record, controls, receipts, register
from .export import journal
from .rules import load_rules
from .store import Ledger, month_range

README = """Quarter audit pack for {month}

entries.csv    every hold, release and charge, each with a SHA-256 hash of itself and
               the row before it. chain.json says whether the whole chain recomputes.
approvals.csv  calls over an approval line, who decided them and when, and the call
               each approval paid for. receipts.jsonl holds signed receipts if a
               signing key was set.
audit_log.csv  finance actions and failed sign-ins, also hash-chained.
pauses.csv     agents paused for spending far faster than usual, and who resumed them.
caps.json      the caps in force when this pack was made.
               chain.json also gives retention_days: how long the ledger promises
               to keep every entry (QUARTER_RETENTION_DAYS), or null if none was set.
rules.json     the egress rules in force when this pack was made, if any: what agents
               may do on each upstream. Each call to an upstream that is not a model is
               an upstream_call row in the audit log, and each tool call held in a
               model's reply a tool_held row.
journal.csv    the month-end journal.
approval_record/
               the approval record for the month: approvals from every source
               (Quarter's own, Stripe, imports), each with its audit chain hash, and
               its own README and chain.json. `quarter-ledger approvals verify` on
               this pack recomputes each row's hash from the file alone.
controls.md    each AIUC-1 control, SOC 2 criterion and EU AI Act article next to the
               evidence above,
               with counts and file names; controls.json holds the same as data.
               Its last section is the quarterly owner review of agents, for this
               month's quarter and the one before: each agent kept or retired, its
               owner, who reviewed it and when, the agents active and not reviewed,
               and those past their review or with no owner when the pack was made.
               Quarter provides evidence and does not certify compliance.

To check the chain yourself: for each row in id order, hash = sha256(previous hash,
then each listed column, joined by the unit separator 0x1F). See chain_hash in
quarter_ledger/store.py. Record chain.json's head hash outside Quarter; a later pack
whose chain does not contain it means history was rewritten.
"""


def _csv(rows: list[tuple], header: list[str]) -> str:
    out = io.StringIO()
    csv.writer(out).writerows([header, *rows])
    return out.getvalue()


def _query(ledger: Ledger, sql: str, args=()) -> tuple[list[str], list[tuple]]:
    cur = ledger.db.execute(sql, args)
    return [c[0] for c in cur.description], cur.fetchall()


def audit_pack(
    ledger: Ledger,
    month: str,
    path: str,
    caps: list | None = None,
    signing_key: str | None = None,
    retention_days: int | None = None,
    people: dict[str, str] | None = None,
    rules: list | None = None,
    register_rule: register.RegisterRule | None = None,
) -> dict:
    """people: name to role for everyone in QUARTER_PEOPLE (never their keys); rules: the
    rules file as parsed JSON; register_rule: QUARTER_REGISTER, for the review grace days."""
    start, end = month_range(month)
    span = (start.isoformat(), end.isoformat())

    # Read row by row into the zip below: a busy month is millions of entries.
    entries = ledger.db.execute("select * from entries where at >= ? and at < ? order by id", span)
    files: dict[str, str | sqlite3.Cursor] = {"README.txt": README.format(month=month), "entries.csv": entries}
    head, rows = _query(
        ledger,
        "select * from approvals where created_at >= ? and created_at < ? order by created_at",
        span,
    )
    files["approvals.csv"] = _csv(rows, head)
    if signing_key:
        decided = [
            dict(zip(head, r, strict=True)) for r in rows if r[head.index("status")] != "pending"
        ]
        files["receipts.jsonl"] = "".join(
            json.dumps({"approval": a["id"], "receipt": receipts.sign(a, signing_key)}) + "\n"
            for a in decided
        )
    head, rows = _query(ledger, "select * from audit where at >= ? and at < ? order by id", span)
    files["audit_log.csv"] = _csv(rows, head)
    head, rows = _query(ledger, "select * from pauses where at >= ? and at < ? order by id", span)
    files["pauses.csv"] = _csv(rows, head)
    files["caps.json"] = json.dumps([c.__dict__ for c in caps or []], indent=2, default=str)
    if rules is not None:
        files["rules.json"] = json.dumps(rules, indent=2)
    files["journal.csv"] = journal(ledger, month)
    reviews = register.evidence(ledger, start, register_rule)

    chain = ledger.verify_chain()
    heads = {
        t: (
            ledger.db.execute(
                f"select id, hash from {t} where hash is not null order by id desc limit 1"
            ).fetchone()
            or (None, None)
        )
        for t in ("entries", "audit")
    }
    summary = {
        "month": month,
        "made_at": datetime.now(UTC).isoformat(),
        "chain": chain,
        "heads": {t: {"id": i, "hash": h} for t, (i, h) in heads.items()},
        "retention_days": retention_days,
    }
    files["chain.json"] = json.dumps(summary, indent=2)
    record, _ = approval_record.files(ledger, month, chain)
    files |= {f"approval_record/{name}": text for name, text in record.items()}
    listed = load_rules(rules or []).listed()
    facts = controls.gather(ledger, span, chain, people, retention_days, listed, reviews)
    files["controls.md"], files["controls.json"] = controls.render(month, facts, set(files))
    summary["warnings"] = controls.warnings(facts)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            if isinstance(text, str):
                z.writestr(name, text)
                continue
            info = zipfile.ZipInfo(name, datetime.now().timetuple()[:6])
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o600 << 16  # as writestr
            with z.open(info, "w", force_zip64=True) as raw, io.TextIOWrapper(raw, "utf-8", newline="") as out:
                rows = csv.writer(out)
                rows.writerow([c[0] for c in text.description])
                rows.writerows(text)
    return summary
