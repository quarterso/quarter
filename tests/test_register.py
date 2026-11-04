import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from test_proxy import AGENT_KEY, PRICES, call, gateway, start  # noqa: F401

from quarter_ledger.cli import main
from quarter_ledger.dashboard import render
from quarter_ledger.evidence import audit_pack
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap, Shadow
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.register import (
    RegisterRule,
    listing,
    load_register,
    previous,
    quarter_of,
    quarter_span,
    reasons,
    review,
)
from quarter_ledger.store import Ledger

BOT = {"agent": "bot", "task": None, "customer": None}
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)  # 2026-Q4, past the 14 grace days
ON = RegisterRule(grace_days=0)  # grace 0, so routing tests do not depend on today's date


def entry(ledger, agent, at, model="m", vendor="openai", amount=1000):
    """A charge recorded at a given time, as an old call would have left it."""
    ledger.db.execute(
        "insert into entries (call_id, kind, at, agent, vendor, model, amount) values (?, 'charge', ?, ?, ?, ?, ?)",
        (f"c{at}{agent}{model}", at.isoformat(), agent, vendor, model, amount),
    )


def kept(ledger, agent, quarter):
    ledger.db.execute(
        "insert into reviews (at, quarter, agent, owner, reviewer, result) values (?, ?, ?, 'Sam', 'Sam', 'keep')",
        (datetime.now(UTC).isoformat(), quarter, agent),
    )


def statuses(ledger, keys=("bot",), now=NOW):
    return {r["agent"]: r["status"] for r in listing(ledger, keys, now=now)}


