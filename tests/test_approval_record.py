import csv
import io
import json
import time
import urllib.error
import urllib.request
import zipfile

import pytest
from test_issuing import signed
from test_proxy import ADMIN_KEY, AGENT_KEY, PRICES, start

from quarter_ledger import approval_record
from quarter_ledger.cli import main
from quarter_ledger.evidence import audit_pack
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

SECRET = "whsec_test_approvals"  # a fake secret: tests only


def thin_event(kind: str, event_id: str = "evt_test_1", request_id: str = "apreq_test_1") -> dict:
    """A Stripe thin event notification, shaped as docs.stripe.com/event-destinations shows
    (id, object, type, livemode, created, context, reason, related_object). Stripe's docs list
    the v2.core.approval_request.* names but show no example of one, nor the approval request
    object; a requester, reviewer, action or amount here would be invented, so there are none."""
    return {
        "id": event_id,
        "object": "v2.core.event",
        "type": f"v2.core.approval_request.{kind}",
        "livemode": False,
        "created": "2026-11-03T13:00:28.435Z",
        "context": None,
        "reason": {"type": "request", "request": {"id": "req_test_1", "idempotency_key": None}},
        "related_object": {"id": request_id, "type": "v2.core.approval_request",
                           "url": f"/v2/core/approval_requests/{request_id}"},
    }


