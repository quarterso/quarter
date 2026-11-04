"""Run forensics through the running gateway, with Claude Code-shaped calls.

The stand-in vendor is the one in test_plug_in.py: it answers in Anthropic's wire format
with whatever usage the test sets, cache reads and writes included.
"""

import http.client
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest
from test_plug_in import AGENT_KEY, ANTHROPIC_USAGE, CACHED, PRICES, Vendor, post, start
from test_slack import FakeSlack

from quarter_ledger import forensics
from quarter_ledger.cli import main
from quarter_ledger.mcp import handle
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.slack import Slack
from quarter_ledger.store import Ledger, dicts

ADMIN = "admin-key-for-forensics"
API_KEY = {"x-api-key": "sk-ant-api03-x"}
LOGIN = {"authorization": "Bearer sk-ant-oat01-x"}  # a claude.ai subscription login


def tool(name, description):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}


TOOLS = [
    tool("Bash", "Run a shell command"),
    tool("Read", "Read a file"),
    tool("mcp__github__create_issue", "Create a GitHub issue"),
    tool("mcp__github__list_issues", "List GitHub issues"),
]


def system(marked=True, ttl=None, today="2026-11-02"):
    mark = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
    return [
        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
        {"type": "text", "text": f"Working directory: /repo. Today's date is {today}.",
         **({"cache_control": mark} if marked else {})},
    ]


def history(turns, command=lambda n: f"ls src/{n}", marked=True, result=lambda n: f"output {n}"):
    """A user ask, then `turns` rounds of a Bash tool_use and its tool_result."""
    messages = [{"role": "user", "content": [{"type": "text", "text": "Fix the failing test."}]}]
    for n in range(turns):
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": f"toolu_{n:03}", "name": "Bash",
             "input": {"command": command(n)}},
        ]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{n:03}", "content": result(n)},
        ]})
    if marked:
        messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return messages


def body(turns, marked=True, **changes):
    return {"model": "claude-cached", "max_tokens": 32000, "system": system(marked),
            "tools": TOOLS, "messages": history(turns, marked=marked),
            "metadata": {"user_id": "user_x"}, **changes}


def usage(read, write, uncached=5, ttl="5m"):
    other = "1h" if ttl == "5m" else "5m"
    return {**ANTHROPIC_USAGE, "input_tokens": uncached, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
            "cache_creation": {f"ephemeral_{ttl}_input_tokens": write,
                               f"ephemeral_{other}_input_tokens": 0}}


@pytest.fixture
def gw(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), Vendor))
    fake_slack = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeSlack))
    Vendor.seen, Vendor.error, Vendor.gzip_body = [], None, False
    fake_slack.calls = []
    Vendor.anthropic_usage, Vendor.wait_after_first = ANTHROPIC_USAGE, None
    FakeSlack.calls = fake_slack.calls
    ledger = Ledger(str(tmp_path / "ledger.db"))
    slack = Slack("xoxb-test", "secret", "C1", {},
                  api=f"http://127.0.0.1:{fake_slack.server_port}")
    gateway = Gateway(ledger, [], PRICES, {"anthropic": f"http://127.0.0.1:{vendor.server_port}"},
                      agent_keys={AGENT_KEY}, admin_key=ADMIN, slack=slack)
    proxy = start(serve(gateway, port=0))
    yield proxy.server_port, ledger
    for server in (proxy, vendor, fake_slack):
        server.shutdown()


def send(port, task, request, used, auth=API_KEY, **hints):
    """One Claude Code turn: the session id is the task; hints are x-claude-code-* headers."""
    Vendor.anthropic_usage = used
    headers = {"x-claude-code-session-id": task, **auth,
               **{f"x-claude-code-{k.replace('_', '-')}": v for k, v in hints.items()}}
    status, _, reply = post(port, "/anthropic/v1/messages?beta=true", request, headers)
    return status, reply


def calls(ledger, task):
    return dicts(ledger.db.execute("select * from calls where task = ? order by at", (task,)))


def charged(ledger, task):
    return ledger.db.execute(
        "select coalesce(sum(amount), 0) from entries where kind = 'charge' and task = ?", (task,)
    ).fetchone()[0]


# Each cache-break reason: the second call changes one thing and reads nothing from cache.

