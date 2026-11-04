"""QUARTER_FAIL_OPEN=model: plain model calls keep working while the ledger cannot be written.

When the hold cannot be written (the database is locked, full or gone), a model call that
nothing would send to a person goes to the vendor anyway, and is written to a fallback file
beside the ledger instead: one line before it is sent, with the hold, and one once it is
settled, with the charge. If that file cannot be written either, the call is refused.

Everything else stays closed: tool-call holds, call rules that ask a person, payments,
bills, cards, a call over a cap's per-call approval line, a retry carrying an approval,
and every call while QUARTER_REGISTER routes agents to people.

`quarter-ledger ingest-fail-open` books the file into the ledger once it is back: each call
as a hold, its release and the charge, at the time it was made, with a fail_open audit row.
A call with no settled line is charged its full hold, so spend is never undercounted.
Booking is by call id, so running it twice books nothing twice.
"""

import json
import os
import threading
import uuid
from datetime import UTC, datetime


class FailOpenLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def write(self, entry: dict) -> None:
        """Append one line and make it durable before the call goes on. Raises OSError."""
        line = json.dumps(entry, sort_keys=True).encode() + b"\n"
        with self._lock:
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def passed(self, ids: dict, vendor: str, model: str, held: int, reason: str) -> str:
        call_id = "fo" + uuid.uuid4().hex
        self.write({"id": call_id, "at": datetime.now(UTC).isoformat(), "agent": ids["agent"],
                    "task": ids.get("task"), "customer": ids.get("customer"), "vendor": vendor,
                    "model": model, "held": held, "reason": reason[:200]})
        return call_id

    def settled(self, call_id: str, charged: int) -> None:
        self.write({"id": call_id, "charged": charged})


def ingest(ledger, path: str) -> dict:
    """Book a fallback file into the ledger; then rename it, so the gateway starts a new one."""
    calls: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            entry = json.loads(line)
            calls.setdefault(entry["id"], {}).update(entry)
    booked = unsettled = orphans = 0
    for call in calls.values():
        if "at" not in call:  # its first line went to an earlier file, booked from there
            orphans += 1
            continue
        charged = call.get("charged")
        if ledger.book_fail_open(call, call["held"] if charged is None else charged):
            booked += 1
            unsettled += charged is None
    done = f"{path}.ingested-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    os.replace(path, done)
    return {"calls": len(calls) - orphans, "booked": booked, "charged_in_full": unsettled,
            "settled_lines_without_a_call": orphans, "moved_to": done}
