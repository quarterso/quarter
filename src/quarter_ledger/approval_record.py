"""One approval record across services: Stripe, any other source, and Quarter's own.

Each vendor keeps its own approvals and nobody joins them. Quarter appends every outside
approval event to its audit log as an approval_record row, so it sits in the same hash
chain as everything else. Quarter only records: it never calls a vendor to approve anything.

Stripe sends v2.core.approval_request.* as thin events: the notification carries the event
id, type, time, the approval request's id (apreq_...), livemode, context and the API request
that caused it. It carries no requester, reviewer, action or amount, and Stripe does not
document the approval request object yet. So a Stripe row leaves those blank; an import can
add them. Other sources (Auth0 CIBA logs, Slack) come in as JSON lines, one event a line.
"""

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

from .money import to_micros, usd_display
from .store import Ledger, chain_hash, month_range

ACTION = "approval_record"
DECISIONS = {"requested", "approved", "denied", "canceled", "expired", "succeeded", "failed"}
# Stripe's event names (docs.stripe.com/account/approvals) to the record's words.
STRIPE = {
    "v2.core.approval_request.created": "requested",
    "v2.core.approval_request.approved": "approved",
    "v2.core.approval_request.rejected": "denied",
    "v2.core.approval_request.canceled": "canceled",
    "v2.core.approval_request.expired": "expired",
    "v2.core.approval_request.succeeded": "succeeded",
    "v2.core.approval_request.failed": "failed",
}
FIELDS = ("source", "via", "id", "ref", "decision", "at", "requester", "approver", "action",
          "amount", "reason")
COLUMNS = ("source", "via", "id", "ref", "decision", "requested_at", "decided_at", "requester",
           "approver", "action", "amount_usd", "reason", "audit_id", "prev_hash", "chain_hash")


def record(ledger: Ledger, event: dict) -> bool:
    """Append one event to the chain; False if this source already sent this id."""
    row = {k: event.get(k) for k in FIELDS}
    return ledger.record_once(ACTION, row["source"], json.dumps(row, sort_keys=True),
                              (row["source"], row["id"]))


def from_stripe(event: dict) -> dict | None:
    """A Stripe approval request event as a record row; None for any other event type."""
    decision = STRIPE.get(event.get("type"))
    related = event.get("related_object") or {}
    if decision is None or not event.get("id") or not related.get("id"):
        return None
    request = (event.get("reason") or {}).get("request") or {}
    note = {"livemode": event.get("livemode"), "context": event.get("context"),
            "stripe_request": request.get("id"), "type": event["type"]}
    return {"source": "stripe", "via": "webhook", "id": event["id"], "ref": related["id"],
            "decision": decision, "at": _utc(event.get("created")),
            "reason": json.dumps(note, sort_keys=True)}


def from_line(line: str, number: int) -> dict:
    """One JSON line of an import: source, id, decision and at are required; requester,
    approver, action, ref, amount_usd and reason are kept if given."""
    try:
        given = json.loads(line)
    except json.JSONDecodeError as e:
        raise ValueError(f"line {number}: not JSON: {e}") from None
    if not isinstance(given, dict):
        raise ValueError(f"line {number}: not a JSON object")  # noqa: TRY004, a bad file
    missing = [k for k in ("source", "id", "decision", "at") if not given.get(k)]
    if missing:
        raise ValueError(f"line {number}: missing {', '.join(missing)}")
    if given["source"] == "quarter":
        raise ValueError(f"line {number}: source quarter is Quarter's own approvals")
    if given["decision"] not in DECISIONS:
        raise ValueError(f"line {number}: decision must be one of {', '.join(sorted(DECISIONS))}")
    row = {k: (str(given[k]) if given.get(k) is not None else None) for k in FIELDS}
    row["via"], row["amount"], row["at"] = "import", None, _utc(row["at"])
    if row["at"] is None:
        raise ValueError(f"line {number}: at is not an ISO time")
    if given.get("amount_usd") is not None:
        try:
            row["amount"] = to_micros(given["amount_usd"])
        except ArithmeticError:
            raise ValueError(f"line {number}: amount_usd is not a number") from None
    return row


def _utc(text) -> str | None:
    """An ISO time in UTC as the ledger stores it (no zone means UTC); None if not one."""
    try:
        at = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def import_lines(ledger: Ledger, lines) -> dict:
    """Every line is checked before any is recorded, so a bad file adds nothing."""
    rows = [from_line(line, n) for n, line in enumerate(lines, 1) if line.strip()]
    added = sum(record(ledger, row) for row in rows)
    return {"added": added, "already_recorded": len(rows) - added}