BREAKS = {
    "tools added: github (1)":
        lambda b: {**b, "tools": [*TOOLS, tool("mcp__github__get_pr", "Get a pull request")]},
    "tools removed: github (2)": lambda b: {**b, "tools": TOOLS[:2]},
    "tools reordered: builtin": lambda b: {**b, "tools": [TOOLS[1], TOOLS[0], *TOOLS[2:]]},
    "tools changed: github":
        lambda b: {**b, "tools": [*TOOLS[:2], tool("mcp__github__create_issue", "New"), TOOLS[3]]},
    "system prompt changed": lambda b: {**b, "system": system(today="2026-11-03")},
    "model switched: claude-cached to claude-sonnet-4-5":
        lambda b: {**b, "model": "claude-sonnet-4-5"},
    "earlier history changed":  # an old turn's text dropped, as the April cache bug did
        lambda b: {**b, "messages": [b["messages"][0], {**b["messages"][1], "content":
                                     b["messages"][1]["content"][1:]}, *b["messages"][2:]]},
    "prefix unchanged: the vendor missed the cache": lambda b: b,
}


@pytest.mark.parametrize("reason", BREAKS)
def test_each_cache_break_is_named(gw, reason):
    port, ledger = gw
    assert send(port, "t", body(1), usage(0, 20000))[0] == 200
    assert send(port, "t", BREAKS[reason](body(2)), usage(0, 21000))[0] == 200
    first, second = calls(ledger, "t")
    assert first["cache_break"] is None
    assert second["cache_break"] == reason
    if reason.startswith("prefix"):  # 20,000 tokens written again instead of read
        actual = CACHED.cost(21005, 300, cache_write=21000)
        stable = CACHED.cost(21005, 300, cache_read=20000, cache_write=1000)
        assert second["break_cost"] == actual - stable == 20000 * (3.75 - 0.3)


def test_a_call_that_reads_the_cache_is_no_break(gw):
    port, ledger = gw
    send(port, "t", body(1), usage(0, 20000))
    send(port, "t", body(2), usage(20000, 1000))
    assert [c["cache_break"] for c in calls(ledger, "t")] == [None, None]


def test_no_cache_marker(gw):
    """A client that sends no marker and asks the gateway not to add one."""
    port, ledger = gw
    off = {**API_KEY, "x-quarter-cache": "off"}
    send(port, "t", body(1, marked=False), {**ANTHROPIC_USAGE, "input_tokens": 20000}, auth=off)
    send(port, "t", body(2, marked=False), {**ANTHROPIC_USAGE, "input_tokens": 21000}, auth=off)
    assert calls(ledger, "t")[1]["cache_break"] == "no cache marker"


@pytest.mark.parametrize(("ttl", "idle", "reason"), [
    (None, 6, "idle 6 minutes, past the 5-minute cache lifetime"),
    ("1h", 6, "prefix unchanged: the vendor missed the cache"),
    ("1h", 61, "idle 61 minutes, past the 60-minute cache lifetime"),
])
def test_gap_longer_than_the_cache_lifetime(gw, ttl, idle, reason):
    port, ledger = gw
    send(port, "t", body(1, system=system(ttl=ttl)), usage(0, 20000, ttl=ttl or "5m"))
    earlier = (datetime.now(UTC) - timedelta(minutes=idle, seconds=5)).isoformat()
    ledger.db.execute("update calls set at = ?", (earlier,))
    send(port, "t", body(2, system=system(ttl=ttl)), usage(0, 21000, ttl=ttl or "5m"))
    assert calls(ledger, "t")[1]["cache_break"] == reason


def test_each_subagent_is_its_own_lane(gw):
    """A subagent's first call has another prompt: it is no break in the main thread's cache."""
    port, ledger = gw
    send(port, "t", body(1), usage(0, 20000), request_class="main")
    other = {**body(1), "system": system(today="x"), "tools": TOOLS[:2]}
    send(port, "t", other, usage(0, 8000), agent_id="agent_7", request_class="subagent")
    send(port, "t", body(2), usage(20000, 1000), request_class="main")
    assert [c["cache_break"] for c in calls(ledger, "t")] == [None, None, None]


