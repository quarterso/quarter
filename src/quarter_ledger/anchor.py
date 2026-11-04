"""Anchors: the chain heads, fixed at a moment, for someone outside Quarter to hold.

Anyone with write access could rewrite a whole chain. An anchor names each chain's head
row, how many chained rows lead to it and its hash, and its digest is one SHA-256 of
those. Kept outside Quarter, a later export whose chain does not pass through that head,
at that row, means history was rewritten.

Opt-in, the digest alone goes to an OpenTimestamps calendar, which commits it to Bitcoin
within hours. The reply is a pending proof; the standard `ots` tool upgrades and checks it.
No ledger data leaves: the digest is a hash of hashes.
"""

import csv
import hashlib
import http.client
import io
import json
import sqlite3
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path

from . import incident
from .store import CHAINED, Ledger, chain_hash, now

SCHEMA = "quarter.anchor/1"
CALENDAR = "https://a.pool.opentimestamps.org"
TIMEOUT = 5  # seconds: a calendar that does not answer by then is recorded as not submitted
# From python-opentimestamps: DetachedTimestampFile.HEADER_MAGIC, version 1, OpSHA256's tag,
# and the PendingAttestation and BitcoinBlockHeaderAttestation tags.
OTS_HEADER = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94\x01\x08"
PENDING, BITCOIN = bytes.fromhex("83dfe30d2ef90c8e"), bytes.fromhex("0588960d73d71901")
MAX_REPLY = 10000  # as the ots client: a timestamp is a few hundred bytes


def heads(ledger: Ledger) -> dict:
    """Each chain's head row id, chained rows up to it, and its hash."""
    out = {}
    for table in CHAINED:
        row_id, rows = ledger.db.execute(
            f"select max(id), count(*) from {table} where hash is not null"
        ).fetchone()
        head = ledger.db.execute(f"select hash from {table} where id = ?", (row_id,)).fetchone()
        out[table] = {"id": row_id, "rows": rows, "hash": head[0] if head else None}
    return out