def test_quarters():
    assert quarter_of(NOW) == "2026-Q4" and previous("2026-Q1") == "2025-Q4"
    assert quarter_span("2026-Q4") == (datetime(2026, 10, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    assert quarter_span("2026-Q2")[1] == datetime(2026, 7, 1, tzinfo=UTC)


def test_load_register():
    assert load_register(None) is None and load_register("off") is None
    assert load_register("on") == RegisterRule() and load_register("shadow").shadow
    assert load_register('{"dormant_days": 60, "grace_days": 7}') == RegisterRule(60, 7)
    with pytest.raises(ValueError):
        load_register('{"mode": "loud"}')


def test_list_gives_each_status(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for agent in ("bot", "old", "late", "ghost"):
        ledger.set_owner(agent, "Sam")
    entry(ledger, "bot", NOW - timedelta(days=1))
    kept(ledger, "bot", "2026-Q3")
    entry(ledger, "old", NOW - timedelta(days=40))  # dormant, and kept for Q3 so not overdue
    kept(ledger, "old", "2026-Q3")
    entry(ledger, "late", datetime(2026, 9, 1, tzinfo=UTC))  # there in Q3, never reviewed
    entry(ledger, "late", NOW - timedelta(days=1), model="x402", vendor="x402:0xabc", amount=to_micros(2))
    entry(ledger, "stray", NOW - timedelta(days=2))  # no key, no owner
    got = statuses(ledger, keys=("bot", "old", "late", "ghost", "fresh"))
    assert got["bot"] == ["ok"]
    assert got["old"] == ["dormant"]
    assert got["late"] == ["review overdue (2026-Q3)"]
    assert got["ghost"] == ["never used"]
    assert got["fresh"] == ["no owner", "never used"]
    assert got["stray"] == ["no owner", "seen without a key"]
    in_grace = statuses(ledger, keys=("late",), now=datetime(2026, 10, 5, tzinfo=UTC))
    assert in_grace["late"] == ["review due (2026-Q3)"]
    late = next(r for r in listing(ledger, ["late"], now=NOW) if r["agent"] == "late")
    assert late["last_payment"] and late["last_call"].startswith("2026-09-01")
    assert late["month_to_date"] == to_micros(2)


def test_spend_booked_by_connect_counts_as_use_but_not_as_a_keyless_call(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for agent, call_id in (("billed", "obs:litellm:r1"), ("billed", "obs:csv:r2"), ("stray", "c1")):
        ledger.db.execute(
            "insert into entries (call_id, kind, at, agent, vendor, model, amount)"
            " values (?, 'charge', ?, ?, 'openai', 'gpt-4o', ?)",
            (call_id, (NOW - timedelta(days=2)).isoformat(), agent, to_micros(240)),
        )
    got = statuses(ledger, keys=())
    assert got["billed"] == ["no owner"]  # used, through a vendor's bill, never with a Quarter key
    assert got["stray"] == ["no owner", "seen without a key"]
    billed = next(r for r in listing(ledger, now=NOW) if r["agent"] == "billed")
    assert billed["last_call"] is None and billed["last_observed"].startswith("2026-11-01")
    page = render(ledger, [], keys=set(), month="2026-11")
    assert "<td>billed</td><td>none</td><td>2026-11-01</td>" in page and "never used" not in page


def test_list_shows_the_caps_and_payee_rules_that_apply(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_standing("bot", "exa", to_micros(1), to_micros(10), by="Priya")
    caps = [Cap("agent", "*", "day", to_micros(5)), Cap("agent", "other", "day", 1), Cap("all", "*", "month", to_micros(100))]
    [row] = listing(ledger, ["bot"], caps, {"x402": ["0xa", "0xb"]})
    assert row["caps"] == ["agent bot day $5.00", "all spend month $100.00"]
    assert row["payees"] == ["x402: 2 listed, others approve", "exa up to $1.00"]


def test_review_keep_and_retire(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.set_owner("bot", "Sam")
    done = review(ledger, "2026-Q4", "bot", "Sam", keep=True, approvers=())  # the owner confirms their own
    assert done["result"] == "keep" and done["owner"] == "Sam"
    assert ledger.hold([], BOT, "openai", "m", 1)[0].outcome == "allow"
    review(ledger, "2026-Q4", "bot", "Priya", keep=False, approvers=("Priya",), note="project over")
    decision, _ = ledger.hold([], BOT, "openai", "m", 1)
    assert decision.outcome == "block" and "retired in the 2026-Q4 review by Priya" in decision.reason
    assert statuses(ledger, now=datetime.now(UTC))["bot"][0] == "retired"
    rows = [json.loads(r["detail"]) for r in ledger.audit_log() if r["action"] == "agent_review"]
    assert [r["result"] for r in rows] == ["retire", "keep"] and rows[0]["note"] == "project over"
    assert all(r["intact"] for r in ledger.verify_chain().values())


def test_retired_agent_key_is_refused_at_the_gateway(gateway):  # noqa: F811
    url, ledger = gateway
    review(ledger, "2026-Q4", "a1", "Priya", keep=False, approvers=("Priya",))
    status, body = call(url + "/fake/v1/chat/completions", {"model": "m", "max_tokens": 1})
    assert status == 423 and json.loads(body)["error"] == "agent_paused"


def test_who_may_review(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.set_owner("bot", "Sam")
    with pytest.raises(PermissionError):
        review(ledger, "2026-Q4", "bot", "Mallory", keep=True, approvers=("Priya",))
    with pytest.raises(PermissionError):  # never the agent itself, even under an approver's name
        review(ledger, "2026-Q4", "bot", "bot", keep=True, approvers=("bot",))
    assert [r["action"] for r in ledger.audit_log()].count("review_refused") == 2
    review(ledger, "2026-Q4", "bot", "priya", keep=True, approvers=("Priya",))
    with pytest.raises(ValueError):
        review(ledger, "2099-Q1", "bot", "Sam", keep=True, approvers=())
    with pytest.raises(ValueError):
        review(ledger, "2026-4", "bot", "Sam", keep=True, approvers=())
    with pytest.raises(ValueError):  # keeping needs an owner
        review(ledger, "2026-Q4", "nobody", "Priya", keep=True, approvers=("Priya",))
    review(ledger, "2026-Q4", "nobody", "Priya", keep=False, approvers=("Priya",))


@pytest.mark.parametrize("reason", ["agent has no owner", "agent unused for 30 days", "quarterly review overdue"])
def test_each_reason_sends_the_call_to_a_person(tmp_path, reason):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.register = ON
    now, this_quarter = datetime.now(UTC), quarter_of(datetime.now(UTC))
    if reason != "agent has no owner":
        ledger.set_owner("bot", "Sam")
    if reason == "agent unused for 30 days":
        entry(ledger, "bot", now - timedelta(days=31))
        kept(ledger, "bot", this_quarter)
    if reason == "quarterly review overdue":
        entry(ledger, "bot", quarter_span(this_quarter)[0] - timedelta(days=5))
        entry(ledger, "bot", now - timedelta(days=1))
    decision, approval_id = ledger.hold([], BOT, "openai", "m", 1)
    assert decision.outcome == "approve" and decision.reason.startswith(reason)
    assert ledger.approval(approval_id)["reason"] == decision.reason


def test_owned_active_reviewed_agent_passes_and_routing_off_passes_all(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    assert ledger.hold([], BOT, "openai", "m", 1)[0].outcome == "allow"  # register off: as before
    ledger.register = ON
    assert ledger.hold([], BOT, "openai", "m", 1)[0].outcome == "approve"
    ledger.set_owner("bot", "Sam")
    assert ledger.hold([], BOT, "openai", "m", 1)[0].outcome == "allow"


def test_shadow_records_what_the_register_would_have_held(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.register = RegisterRule(grace_days=0, shadow=True)
    decision, call_id = ledger.hold([], BOT, "openai", "m", 1)
    assert decision.outcome == "allow" and call_id and ledger.approvals("pending") == []
    assert decision.would_hold.outcome == "approve" and decision.would_hold.reason == "agent has no owner"
    ledger.register = ON  # QUARTER_MODE=shadow quiets an enforcing register too
    decision, _ = ledger.hold([], BOT, "openai", "m", 1, shadow=Shadow(everything=True, approvers=("Priya",)))
    assert decision.outcome == "allow" and decision.would_hold.reason == "agent has no owner"
    rows = [json.loads(r["detail"]) for r in ledger.audit_log() if r["action"] == "would_hold"]
    assert len(rows) == 2 and rows[0]["asked"] == ["Priya"]


def test_approving_the_call_is_not_a_review(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.register = ON
    decision, approval_id = ledger.hold([], BOT, "openai", "m", 5)
    assert decision.outcome == "approve"
    ledger.decide_approval(approval_id, True, "Priya")
    assert ledger.hold([], BOT, "openai", "m", 5, approval_id)[0].outcome == "allow"
    assert ledger.hold([], BOT, "openai", "m", 5)[0].outcome == "approve"  # asks again
    assert ledger.db.execute("select count(*) from reviews").fetchone()[0] == 0
    assert "no owner" in statuses(ledger, now=datetime.now(UTC))["bot"]


def test_a_cheaper_model_does_not_skip_the_register(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.register = ON
    caps = [Cap("agent", "bot", "day", to_micros(1))]
    decision, _ = ledger.hold(caps, BOT, "openai", "m", to_micros(2), fallback=("mini", 1))
    assert decision.outcome != "allow" and decision.fallback_to is None


def test_model_calls_and_payments_are_routed_at_the_gateway(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.register = ON
    gw = Gateway(ledger, [], PRICES, {"fake": "http://127.0.0.1:9"}, {AGENT_KEY}, signing_key="k" * 32)
    proxy = start(serve(gw, port=0))
    url = f"http://127.0.0.1:{proxy.server_port}"
    try:
        status, body = call(url + "/fake/v1/chat/completions", {"model": "m", "max_tokens": 1})
        assert status == 403 and json.loads(body)["reason"] == "agent has no owner"
        status, body = call(url + "/authorize", {"rail": "x402", "payee": "0xabc", "amount_usd": "0.01"})
        assert status == 403 and json.loads(body)["reason"] == "agent has no owner"
        ledger.set_owner("a1", "Sam")
        status, body = call(url + "/authorize", {"rail": "x402", "payee": "0xabc", "amount_usd": "0.01"})
        assert status == 200 and json.loads(body)["authorisation"]
    finally:
        proxy.shutdown()


def test_audit_pack_carries_the_reviews(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    month = f"{datetime.now(UTC):%Y-%m}"
    ledger.set_owner("bot", "Sam")
    ledger.hold([], BOT, "openai", "m", 1)
    ledger.hold([], {"agent": "idle"}, "openai", "m", 1)
    review(ledger, quarter_of(datetime.now(UTC)), "bot", "Sam", keep=True, approvers=(), note="still in use")
    with pytest.raises(PermissionError):
        review(ledger, quarter_of(datetime.now(UTC)), "idle", "idle", keep=False, approvers=())
    out = str(tmp_path / "pack.zip")
    audit_pack(ledger, month, out)
    z = zipfile.ZipFile(out)
    md, data = z.read("controls.md").decode(), json.loads(z.read("controls.json"))
    reviews = data["facts"]["reviews"]
    this = reviews["quarters"][quarter_of(datetime.now(UTC))]
    assert [(r["agent"], r["reviewer"], r["result"]) for r in this["reviews"]] == [("bot", "Sam", "keep")]
    assert this["not_reviewed"] == ["idle"] and reviews["no_owner_now"] == ["idle"] and reviews["refused"] == 1
    assert "- bot: keep, owner Sam, reviewed by Sam on" in md and "(still in use)" in md
    mapped = {c["ref"]: c for c in data["controls"]}
    for ref in ("CC6.2", "E004"):
        assert any(e.startswith("Quarterly owner reviews of agents") for e in mapped[ref]["evidence"])
    assert any("reviews refused" in e for e in mapped["CC6.3"]["evidence"])


def test_dashboard_shows_review_status(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.hold([], BOT, "openai", "m", 1)
    page = render(ledger, [], keys={"bot"})
    assert "Agent register" in page and "no owner" in page


def test_cli_list_and_review(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    ledger.hold([], BOT, "openai", "m", 1)
    monkeypatch.setenv("QUARTER_AGENT_KEYS", "bot=k1")
    monkeypatch.setenv("QUARTER_PEOPLE", json.dumps({"Priya": {"key": "x" * 24, "role": "approver"}}))
    main(["--db", db, "owner", "bot", "Sam"])
    quarter = quarter_of(datetime.now(UTC))
    with pytest.raises(SystemExit):
        main(["--db", db, "agents", "review", "--quarter", quarter, "--agent", "bot", "--by", "Mallory", "--keep"])
    main(["--db", db, "agents", "review", "--quarter", quarter, "--agent", "bot", "--by", "Priya", "--keep"])
    capsys.readouterr()
    main(["--db", db, "agents", "list", "--csv"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("agent,owner,caps,payees") and lines[1].startswith("bot,Sam,")
    assert f",{quarter}," in lines[1] and lines[1].endswith("Priya,keep,ok")
    main(["--db", db, "agents", "list"])
    assert "review " + quarter + " keep by Priya  ok" in capsys.readouterr().out


def test_reasons_are_pure():
    old = (NOW - timedelta(days=60)).isoformat()
    assert reasons("Sam", old, NOW.isoformat(), "2026-Q3", NOW, RegisterRule()) == []
    assert [s for s, _ in reasons(None, old, old, None, NOW, RegisterRule())] == [
        "no owner", "dormant", "review overdue (2026-Q3)"]