def test_why_adds_up_to_what_was_charged(gw, tmp_path, capsys):
    port, ledger = gw
    turns = [
        (body(1), usage(0, 20000), {"prompt_id": "p1"}),
        (body(2), usage(20000, 1000), {"prompt_id": "p1"}),
        ({**body(1), "tools": TOOLS[:2]}, usage(0, 9000),
         {"prompt_id": "p1", "agent_id": "agent_7", "parent_agent_id": "main"}),
        ({**body(3), "stream": True}, usage(0, 22000), {"prompt_id": "p2"}),  # break: missed
        (body(4), usage(22000, 900), {"prompt_id": "p2"}),
    ]
    for request, used, hints in turns:
        assert send(port, "t", request, used, **hints)[0] == 200
    report = forensics.why(ledger, "t")
    total = charged(ledger, "t")
    assert report["calls"] == 5 and report["charged_micros"] == total == report["list_micros"]
    assert sum(report["parts"].values()) == total
    assert report["parts"]["output_micros"] == 5 * 300 * 15  # 300 output tokens at $15/M
    assert sum(r["micros"] for r in report["tools_by_server"]) == report["parts"]["tools_micros"]
    assert {r["server"] for r in report["tools_by_server"]} == {"builtin", "github"}
    for group in ("by_subagent", "by_prompt"):
        assert sum(r["charged_micros"] for r in report[group]) == total
    assert [(r["subagent"], r["calls"]) for r in report["by_subagent"]] == [("main", 4),
                                                                          ("agent_7", 1)]
    [miss] = report["breaks"]
    assert (miss["turn"], miss["reason"]) == (4, "prefix unchanged: the vendor missed the cache")
    assert report["cache_miss_micros"] == miss["micros"] > 0

    # The same through the CLI, the dashboard route and the MCP tool.
    db = str(tmp_path / "ledger.db")
    main(["--db", db, "why", "t"])
    out = capsys.readouterr().out
    assert f"Task t: 5 calls, ${total / 1e6:.2f} charged" in out
    assert "  turn 4    main" in out and "github" in out
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/tasks/t/why", headers={"x-quarter-key": ADMIN})
    resp = conn.getresponse()
    assert resp.status == 200 and json.loads(resp.read()) == report
    conn.request("GET", "/tasks/t/why")
    assert conn.getresponse().status == 401
    reply = handle(ledger, [], {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "why", "arguments": {"task": "t"}}})
    data = reply["result"]["structuredContent"]
    assert data["calls"] == 5 and data["breaks"][0]["usd"] == f"{miss['micros'] / 1e6:.2f}"


def test_no_prompt_text_is_stored(gw):
    port, ledger = gw
    send(port, "t", body(1), usage(0, 20000))
    dump = "\n".join(ledger.db.iterdump())
    assert "Fix the failing test" not in dump and "Working directory" not in dump
    assert "ls src/0" not in dump and "Create a GitHub issue" not in dump
    fp = json.loads(calls(ledger, "t")[0]["fingerprint"])
    assert list(fp["tools"]) == ["builtin", "github"] and fp["tools"]["github"]["count"] == 2
    assert fp["markers"] == ["system[1]", "history[2]"] and fp["history"]["count"] == 3


def test_subscription_calls_cost_nothing_and_show_their_list_cost(gw):
    port, ledger = gw
    send(port, "t", body(1), usage(0, 20000), auth=LOGIN)
    send(port, "t", body(2), usage(20000, 1000), auth=LOGIN)
    worth = CACHED.cost(20005, 300, cache_write=20000) + CACHED.cost(
        21005, 300, cache_read=20000, cache_write=1000)
    report = forensics.why(ledger, "t")
    assert charged(ledger, "t") == 0 == report["charged_micros"]
    assert report["list_micros"] == worth and report["subscription_calls"] == 2
    assert sum(report["parts"].values()) == worth
    assert {c["route"] for c in calls(ledger, "t")} == {"subscription"}


def looping(turns, result=lambda n: "1 failed"):
    """Every turn the model runs the same command again and gets the same answer: the body
    grows, the call and its result repeat."""
    return body(turns, messages=history(turns, command=lambda n: "npm test", result=result))


