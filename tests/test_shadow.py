import hashlib
import json
from datetime import UTC, datetime, timedelta

from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, call, start

from quarter_ledger.dashboard import render
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap, Shadow, load_caps
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.runaway import load_runaway
from quarter_ledger.shadow import replay, shadow_report
from quarter_ledger.store import Ledger

BOT = {"agent": "bot", "task": None, "customer": None}


def would_holds(ledger):
    return [json.loads(r["detail"]) for r in ledger.audit_log() if r["action"] == "would_hold"]


def test_a_cap_in_shadow_lets_the_call_through_and_records_what_it_would_have_held(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = load_caps([{"scope": "agent", "key": "bot", "window": "day", "limit_usd": "1", "mode": "shadow"}])
    decision, call_id = ledger.hold(caps, BOT, "openai", "m", to_micros(2))
    assert decision.outcome == "allow" and call_id
    assert decision.would_hold.outcome == "block" and "day cap reached" in decision.would_hold.reason
    [row] = would_holds(ledger)
    assert row["call_id"] == call_id and row["amount"] == to_micros(2) and row["outcome"] == "block"
    ledger.settle(call_id, to_micros("1.5"))  # spend is recorded exactly as when enforced
    assert ledger.spent("agent", "bot", None) == to_micros("1.5")


def test_a_shadow_flag_on_one_cap_leaves_every_other_rule_enforcing(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "bot", "day", to_micros(1), mode="shadow"), Cap("all", "*", "day", to_micros(3))]
    assert ledger.hold(caps, BOT, "openai", "m", to_micros(2))[0].outcome == "allow"
    decision, ref = ledger.hold(caps, BOT, "openai", "m", to_micros(2))  # $4 against the $3 cap
    assert decision.outcome == "block" and ref is None and decision.would_hold is None
    assert len(would_holds(ledger)) == 1
    ledger.pause("bot", "loop")  # a pause is not a cap: only QUARTER_MODE=shadow lets it through
    assert ledger.hold(caps, BOT, "openai", "m", 1)[0].outcome == "block"
    decision, _ = ledger.hold(caps, BOT, "openai", "m", 1, shadow=Shadow(everything=True))
    assert decision.outcome == "allow" and decision.would_hold.reason == "agent paused: loop"


def test_shadow_records_who_would_have_been_asked_never_the_owner(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.set_owner("bot", "Sam")
    caps = [Cap("agent", "bot", "day", to_micros(100), approve_above=to_micros(1), mode="shadow")]
    shadow = Shadow(approvers=("Priya", "Sam"))
    decision, _ = ledger.hold(caps, BOT, "exa", "m", to_micros(2), shadow=shadow)
    assert decision.would_hold.outcome == "approve" and ledger.approvals("pending") == []
    assert would_holds(ledger)[0]["asked"] == ["Priya"]


def test_gateway_in_shadow_mode_forwards_and_flags(tmp_path):
    from quarter_ledger.payments import authorised

    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros("0.5"), approve_above=to_micros("0.2"))]
    gw = Gateway(ledger, caps, PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32,
                 payees={"x402": ["0xGood"], "unlisted": "deny"}, mode="shadow")
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        pay = {"rail": "x402", "payee": "0xBad", "amount_usd": "0.3"}
        status, body = call(url + "/authorize", pay)
        reply = json.loads(body)
        assert status == 200 and authorised(reply["authorisation"], "s" * 32, "0xBad", 300_000)
        assert reply["shadow"]["would"] == "block" and "not allowed" in reply["shadow"]["reason"]
        assert call(url + "/authorize", {**pay, "payee": "0xGood", "amount_usd": "0.4"})[0] == 200  # over the cap
        assert [r["outcome"] for r in would_holds(ledger)] == ["block", "block"]
        assert ledger.spent("agent", "a1", None) == to_micros("0.7")
    finally:
        server.shutdown()
    assert "everything" in gw.in_shadow()[0]


def test_payee_shadow_does_not_shadow_caps(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros("0.5"))]
    gw = Gateway(ledger, caps, PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32,
                 payees={"x402": ["0xGood"], "mode": "shadow"})
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert call(url + "/authorize", {"rail": "x402", "payee": "0xBad", "amount_usd": "0.3"})[0] == 200
        status, body = call(url + "/authorize", {"rail": "x402", "payee": "0xGood", "amount_usd": "0.3"})
        assert status == 402 and json.loads(body)["error"] == "cap_reached"
    finally:
        server.shutdown()
    assert gw.in_shadow() == ["payee lists"]