def rows(ledger: Ledger, month: str | None = None) -> list[dict]:
    """Every approval, outside and Quarter's own, oldest first, each with its chain row."""
    out = []
    cur = ledger.db.execute(
        "select a.id, a.at, a.action, a.client, a.detail, a.hash,"
        " (select p.hash from audit p where p.id < a.id and p.hash is not null"
        "  order by p.id desc limit 1)"
        f" from audit a where a.action in ('{ACTION}', 'approve', 'deny') order by a.id"
    )
    decisions = {}  # Quarter approval id: its decision's audit row
    for audit_id, at, action, client, detail, stored, prev in cur:
        chained = {"audit_id": audit_id, "prev_hash": prev or "", "chain_hash": stored}
        if action != ACTION:
            decisions[detail] = chained
            continue
        r = json.loads(detail)
        r["at"] = r["at"] or at  # an event that gave no time: when Quarter recorded it
        amount = r.get("amount")
        out.append({
            **{k: r.get(k) for k in ("source", "via", "id", "ref", "decision", "requester",
                                    "approver", "action", "reason")},
            "requested_at": r["at"] if r["decision"] == "requested" else None,
            "decided_at": None if r["decision"] == "requested" else r["at"],
            "amount_usd": usd_display(amount) if amount is not None else None,
            **chained,
        })
    for a in ledger.db.execute(
        "select id, created_at, agent, vendor, model, estimate, reason, status, decided_at,"
        " decided_by from approvals order by created_at"
    ):
        approval_id, created, agent, vendor, model, estimate, reason, status, decided, by = a
        out.append({
            "source": "quarter", "via": "gateway", "id": approval_id, "ref": approval_id,
            "decision": {"used": "approved", "pending": "requested"}.get(status, status),
            "requested_at": created, "decided_at": decided, "requester": agent,
            "approver": by, "action": f"{vendor} {model}" if model else vendor,
            "amount_usd": usd_display(estimate), "reason": reason,
            **decisions.get(approval_id, {"audit_id": None, "prev_hash": None, "chain_hash": None}),
        })
    if month:
        start, end = (t.isoformat() for t in month_range(month))
        out = [r for r in out if start <= (r["decided_at"] or r["requested_at"] or "") < end]
    return sorted(out, key=lambda r: r["decided_at"] or r["requested_at"] or "")


README = """Quarter approval record{scope}

approvals.csv  every approval Quarter knows of, one row per event, from each source:
               quarter    the gateway's own approvals (calls, payments, actions over a line)
               stripe     Stripe approval requests, from its signed webhook (via webhook)
               any other  imported JSON lines, as the customer gave them (via import)
               requester is the agent (or account) asking; approver the person deciding.
               Stripe's webhook does not say who asked, who decided, what or how much,
               so those are blank on its rows unless an import added them.
chain.json     the ledger's own check of both hash chains when this file was made, and
               the audit chain's head hash. Record that hash outside Quarter.

Each row with a chain_hash is a row of Quarter's audit log. To check one yourself:
chain_hash = sha256 of prev_hash, then at, action, client and detail of that audit row,
joined by the unit separator 0x1F (chain_hash in quarter_ledger/store.py). Run
`quarter-ledger approvals verify FILE` to do this for every row in this file.
A Quarter row with no chain_hash is still waiting, or was decided where no decision row
was written (the CLI before this record existed, or a bill Quarter refused).

For SOC 2 change approval evidence (CC8.1, with CC6.3 for segregation of duties): the
action is the change, requester who asked for it, approver who authorised it, decided_at
when, decision what was decided, and chain_hash shows the record was not edited later.
Quarter provides evidence and does not certify compliance.
"""


def files(
    ledger: Ledger, month: str | None = None, chain: dict | None = None
) -> tuple[dict[str, str], dict]:
    """README.txt, approvals.csv and chain.json, and the summary chain.json holds.
    chain: a check of both chains just made, so a caller that has one does not redo it."""
    found = rows(ledger, month)
    details = {r[0]: r[1:] for r in ledger.db.execute(
        "select id, at, action, client, detail from audit where id in"
        f" ({','.join('?' * len(found))})", [r["audit_id"] for r in found]
    )} if found else {}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([*COLUMNS, "audit_at", "audit_action", "audit_client", "audit_detail"])
    for r in found:
        writer.writerow([r[c] for c in COLUMNS] + list(details.get(r["audit_id"], ("",) * 4)))
    head = ledger.db.execute(
        "select id, hash from audit where hash is not null order by id desc limit 1"
    ).fetchone() or (None, None)
    summary = {
        "month": month,
        "made_at": datetime.now(UTC).isoformat(),
        "approvals": len(found),
        "chain": chain or ledger.verify_chain(),
        "audit_head": {"id": head[0], "hash": head[1]},
        "approvals_csv_sha256": hashlib.sha256(out.getvalue().encode()).hexdigest(),
    }
    return {
        "README.txt": README.format(scope=f" for {month}" if month else ""),
        "approvals.csv": out.getvalue(),
        "chain.json": json.dumps(summary, indent=2),
    }, summary


def export(ledger: Ledger, path: str, month: str | None = None) -> dict:
    """A zip an auditor can read: approvals.csv, chain.json and a README."""
    written, summary = files(ledger, month)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in written.items():
            z.writestr(name, text)
    return summary


def verify(path: str) -> dict:
    """Recompute every row's chain hash from the export alone, with no ledger. Takes an
    approvals export, or an audit pack, which holds the same files under approval_record/."""
    with zipfile.ZipFile(path) as z:
        folder = "approval_record/" if "approval_record/approvals.csv" in z.namelist() else ""
        text = z.read(folder + "approvals.csv").decode()
        summary = json.loads(z.read(folder + "chain.json"))
    checked, bad = 0, []
    for r in csv.DictReader(io.StringIO(text)):
        if not r["chain_hash"]:
            continue
        values = (r["audit_at"], r["audit_action"], r["audit_client"], r["audit_detail"])
        checked += 1
        if chain_hash(r["prev_hash"], values) != r["chain_hash"]:
            bad.append(r["audit_id"])
    file_ok = hashlib.sha256(text.encode()).hexdigest() == summary.get("approvals_csv_sha256")
    chain_ok = all(t["intact"] for t in summary["chain"].values())
    return {"rows_checked": checked, "bad_rows": bad, "file_matches_chain_json": file_ok,
            "ledger_chain_intact_when_made": chain_ok, "audit_head": summary["audit_head"],
            "intact": not bad and file_ok and chain_ok}
