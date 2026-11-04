import json
import subprocess
import sys
from datetime import UTC, datetime

from quarter_ledger.cli import main
from quarter_ledger.dashboard import render as dashboard
from quarter_ledger.mcp import handle
from quarter_ledger.money import to_micros, usd_display, usd_shown
from quarter_ledger.policy import load_caps
from quarter_ledger.status import render
from quarter_ledger.store import Ledger

CAPS = [
    {"scope": "all", "window": "month", "limit_usd": "100", "approve_above_usd": "5"},
    {"scope": "agent", "key": "*", "window": "day", "limit_usd": "10"},
    {"scope": "customer", "key": "acme", "window": "month", "limit_usd": "20"},
]


def seeded(tmp_path) -> str:
    """$4 by bot for acme and $1 by helper today, one approval waiting, helper paused."""
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    caps = load_caps(CAPS)
    for agent, customer, usd in (("bot", "acme", 4), ("helper", None, 1)):
        _, call_id = ledger.hold(caps, {"agent": agent, "customer": customer}, "openai", "m", 1)
        ledger.settle(call_id, to_micros(usd))
    decision, approval_id = ledger.hold(caps, {"agent": "bot"}, "openai", "m", to_micros(6))
    assert decision.outcome == "approve"
    ledger.db.execute("update approvals set id = 'apr_1' where id = ?", (approval_id,))
    ledger.pause("helper", "spending 10x usual")
    return db


def test_status_shows_spend_caps_approvals_and_paused(tmp_path, capsys):
    db = seeded(tmp_path)
    main(["--db", db, "status", "--caps", json.dumps(CAPS)])
    out = capsys.readouterr().out
    assert "Spent today $5.00, this month $5.00" in out
    assert "all spend              month [#...................] $5.00 of $100.00" in out
    assert "agent bot              day   [########............] $4.00 of $10.00" in out
    assert "agent helper           day   [##..................] $1.00 of $10.00" in out
    assert "customer acme          month [####................] $4.00 of $20.00" in out
    assert "Month end: not enough data yet" in out  # the ledger is seconds old
    assert "Approvals waiting: 1\n  apr_1  bot" in out and "$6.00" in out
    assert "Paused agents: 1\n  helper" in out and "spending 10x usual" in out
    assert "\033[" not in out  # not a terminal: no colours
    assert max(len(line) for line in out.splitlines()) <= 80


def test_status_without_caps_says_so(tmp_path):
    out = render(Ledger(str(tmp_path / "l.db")), [], datetime(2026, 11, 1, tzinfo=UTC))
    assert "Spent today $0.00, this month $0.00" in out and "No caps set" in out


def test_status_shows_a_charge_under_a_cent_as_more_than_nothing(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    _, call_id = ledger.hold([], {"agent": "bot"}, "openai", "m", 1)
    ledger.settle(call_id, to_micros("0.004"))
    assert "Spent today <$0.01, this month <$0.01" in render(ledger, [])
    assert usd_shown(0) == "$0.00" and usd_shown(9_999) == "<$0.01" and usd_shown(10_000) == "$0.01"
    assert usd_display(4_000) == "0.00"  # files keep the rounded figure
    assert "<b>&lt;$0.01</b>Spent by agents" in dashboard(ledger, [])


def call(ledger, caps, name, arguments, msg_id=1):
    return handle(ledger, caps, {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}})


def test_mcp_tools(tmp_path):
    ledger, caps = Ledger(seeded(tmp_path)), load_caps(CAPS)

    left = call(ledger, caps, "budget_left", {"agent": "bot", "customer": "acme"})["result"]
    assert left["isError"] is False
    assert json.loads(left["content"][0]["text"]) == left["structuredContent"]
    assert left["structuredContent"]["paused"] is None
    assert [(c["cap"], c["left_usd"]) for c in left["structuredContent"]["caps"]] == [
        ("all spend", "95.00"), ("agent bot", "6.00"), ("customer acme", "16.00")]
    assert left["structuredContent"]["caps"][0]["approve_above_usd"] == "5.00"
    helper = call(ledger, caps, "budget_left", {"agent": "helper"})["result"]["structuredContent"]
    assert helper["paused"] == "spending 10x usual" and len(helper["caps"]) == 2

    spent = call(ledger, caps, "spend", {"by": "customer", "period": "today"})["result"]
    assert spent["structuredContent"]["total_usd"] == "5.00"
    assert spent["structuredContent"]["rows"] == [
        {"customer": "(none)", "usd": "1.00"}, {"customer": "acme", "usd": "4.00"}]
    by_agent = call(ledger, caps, "spend", {})["result"]["structuredContent"]
    assert by_agent["by"] == "agent" and by_agent["period"] == "month"

    waiting = call(ledger, caps, "approval_status", {"id": "apr_1"})["result"]
    assert waiting["structuredContent"]["status"] == "pending"
    assert waiting["structuredContent"]["estimate_usd"] == "6.00"
    missing = call(ledger, caps, "approval_status", {"id": "apr_nope"})["result"]
    assert missing["isError"] is True and "no approval apr_nope" in missing["content"][0]["text"]
    bad = call(ledger, caps, "spend", {"period": "year"})["result"]
    assert bad["isError"] is True


def test_mcp_protocol_errors(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    unknown = call(ledger, [], "approve", {"id": "apr_1"})
    assert unknown["error"]["code"] == -32602 and "Unknown tool" in unknown["error"]["message"]
    assert handle(ledger, [], {"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"] == -32601
    assert handle(ledger, [], {"id": 3, "method": "tools/list"})["error"]["code"] == -32600
    assert handle(ledger, [], [{"jsonrpc": "2.0", "id": 4, "method": "ping"}])["error"]["code"] == -32600
    assert handle(ledger, [], {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_over_stdio(tmp_path):
    """The real process: initialize, initialized, list, call, a malformed line, then EOF."""
    db = seeded(tmp_path)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "budget_left", "arguments": {"agent": "bot"}}},
    ]
    stdin = "\n".join(json.dumps(m) for m in lines) + "\n{not json\n"
    done = subprocess.run(
        [sys.executable, "-c", "from quarter_ledger.cli import main; main()", "--db", db, "mcp", "--caps", json.dumps(CAPS)],
        input=stdin, capture_output=True, text=True, timeout=30, check=True,
    )
    init, listed, called, broken = [json.loads(line) for line in done.stdout.splitlines()]
    assert init["id"] == 1 and init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert init["result"]["serverInfo"]["name"] == "quarter-ledger"
    tools = listed["result"]["tools"]
    assert [t["name"] for t in tools] == ["budget_left", "spend", "approval_status", "why"]
    assert all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["type"] == "object"
               for t in tools)
    assert called["id"] == 3 and called["result"]["structuredContent"]["agent"] == "bot"
    assert broken == {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32700, "message": "Parse error"}}


def test_mcp_offers_its_latest_version_for_one_it_does_not_know(tmp_path):
    reply = handle(Ledger(str(tmp_path / "l.db")), [], {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert reply["result"]["protocolVersion"] == "2025-11-25"