def test_model_call_over_a_shadow_cap_reaches_the_vendor(tmp_path):
    import urllib.request
    from http.server import ThreadingHTTPServer

    from test_proxy import FakeVendor

    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls = []
    FakeVendor.usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("customer", "acme", "month", to_micros("0.05"), mode="shadow")]
    gw = Gateway(ledger, caps, PRICES, {"fake": f"http://127.0.0.1:{vendor.server_port}"}, {AGENT_KEY}, ADMIN_KEY)
    proxy = start(serve(gw, port=0))
    try:
        body = json.dumps({"model": "m", "max_tokens": 100_000}).encode()  # holds $0.2, over $0.05
        req = urllib.request.Request(f"http://127.0.0.1:{proxy.server_port}/fake/v1/chat", body, HEADERS)
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200 and resp.headers["x-quarter-shadow"] == "would_block"
        assert len(FakeVendor.calls) == 1
        assert ledger.totals("customer") == [("acme", "fake", to_micros("0.002"))]
        assert would_holds(ledger)[0]["reason"] == "customer acme: month cap reached"
    finally:
        proxy.shutdown()
        vendor.shutdown()


def test_loop_stop_in_shadow_records_once_and_pauses_nothing(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, runaway=load_runaway('{"mode": "shadow"}'))
    gw._pause("bot", "spent $9 in 5 minutes")
    gw._pause("bot", "spent $9 in 5 minutes")
    assert ledger.paused("bot") is None
    [row] = would_holds(ledger)
    assert row["reason"] == "loop stop: spent $9 in 5 minutes" and row["outcome"] == "block"
    assert gw.in_shadow() == ["loop stops"]


def test_startup_and_dashboard_say_shadow_plainly(tmp_path, monkeypatch, capsys):
    from quarter_ledger import cli

    started = []

    class Server:
        def serve_forever(self, poll_interval=0.5):
            pass

    monkeypatch.setattr(cli, "serve", lambda gw, host, port: started.append(gw) or Server())
    caps = json.dumps([{"scope": "agent", "key": "bot", "limit_usd": "5", "mode": "shadow"}])
    monkeypatch.setenv("QUARTER_CAPS", caps)
    monkeypatch.delenv("QUARTER_MODE", raising=False)
    db = str(tmp_path / "l.db")
    cli.main(["--db", db, "serve"])
    out = capsys.readouterr().out
    assert "SHADOW MODE: cap agent bot (month)" in out
    monkeypatch.setenv("QUARTER_MODE", "shadow")
    cli.main(["--db", db, "serve"])
    assert "SHADOW MODE: everything" in capsys.readouterr().out
    changed = [r["detail"] for r in Ledger(db).audit_log() if r["action"] == "config.changed"]
    assert changed[0] == "mode"  # the newest first: turning shadow on is in the change log
    page = render(started[-1].ledger, started[-1].caps, shadow=started[-1].in_shadow())
    assert "Shadow mode." in page and "(shadow)" in page
    assert "Shadow mode." not in render(started[-1].ledger, started[-1].caps)
    monkeypatch.setenv("QUARTER_MODE", "shadw")
    try:
        cli.main(["--db", db, "serve"])
    except SystemExit as e:
        assert "QUARTER_MODE" in str(e)
    else:
        raise AssertionError("a misspelt mode must stop the gateway")


def dump(path) -> str:
    import sqlite3

    with sqlite3.connect(path) as db:
        return hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()