def test_a_repeated_tool_call_pauses_only_its_task(gw):
    port, ledger = gw
    for turns in range(1, 8):
        for _ in range(2):  # a retried request carries the same tool call id: counted once
            assert send(port, "loop", looping(turns), usage(0, 2000))[0] == 200
        assert send(port, "other", body(turns), usage(0, 2000))[0] == 200
    status, reply = send(port, "loop", looping(8), usage(0, 2000))
    assert status == 423 and json.loads(reply)["error"] == "task_paused"
    assert "same tool call 8 times in 5 minutes, with the same result" in json.loads(reply)["reason"]
    assert send(port, "other", body(8), usage(0, 2000))[0] == 200  # the agent still works
    assert ledger.paused("a1") is None and ledger.task_paused("loop")
    assert any(r["detail"].startswith("task loop of a1:") for r in ledger.audit_log())

    admin = {"x-quarter-key": ADMIN}
    assert post(port, "/tasks/loop/resume", {}, admin)[0] == 200
    assert send(port, "loop", looping(9), usage(0, 2000))[0] == 200  # counting starts afresh
    assert ledger.task_paused("loop") is None


def test_a_call_repeated_while_its_result_changes_is_work_not_a_loop(gw):
    port, ledger = gw
    progress = lambda n: f"{20 - n} failed"
    for turns in range(1, 24):  # tests rerun after each edit: fewer fail each time
        assert send(port, "work", looping(turns, progress), usage(0, 2000))[0] == 200
    assert ledger.task_paused("work") is None
    status, reply = send(port, "work", looping(24, progress), usage(0, 2000))
    assert status == 423 and "same tool call 24 times in 5 minutes" in json.loads(reply)["reason"]


def test_openai_tool_calls_are_read_too():
    def chat(turn_id):
        return {"messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": turn_id, "type": "function", "function": {
                "name": "read", "arguments": '{"path": "a.py"}'}}]},
            {"role": "tool", "tool_call_id": turn_id, "content": "x"}]}
    responses = {"input": [
        {"role": "user", "content": "go"},
        {"type": "function_call", "call_id": "c1", "name": "read", "arguments": '{"path":"a.py"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "x"}]}
    [(a, same, out_a)] = forensics.tool_calls(chat("call_1"))
    [(b, again, out_b)] = forensics.tool_calls(chat("call_2"))
    [(c, resp, out_c)] = forensics.tool_calls(responses)
    assert (a, b, c) == ("call_1", "call_2", "c1") and same == again == resp
    assert out_a == out_b == out_c == forensics.digest("x")


def test_a_cache_collapse_alerts_once_per_task(gw):
    port, ledger = gw
    send(port, "t", body(1), usage(0, 20000))
    for turns in range(2, 5):
        send(port, "t", body(turns), usage(20000, 1000))
    for turns in range(5, 9):  # every call from here on writes the whole prompt again
        send(port, "t", body(turns, system=system(today=str(turns))), usage(0, 21000))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not FakeSlack.calls:
        time.sleep(0.02)
    time.sleep(0.1)
    posted = [c for c in FakeSlack.calls if c[0] == "chat.postMessage"]
    assert len(posted) == 1 and posted[0][2]["text"].startswith("Cache collapse: task t read 0%")
    assert [r["action"] for r in ledger.audit_log()].count("cache_collapse") == 1


def test_old_database_opens_and_gains_task_pauses(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript(
        "create table pauses (id integer primary key, agent text not null, at text not null,"
        " reason text not null, resumed_at text, resumed_by text);"
        "insert into pauses (agent, at, reason) values ('bot', '2026-10-01', 'loop');"
    )
    db.commit()
    db.close()
    ledger = Ledger(path)
    assert ledger.paused("bot") == "loop"
    assert ledger.pause("helper", "same tool call", task="t1")
    assert ledger.paused("helper") is None and ledger.task_paused("t1") == "same tool call"
    decision, _ = ledger.hold([], {"agent": "anyone", "task": "t1"}, "anthropic", "m", 1)
    assert decision.outcome == "block" and decision.reason.startswith("task paused")
    assert {(p["agent"], p["task"]) for p in ledger.paused_agents()} == {
        ("bot", None), ("helper", "t1")}
    assert ledger.resume("bot", "cli") and ledger.task_paused("t1")  # an agent resume is not a task's
    assert forensics.why(ledger, "t1")["calls"] == 0
