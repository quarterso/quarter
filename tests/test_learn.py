import json

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, PRICES, call, start

from quarter_ledger import bills, dashboard
from quarter_ledger.learn import suggestions
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

IDS = {"agent": "coder", "task": None, "customer": None}
LINE = [Cap("agent", "*", "month", to_micros(1000), approve_above=to_micros(1))]


def asked(ledger, caps, amount, approve=True, vendor="openai", ids=IDS):
    decision, approval = ledger.hold(caps, ids, vendor, "m", to_micros(amount))
    assert decision.outcome == "approve"
    ledger.decide_approval(approval, approve, "dana")
    return approval


def test_repeated_approvals_become_a_suggestion_people_accept(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for amount in ("2", "3.40", "2.50", "4", "3"):
        asked(ledger, LINE, amount)
    out = suggestions(ledger)
    s = out["suggestions"][0]
    assert out["decisions"] == 5 and out["would_not_ask"] == 5
    assert s["kind"] == "standing" and s["approvers"] == ["dana"]
    assert (s["largest"], s["up_to"], s["monthly"]) == (to_micros(4), to_micros(4), to_micros(15))
    assert "approved all 5 requests from coder to openai" in s["text"]

    rule = ledger.add_standing("coder", "OpenAI", s["up_to"], s["monthly"], by="dana")
    assert suggestions(ledger)["suggestions"] == []  # covered now
    decision, call_id = ledger.hold(LINE, IDS, "openai", "m", to_micros(3))
    assert decision.outcome == "allow" and call_id
    assert any(r["action"] == "standing_used" and rule in r["detail"] for r in ledger.audit_log())
    assert ledger.hold(LINE, IDS, "openai", "m", to_micros(5))[0].outcome == "approve"  # over up to
    assert (
        ledger.hold(LINE, IDS, "anthropic", "m", to_micros(3))[0].outcome == "approve"
    )  # another vendor
    assert (
        ledger.hold(LINE, {**IDS, "agent": "other"}, "openai", "m", to_micros(3))[0].outcome
        == "approve"
    )
    assert "Suggested from your approvals" not in dashboard.render(ledger, LINE)
    assert "Standing approvals" in dashboard.render(ledger, LINE)


def test_standing_approvals_never_stretch_a_cap_or_a_month(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [
        Cap("agent", "coder", "month", to_micros(10), approve_above=to_micros(1), over="approve")
    ]
    ledger.add_standing("coder", "openai", to_micros(4), to_micros(7), by="dana")
    assert ledger.hold(caps, IDS, "openai", "m", to_micros(4))[0].outcome == "allow"
    assert (
        ledger.hold(caps, IDS, "openai", "m", to_micros(4))[0].outcome == "approve"
    )  # 8 > 7 a month
    ledger.add_standing("coder", "openai", to_micros(4), to_micros(100), by="dana")
    assert (
        ledger.hold(caps, IDS, "openai", "m", to_micros(4))[0].outcome == "allow"
    )  # 8 of the cap's 10
    decision = ledger.hold(caps, IDS, "openai", "m", to_micros(3))[0]
    assert (
        decision.outcome == "approve" and "cap reached" in decision.reason
    )  # past the cap: a person


def test_revoked_expired_owned_and_bank_checks(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    rule = ledger.add_standing("coder", "openai", to_micros(5), to_micros(50), by="dana")
    assert ledger.revoke_standing(rule, "sam") and not ledger.revoke_standing(rule, "sam")
    assert ledger.hold(LINE, IDS, "openai", "m", to_micros(2))[0].outcome == "approve"

    rule = ledger.add_standing("coder", "openai", to_micros(5), to_micros(50), by="dana", days=1)
    ledger.db.execute("update standing set ends = '2000-01-01' where id = ?", (rule,))
    assert ledger.hold(LINE, IDS, "openai", "m", to_micros(2))[0].outcome == "approve"

    ledger.db.execute("insert into owners (agent, owner) values ('coder', 'Dana')")
    with pytest.raises(PermissionError, match="owns"):
        ledger.add_standing("coder", "openai", to_micros(5), to_micros(50), by="dana")
    with pytest.raises(ValueError):
        ledger.add_standing("coder", "openai", to_micros(5), to_micros(4), by="sam")

    ap = {"agent": "ap", "task": None, "customer": None}
    ledger.add_standing("ap", "invoice:Acme", to_micros(500), to_micros(5000), by="sam")
    b = bills.add(ledger, LINE, {}, ap, "Acme", "INV-1", to_micros(100), "bank 1234")
    assert (
        b["status"] == "waiting" and "first payment" in b["reason"]
    )  # a bank check is never skipped


def test_always_denied_is_pointed_out(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for _ in range(3):
        asked(ledger, LINE, "9", approve=False, vendor="x402:0xBad")
    s = suggestions(ledger)["suggestions"][0]
    assert s["kind"] == "always_denied" and s["denied"] == 3 and "x402:0xBad" in s["text"]


def test_suggestions_and_standing_over_http(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for amount in ("2", "2", "2", "2", "2"):
        asked(ledger, LINE, amount)
    gw = Gateway(ledger, LINE, PRICES, {}, {AGENT_KEY}, ADMIN_KEY)
    server = start(serve(gw, port=0))
    try:
        url, admin = f"http://127.0.0.1:{server.server_port}", {"x-quarter-key": ADMIN_KEY}
        status, body = call(url + "/approvals/suggestions", None, admin)
        assert status == 200 and json.loads(body)["suggestions"][0]["up_to"] == to_micros(2)
        assert call(url + "/standing", {"agent": "coder"}, {"x-quarter-key": AGENT_KEY})[0] == 401
        rule = {"agent": "coder", "vendor": "openai", "up_to_usd": "2", "monthly_usd": "20"}
        status, body = call(url + "/standing", rule, admin)
        rule_id = json.loads(body)["id"]
        assert (
            status == 200
            and json.loads(call(url + "/standing", None, admin)[1])[0]["id"] == rule_id
        )
        assert call(url + f"/standing/{rule_id}/revoke", {}, admin)[0] == 200
        assert json.loads(call(url + "/standing", None, admin)[1]) == []
    finally:
        server.shutdown()


def test_monthly_limit_covers_the_calls_under_the_approval_line(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for _ in range(5):
        approval = asked(ledger, LINE, "2")
        _, call_id = ledger.hold(LINE, IDS, "openai", "m", to_micros(2), approval)  # the approved retry
        ledger.settle(call_id, to_micros(2))
    for _ in range(30):  # small calls that never needed a person
        _, call_id = ledger.hold(LINE, IDS, "openai", "m", to_micros("0.50"))
        ledger.settle(call_id, to_micros("0.50"))
    s = suggestions(ledger)["suggestions"][0]
    assert s["monthly"] == to_micros(25)  # all 25 spent with openai that month, not the 10 approved


def test_shared_admin_key_cannot_pay_a_bill(tmp_path):
    from test_roles import PEOPLE

    ledger = Ledger(str(tmp_path / "l.db"))
    b = bills.add(ledger, [], {}, IDS, "Acme", "INV-1", to_micros(40), "bank 1234")
    ledger.decide_approval(b["approval_id"], True, "Ann")
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, people=PEOPLE)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}/bills/{b['id']}/pay"
        body = {"method": "manual", "ref": "r"}
        assert call(url, body, {"x-quarter-key": ADMIN_KEY})[0] == 403
        status, reply = call(url, body, {"x-quarter-key": "finance-key"})
        assert status == 200 and json.loads(reply)["paid_by"] == "Fay"
    finally:
        server.shutdown()