def history(ledger):
    """Calls through the real hold: allowed, asked, approved and retried, refused, under a
    standing approval, and a payment off the payee list."""
    caps = [Cap("agent", "*", "day", to_micros(3), approve_above=to_micros(1)),
            Cap("all", "*", "month", to_micros(5), over="approve")]
    payees = {"x402": ["0xgood"]}
    ledger.add_standing("std-bot", "exa", to_micros(2), to_micros(10), "Priya")
    outcomes = []
    for agent, vendor, model, usd in [
        ("bot", "openai", "m", "0.5"), ("bot", "openai", "m", "1.5"), ("bot", "openai", "m", "0.9"),
        ("bot", "openai", "m", "1"), ("bot", "openai", "m", "0.9"), ("std-bot", "exa", "m", "1.5"),
        ("pay", "x402:0xbad", "x402", "0.2"), ("pay", "x402:0xgood", "x402", "0.2"),
        ("bot", "openai", "m", "2"), ("std-bot", "exa", "m", "0.4"),
    ]:
        ids = {"agent": agent, "task": None, "customer": None}
        ask = "0xbad is not on the x402 payee list" if vendor == "x402:0xbad" else None
        decision, ref = ledger.hold(caps, ids, vendor, model, to_micros(usd), ask=ask)
        if decision.outcome == "approve" and usd == "1.5" and agent == "bot":  # a person approves, the agent retries
            ledger.decide_approval(ref, True, "Priya")
            decision, ref = ledger.hold(caps, ids, vendor, model, to_micros(usd), ref)
        if decision.outcome == "allow":
            ledger.settle(ref, to_micros(usd) // 2)
        outcomes.append(decision.outcome)
    return caps, payees, outcomes


def test_replay_with_the_live_rules_gives_the_recorded_decisions_and_writes_nothing(tmp_path):
    path = str(tmp_path / "l.db")
    ledger = Ledger(path)
    caps, payees, outcomes = history(ledger)
    assert {"allow", "approve", "block"} <= set(outcomes)  # every kind of decision is in it
    before = dump(path)
    out = replay(ledger, (caps, payees), (caps, payees))
    assert dump(path) == before  # read only
    assert len(out["calls"]) == 10  # the approved call counts once; its first ask is the record
    assert out["self_check"] == {"compared": 10, "matched": 10}
    assert out["newly_held"]["calls"] == out["no_longer_held"]["calls"] == out["changed"]["calls"] == 0

    looser = [Cap("agent", "*", "day", to_micros(100))]
    out = replay(ledger, (caps, payees), (looser, {}))
    assert out["no_longer_held"]["calls"] == 3 and out["newly_held"]["calls"] == 0  # asked twice, refused once
    stricter = [Cap("agent", "*", "day", to_micros(1))]
    out = replay(ledger, (caps, payees), (stricter, payees))
    assert out["newly_held"]["calls"] > 0 and out["newly_held"]["block"] == out["newly_held"]["calls"]
    assert out["by_rule"][0]["rule"].endswith("day cap reached")
    assert dump(path) == before


def test_replay_reads_a_shadow_ledger_as_enforced(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "bot", "day", to_micros(1), mode="shadow")]
    for _ in range(3):
        ledger.settle(ledger.hold(caps, BOT, "openai", "m", to_micros("0.6"))[1], to_micros("0.6"))
    out = replay(ledger, (caps, {}), (caps, {}))
    assert [c["recorded"] for c in out["calls"]] == ["allow", "block", "block"]
    assert out["self_check"] == {"compared": 3, "matched": 3}


def test_shadow_report_totals_by_rule_and_standing_cover(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros(3), approve_above=to_micros(1), mode="shadow")]
    shadow = Shadow(approvers=("Priya",))
    for usd in ("1.5", "1.2", "0.5"):  # the third takes the day past $3
        ledger.hold(caps, BOT, "exa", "m", to_micros(usd), shadow=shadow)
    ledger.add_standing("bot", "exa", to_micros("1.5"), to_micros(50), "Priya")
    out = shadow_report(ledger, datetime.now(UTC) - timedelta(days=1))
    assert out["calls"] == 3 and out["amount"] == to_micros("3.2")
    rules = {g["rule"]: g for g in out["by_rule"]}
    line = rules["agent bot: call over approval limit"]
    assert line["count"] == 2 and line["amount"] == to_micros("2.7") and line["asked"] == ["Priya"]
    assert rules["agent bot: day cap reached"]["outcome"] == "block"
    [cover] = out["standing"]
    assert cover["from"] == "in force" and cover["count"] == 2 and cover["amount"] == to_micros("2.7")


def test_shadow_report_and_replay_commands(tmp_path, monkeypatch, capsys):
    from quarter_ledger import cli

    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    caps = [{"scope": "agent", "key": "bot", "window": "day", "limit_usd": "1", "mode": "shadow"}]
    for _ in range(2):
        ledger.settle(ledger.hold(load_caps(caps), BOT, "openai", "m", to_micros("0.6"))[1], to_micros("0.6"))
    cli.main(["--db", db, "shadow-report", "--since", f"{datetime.now(UTC):%Y-%m-%d}", "--csv"])
    rows = capsys.readouterr().out.splitlines()
    assert rows == ["rule,outcome,calls,amount_usd,would_ask", "agent bot: day cap reached,block,1,0.60,"]
    monkeypatch.delenv("QUARTER_PAYEES", raising=False)
    looser = json.dumps([{**caps[0], "limit_usd": "2"}])
    cli.main(["--db", db, "replay", "--caps", looser, "--live-caps", json.dumps(caps)])
    out = capsys.readouterr().out
    assert "for 2 of 2 calls" in out and "No longer held: 1 calls, $0.60" in out