def post(url: str, event: dict, signature: str | None = None):
    raw = json.dumps(event).encode()
    req = urllib.request.Request(
        url + "/approvals/stripe", raw, {"stripe-signature": signature or signed(raw, SECRET)}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def gateway(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, stripe_approvals_secret=SECRET)
    server = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{server.server_port}", gw
    server.shutdown()


def test_stripe_events_are_recorded_once_each(gateway):
    url, gw = gateway
    assert post(url, thin_event("created")) == (200, {"received": True, "recorded": True})
    assert post(url, thin_event("rejected", "evt_test_2")) == (200, {"received": True, "recorded": True})
    assert post(url, thin_event("rejected", "evt_test_2"))[1]["recorded"] is False  # sent twice
    rows = approval_record.rows(gw.ledger)
    assert [(r["source"], r["via"], r["ref"], r["decision"]) for r in rows] == [
        ("stripe", "webhook", "apreq_test_1", "requested"),
        ("stripe", "webhook", "apreq_test_1", "denied"),
    ]
    assert rows[0]["requested_at"] == "2026-11-03T13:00:28.435000+00:00"
    assert rows[1]["decided_at"] == rows[0]["requested_at"]
    assert all(r["chain_hash"] and r["requester"] is None for r in rows)
    assert json.loads(rows[0]["reason"])["stripe_request"] == "req_test_1"
    assert gw.ledger.verify_chain()["audit"]["intact"]


def test_stripe_signature_age_and_secret_are_checked(gateway, tmp_path):
    url, gw = gateway
    raw = json.dumps(thin_event("approved")).encode()
    assert post(url, thin_event("approved"), signed(raw, "whsec_wrong"))[0] == 400
    assert post(url, thin_event("approved"), signed(raw, SECRET, int(time.time()) - 600))[0] == 400
    assert post(url, {"type": "v1.billing.meter.no_meter_found", "id": "evt_x"}) == (
        200, {"received": True, "recorded": False})
    assert approval_record.rows(gw.ledger) == []
    unset = Gateway(Ledger(str(tmp_path / "u.db")), [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY)
    server = start(serve(unset, port=0))
    try:
        assert post(f"http://127.0.0.1:{server.server_port}", thin_event("approved"))[0] == 503
    finally:
        server.shutdown()


LINES = [
    {"source": "auth0", "id": "ciba_1", "decision": "requested", "at": "2026-11-02T09:00:00",
     "requester": "billing-agent", "action": "refund order 1234", "amount_usd": "40.00"},
    {"source": "auth0", "id": "ciba_2", "decision": "approved", "at": "2026-11-02T09:05:00Z",
     "requester": "billing-agent", "approver": "ann@example.com", "ref": "ciba_1"},
    {"source": "slack", "id": "slack_1", "decision": "denied", "at": "2026-11-02T12:00:00+02:00",
     "approver": "sam"},
]


def test_import_adds_each_line_once_and_refuses_a_bad_file(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    text = [json.dumps(line) for line in LINES]
    assert approval_record.import_lines(ledger, text) == {"added": 3, "already_recorded": 0}
    assert approval_record.import_lines(ledger, text) == {"added": 0, "already_recorded": 3}
    rows = approval_record.rows(ledger)
    assert [(r["source"], r["decision"], r["via"]) for r in rows] == [
        ("auth0", "requested", "import"), ("auth0", "approved", "import"), ("slack", "denied", "import")]
    assert rows[0]["amount_usd"] == "40.00" and rows[0]["requested_at"] == "2026-11-02T09:00:00+00:00"
    assert rows[2]["decided_at"] == "2026-11-02T10:00:00+00:00"
    for bad, says in (
        ({"source": "x", "id": "1", "decision": "maybe", "at": "2026-11-02"}, "decision"),
        ({"source": "x", "id": "1", "decision": "approved"}, "missing at"),
        ({"source": "quarter", "id": "1", "decision": "approved", "at": "2026-11-02"}, "quarter"),
        ({"source": "x", "id": "1", "decision": "approved", "at": "soon"}, "ISO"),
        ({"source": "x", "id": "1", "decision": "approved", "at": "2026-11-02", "amount_usd": "lots"}, "amount"),
    ):
        with pytest.raises(ValueError, match=says):
            approval_record.import_lines(ledger, [json.dumps({**LINES[0], "id": "new"}), json.dumps(bad)])
    assert len(approval_record.rows(ledger)) == 3  # a bad line anywhere adds nothing


def test_quarter_approvals_sit_beside_the_others_and_export_verifies(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    asked = ledger.request_action_approval({"agent": "ops-agent"}, "railway", "volumeDelete", "deletes a volume")
    waiting = ledger.request_action_approval({"agent": "ops-agent"}, "vercel", "deploy", "production deploy")
    with pytest.raises(SystemExit):
        main(["--db", db, "approve", asked, "--as", "Ann"])
    lines = tmp_path / "auth0.jsonl"
    lines.write_text("".join(json.dumps(line) + "\n" for line in LINES))
    main(["--db", db, "approvals", "import", str(lines)])
    capsys.readouterr()

    own = {r["id"]: r for r in approval_record.rows(ledger) if r["source"] == "quarter"}
    assert own[asked]["decision"] == "approved" and own[asked]["approver"] == "Ann"
    assert own[asked]["requester"] == "ops-agent" and own[asked]["action"] == "railway volumeDelete"
    assert own[asked]["chain_hash"]  # the CLI decision is now in the chain
    assert own[waiting]["decision"] == "requested" and own[waiting]["chain_hash"] is None

    out = str(tmp_path / "approvals.zip")
    with pytest.raises(SystemExit) as done:
        main(["--db", db, "approvals", "export", "--out", out])
    assert done.value.code == 0
    assert "5 approvals" in capsys.readouterr().out
    with pytest.raises(SystemExit) as done:
        main(["--db", db, "approvals", "verify", out])
    result = json.loads(capsys.readouterr().out)
    assert done.value.code == 0 and result["intact"] and result["rows_checked"] == 4

    with zipfile.ZipFile(out) as z:  # an edit to one row of the file is caught
        files = {n: z.read(n).decode() for n in z.namelist()}
    files["approvals.csv"] = files["approvals.csv"].replace("ann@example.com", "bob@example.com")
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    assert approval_record.verify(out)["bad_rows"] and not approval_record.verify(out)["intact"]

    ledger.db.execute("update audit set detail = replace(detail, 'sam', 'max') where action = 'approval_record'")
    with pytest.raises(SystemExit) as done:  # an edit in the ledger itself fails the export
        main(["--db", db, "approvals", "export", "--out", out, "--month", "2026-11"])
    assert done.value.code == 1


def test_plain_approvals_still_lists_waiting_calls(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    approval_id = Ledger(db).request_action_approval({"agent": "a"}, "railway", "volumeDelete", "why")
    main(["--db", db, "approvals"])
    assert approval_id in capsys.readouterr().out


def test_the_audit_pack_holds_the_month_of_the_approval_record(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    approval_record.import_lines(ledger, [json.dumps(line) for line in LINES])
    out = str(tmp_path / "pack.zip")
    audit_pack(ledger, "2026-11", out)
    with zipfile.ZipFile(out) as z:
        record = z.read("approval_record/approvals.csv").decode()
        controls = json.loads(z.read("controls.json"))["controls"]
    assert [r["id"] for r in csv.DictReader(io.StringIO(record))] == ["ciba_1", "ciba_2", "slack_1"]
    result = approval_record.verify(out)
    assert result["intact"] and result["rows_checked"] == 3
    cc81 = next(c for c in controls if c["ref"] == "CC8.1")
    assert "approval_record/approvals.csv" in cc81["files"]
    audit_pack(ledger, "2026-10", out)  # another month: the record is there, with no rows
    assert approval_record.verify(out) | {"audit_head": None} == {
        "rows_checked": 0, "bad_rows": [], "file_matches_chain_json": True,
        "ledger_chain_intact_when_made": True, "audit_head": None, "intact": True}