def digest(chain_heads: dict) -> str:
    return hashlib.sha256(
        json.dumps(chain_heads, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def submit(calendar: str, value: bytes, timeout: float = TIMEOUT) -> bytes:
    """POST the raw 32-byte digest to <calendar>/digest, as the ots client does. The reply
    is a timestamp for that digest; with the header in front it is an .ots file."""
    if not calendar.startswith(("https://", "http://")):
        raise ValueError("the calendar must be an http or https URL")
    req = urllib.request.Request(
        calendar.rstrip("/") + "/digest", data=value,
        headers={"Accept": "application/vnd.opentimestamps.v1", "User-Agent": "quarter-ledger"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        reply = resp.read(MAX_REPLY + 1)
    if len(reply) > MAX_REPLY or not (PENDING in reply or BITCOIN in reply):
        raise ValueError("the calendar's reply is not a timestamp")
    return OTS_HEADER + value + reply


def make(ledger: Ledger, out: str, calendar: str | None = None) -> dict:
    """Write the anchor to `out` and to the audit log. The calendar is asked before the
    audit row is written, so no lock is held while waiting on the network."""
    chain_heads = heads(ledger)
    record = {"schema": SCHEMA, "made_at": now(), "heads": chain_heads,
              "digest": digest(chain_heads)}
    if calendar is None:
        record["ots"] = {"status": "off"}
    else:
        proof = Path(out).with_suffix(".ots")
        try:
            proof.write_bytes(submit(calendar, bytes.fromhex(record["digest"])))
            record["ots"] = {"status": "submitted", "calendar": calendar, "file": proof.name}
        except (OSError, ValueError, http.client.HTTPException) as e:  # URLError, timeouts: OSError
            record["ots"] = {"status": "not submitted", "calendar": calendar,
                             "reason": str(e)[:200]}
    ledger.audit("anchor", "cli", json.dumps(record, sort_keys=True))
    Path(out).write_text(json.dumps(record, indent=2) + "\n")
    return record


def _rows(header: list[str], rows, columns: tuple) -> list[list]:
    at = [header.index(c) for c in ("id", *columns, "hash")]
    return [[r[i] for i in at] for r in rows]


def chains(path: str) -> dict[str, tuple[str | None, bool, list[list]]]:
    """By table: the hash before the first row (None if unknown), whether the rows start
    at the chain's first row, and [id, *columns, hash] for each chained row, from a
    ledger or backup, an audit pack or an incident pack."""
    raw = Path(path).read_bytes()
    if raw.startswith(b"SQLite format 3\x00"):
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            return {
                t: ("", True, [list(r) for r in db.execute(
                    f"select id, {', '.join(cols)}, hash from {t} where hash is not null order by id")])
                for t, cols in CHAINED.items()
            }
    if zipfile.is_zipfile(path):
        out = {}
        with zipfile.ZipFile(path) as z:
            for table, name in (("entries", "entries.csv"), ("audit", "audit_log.csv")):
                header, *rows = csv.reader(io.TextIOWrapper(z.open(name), "utf-8", newline=""))
                rows = [r for r in _rows(header, rows, CHAINED[table]) if r[-1]]
                out[table] = (None, False, [[int(r[0]), *r[1:]] for r in rows])
        return out
    pack = json.loads(raw)
    if pack.get("schema") != incident.SCHEMA:
        raise ValueError(f"not a ledger, audit pack or incident pack: {path}")
    return {t: (c["prev"], c["prev"] == "", c["rows"]) for t, c in pack["chains"].items()}


def check_chain(prev: str | None, from_start: bool, rows: list[list], head: dict) -> dict:
    """Recompute each link the file holds, and find the anchored head in it. With prev
    None the file leaves rows out (a month's audit pack), so a row whose row before it is
    missing is taken as written; `linked` says whether the head row itself was recomputed."""
    gaps = prev is None
    found, linked, rows_match, bad, last_id, count = head["id"] is None, None, None, None, None, 0
    for row_id, *values, stored in rows:
        if gaps and last_id is not None and row_id != last_id + 1:
            prev = None
        if prev is not None and chain_hash(prev, tuple(values)) != stored:
            bad = row_id
            break
        if row_id == head["id"]:
            found, linked = stored == head["hash"], prev is not None
            rows_match = count + 1 == head["rows"] if from_start else None
        prev, last_id, count = stored, row_id, count + 1
    return {"anchored": head, "found": found, "linked": linked, "rows_match": rows_match,
            "intact": bad is None, "first_bad_id": bad}


def proof_status(path: Path, value: str) -> dict:
    data = path.read_bytes()
    if not data.startswith(OTS_HEADER + bytes.fromhex(value)):
        return {"file": str(path), "status": "not a proof of this anchor's digest"}
    status = "complete" if BITCOIN in data else "pending"
    return {"file": str(path), "status": status,
            "next": (f"ots upgrade {path} (once the calendar has committed it, a few hours), then "
                     if status == "pending" else "")
            + f"ots verify -d {value} {path}"}


def verify(anchor_path: str, export: str) -> dict:
    record = json.loads(Path(anchor_path).read_text())
    if record.get("schema") != SCHEMA:
        raise ValueError(f"not an anchor: {anchor_path}")
    found = chains(export)
    result = {
        "anchor": anchor_path, "made_at": record["made_at"], "file": export,
        "digest_matches": digest(record["heads"]) == record["digest"],
        "chains": {t: check_chain(*found[t], h) for t, h in record["heads"].items() if t in found},
    }
    ots = record.get("ots", {})
    if ots.get("file"):
        proof = Path(anchor_path).parent / ots["file"]
        result["ots"] = (proof_status(proof, record["digest"]) if proof.exists()
                         else {"file": str(proof), "status": "missing"})
    else:
        result["ots"] = {k: v for k, v in ots.items() if k in ("status", "reason")} or {"status": "off"}
    checked = result["chains"].values()
    result["ok"] = result["digest_matches"] and len(checked) == len(record["heads"]) and all(
        c["found"] and c["intact"] and c["rows_match"] is not False for c in checked)
    return result
