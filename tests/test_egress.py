"""Egress: agents act on APIs through the gateway without holding the real credential,
within rules, and destructive calls wait for a person."""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from test_hardening import Elsewhere, raw_exchange
from test_proxy import ADMIN_KEY, PRICES, call, start

from quarter_ledger import cli, receipts, status, underwriting
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, approval_text, serve
from quarter_ledger.rules import load_rules
from quarter_ledger.store import Ledger

DEPLOYER, READER, SHARED = "deployer-key-0123456789", "reader-key-0123456789", "shared-key-0123456789"
RULES = [
    {"upstream": "railway", "default": "deny"},
    {"upstream": "railway", "method": ["POST"], "path": "/graphql/v2",
     "graphql_operation": "*Delete", "action": "approve"},
    {"upstream": "railway", "method": ["POST"], "path": "/graphql/v2", "action": "allow"},
    {"upstream": "github", "method": ["DELETE"], "path": "/repos/*", "action": "deny"},
    {"agent": "reader", "upstream": "github", "method": ["PUT", "PATCH"], "action": "deny"},
]
VOLUME_DELETE = {
    "query": 'mutation volumeDelete($id: String!) { volumeDelete(volumeId: $id) }',
    "variables": {"id": "vol-prod"},
}


class FakeApi(BaseHTTPRequestHandler):
    """Railway, GitHub and a model vendor in one: records each call, answers `reply`
    (a str is sent as a stream)."""

    protocol_version = "HTTP/1.1"
    calls: ClassVar[list] = []
    reply: ClassVar[dict | str] = {"ok": True}

    def answer(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        FakeApi.calls.append((self.command, self.path, body, self.headers.get("authorization")))
        stream = isinstance(FakeApi.reply, str)
        out = FakeApi.reply.encode() if stream else json.dumps(FakeApi.reply).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream" if stream else "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer

    def log_message(self, *args):
        pass


@pytest.fixture
def egress(tmp_path):
    api = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
    FakeApi.calls, FakeApi.reply = [], {"ok": True}
    ledger = Ledger(str(tmp_path / "ledger.db"))
    base = f"http://127.0.0.1:{api.server_port}"
    gw = Gateway(
        ledger, [], PRICES, {"railway": base, "github": base, "fake": base},
        {DEPLOYER, READER, SHARED}, ADMIN_KEY,
        key_agents={DEPLOYER: "deployer", READER: "reader"},
        vendor_keys={"railway": "rw-team", "railway:deployer": "rw-deployer",
                     "github": "gh-real", "fake": "sk-real"},
        rules=load_rules(RULES),
    )
    server = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{server.server_port}", ledger
    server.shutdown()
    api.shutdown()


def as_agent(key: str, **extra) -> dict:
    """An agent that holds only its Quarter key, sent where the vendor key goes."""
    return {"authorization": f"Bearer {key}", "content-type": "application/json", **extra}


def upstream_calls(ledger: Ledger) -> list[dict]:
    rows = ledger.db.execute("select detail from audit where action = 'upstream_call' order by id")
    return [json.loads(d) for (d,) in rows]


def approve_when_asked(url: str, ledger: Ledger, verb: str = "approve", by: str = "Sam"):
    """A person deciding the first approval that appears, from the dashboard's API."""

    def decide():
        for _ in range(100):
            waiting = ledger.approvals()
            if waiting:
                headers = {"x-quarter-key": ADMIN_KEY, "x-quarter-approver": by}
                call(f"{url}/approvals/{waiting[0]['id']}/{verb}", {}, headers)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=decide)
    thread.start()
    return thread


# Rules on calls


def test_graphql_delete_waits_for_a_person_then_goes_through(egress):
    url, ledger = egress
    person = approve_when_asked(url, ledger)
    status, body = call(url + "/railway/graphql/v2", VOLUME_DELETE,
                        as_agent(DEPLOYER, **{"x-quarter-wait": "10"}))
    person.join()
    assert status == 200 and json.loads(body) == {"ok": True}
    assert [(m, p) for m, p, *_ in FakeApi.calls] == [("POST", "/graphql/v2")]
    assert FakeApi.calls[0][3] == "Bearer rw-deployer"
    (approval,) = ledger.db.execute("select status, decided_by, model, estimate from approvals")
    assert approval == ("used", "Sam", "POST /graphql/v2 volumeDelete", 0)
    (row,) = upstream_calls(ledger)
    assert row["decision"] == "approved" and row["decided_by"] == "Sam"


def test_graphql_delete_without_wait_needs_an_approval_that_covers_only_that_call(egress):
    url, ledger = egress
    status, body = call(url + "/railway/graphql/v2", VOLUME_DELETE, as_agent(DEPLOYER))
    reply = json.loads(body)
    assert status == 403 and reply["error"] == "approval_required"
    assert "rule 2" in reply["reason"] and FakeApi.calls == []
    ledger.decide_approval(reply["approval_id"], True, by="Sam")

    other = {"query": "mutation { projectDelete(id: 1) }"}
    retry = as_agent(DEPLOYER, **{"x-quarter-approval": reply["approval_id"]})
    assert call(url + "/railway/graphql/v2", other, retry)[0] == 403  # another action
    assert call(url + "/railway/graphql/v2", VOLUME_DELETE, retry)[0] == 200
    assert call(url + "/railway/graphql/v2", VOLUME_DELETE, retry)[0] == 403  # used once
    assert len(FakeApi.calls) == 1


def test_a_denied_approval_never_reaches_the_upstream(egress):
    url, ledger = egress
    person = approve_when_asked(url, ledger, "deny")
    status, body = call(url + "/railway/graphql/v2", VOLUME_DELETE,
                        as_agent(DEPLOYER, **{"x-quarter-wait": "10"}))
    person.join()
    assert status == 403 and json.loads(body)["error"] == "approval_denied"
    assert FakeApi.calls == []
    assert upstream_calls(ledger)[-1]["decision"] == "denied"


def test_graphql_names_cannot_hide_a_delete(egress):
    url, _ = egress
    hidden = [
        {"operationName": "listProjects", "query": "mutation listProjects { volumeDelete(volumeId: 1) }"},
        {"query": "mutation { me: projects { id } gone: volumeDelete(volumeId: 1) }"},
        {"query": "mutation { ...Gone } fragment Gone on Mutation { volumeDelete(volumeId: 1) }"},
        [{"query": "{ me { id } }"}, VOLUME_DELETE],  # a batch
        {"query": "this is not GraphQL"},  # unreadable: a person decides
    ]
    for body in hidden:
        status, reply = call(url + "/railway/graphql/v2", body, as_agent(DEPLOYER))
        assert status == 403 and json.loads(reply)["error"] == "approval_required", body
    form = {"content-type": "application/x-www-form-urlencoded"}
    smuggled = [  # (body, extra headers, query string)
        (b"query=mutation+%7B+volumeDelete%28volumeId%3A+1%29+%7D", form, ""),
        (b"{}", {}, "?query=mutation+%7B+volumeDelete%28volumeId%3A+1%29+%7D"),
        (b"--b\r\ncontent-disposition: form-data; name=operations\r\n\r\n{}\r\n--b--",
         {"content-type": "multipart/form-data; boundary=b"}, ""),
    ]
    for raw, extra, query in smuggled:
        req = urllib.request.Request(url + "/railway/graphql/v2" + query, raw, as_agent(DEPLOYER, **extra))
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req)
        assert json.loads(refused.value.read())["error"] == "approval_required", raw
    assert FakeApi.calls == []
    query = {"query": "query { projects { edges { node { id } } } }"}
    assert call(url + "/railway/graphql/v2", query, as_agent(DEPLOYER))[0] == 200


def test_upstream_marked_deny_refuses_what_no_rule_allows(egress):
    url, _ = egress
    status, body = call(url + "/railway/v1/anything", headers=as_agent(DEPLOYER))
    assert status == 403
    assert json.loads(body) == {"error": "rule_denied", "rule": "no rule matched; railway defaults to deny"}
    assert FakeApi.calls == []


def test_rest_delete_is_denied_with_the_rule(egress):
    url, ledger = egress
    status, body = call(url + "/github/repos/acme/app", headers=as_agent(DEPLOYER), method="DELETE")
    assert status == 403
    assert json.loads(body) == {"error": "rule_denied", "rule": "rule 4: deny github DELETE /repos/*"}
    assert FakeApi.calls == []
    (row,) = upstream_calls(ledger)
    assert row["decision"] == "denied" and row["method"] == "DELETE"


def test_rule_matches_the_decoded_path_whatever_its_case(egress):
    url, _ = egress
    for path in ("/github/Repos/acme/app", "/github/repos%2Facme%2Fapp"):
        assert call(url + path, headers=as_agent(DEPLOYER), method="DELETE")[0] == 403
    assert FakeApi.calls == []


def test_rules_per_agent(egress):
    url, _ = egress
    patch = {"name": "renamed"}
    assert call(url + "/github/repos/acme/app", patch, as_agent(READER), "PATCH")[0] == 403
    assert call(url + "/github/repos/acme/app", patch, as_agent(DEPLOYER), "PATCH")[0] == 200
    assert FakeApi.calls == [("PATCH", "/repos/acme/app", b'{"name": "renamed"}', "Bearer gh-real")]


def test_get_is_allowed_unpriced_and_audited(egress):
    url, ledger = egress
    status, body = call(url + "/github/repos/acme/app?per_page=5", headers=as_agent(READER))
    assert status == 200 and json.loads(body) == {"ok": True}
    assert FakeApi.calls == [("GET", "/repos/acme/app?per_page=5", b"", "Bearer gh-real")]
    assert ledger.db.execute("select count(*) from entries").fetchone()[0] == 0
    assert upstream_calls(ledger) == [{
        "agent": "reader", "task": None, "upstream": "github", "method": "GET",
        "path": "/repos/acme/app", "decision": "allowed",
    }]


def test_agent_never_sees_the_real_key(egress):
    url, ledger = egress
    body = {"title": "token=s3cret-in-body"}
    for method in ("GET", "POST", "PUT", "PATCH"):
        data = None if method == "GET" else body
        status, reply = call(url + "/github/repos/acme/app/issues", data, as_agent(DEPLOYER), method)
        assert status == 200 and b"gh-real" not in reply
    assert {auth for *_, auth in FakeApi.calls} == {"Bearer gh-real"}
    log = json.dumps(ledger.audit_log())
    for secret in ("gh-real", DEPLOYER, "s3cret-in-body"):
        assert secret not in log


def test_each_named_agent_gets_its_own_key_and_others_the_upstreams(egress):
    url, _ = egress
    query = {"query": "{ me { id } }"}
    call(url + "/railway/graphql/v2", query, as_agent(DEPLOYER))
    call(url + "/railway/graphql/v2", query, as_agent(READER))
    # An unnamed key may call itself anything, so it never gets a named agent's key.
    call(url + "/railway/graphql/v2", query, as_agent(SHARED, **{"x-quarter-agent": "deployer"}))
    assert [auth for *_, auth in FakeApi.calls] == ["Bearer rw-deployer", "Bearer rw-team", "Bearer rw-team"]


def test_paused_agent_cannot_act_on_an_upstream(egress):
    url, ledger = egress
    ledger.pause("deployer", "spent too fast")
    status, body = call(url + "/github/repos/acme/app", headers=as_agent(DEPLOYER))
    assert status == 423 and json.loads(body)["error"] == "agent_paused"
    assert FakeApi.calls == []


def test_a_task_paused_for_looping_cannot_act_on_an_upstream(egress):
    url, ledger = egress
    ledger.pause("deployer", "made the same tool call 8 times in 5 minutes", task="t1")
    status, body = call(url + "/github/repos/acme/app", headers=as_agent(DEPLOYER, **{"x-quarter-task": "t1"}))
    assert status == 423 and json.loads(body)["error"] == "task_paused"
    assert FakeApi.calls == []
    status, _ = call(url + "/github/repos/acme/app", headers=as_agent(DEPLOYER, **{"x-quarter-task": "t2"}))
    assert status == 200


def test_model_calls_are_still_priced(egress):
    url, ledger = egress
    FakeApi.reply = {"ok": True, "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
    assert call(url + "/fake/v1/chat", {"model": "m"}, as_agent(DEPLOYER))[0] == 200
    assert ledger.totals("agent") == [("deployer", "fake", 2000)]
    assert upstream_calls(ledger) == []  # in the ledger, not the action log


def test_underwriting_statement_counts_the_risk_and_names_no_one(egress, tmp_path, monkeypatch):
    url, ledger = egress
    FakeApi.reply = {"ok": True, "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
    call(url + "/fake/v1/chat", {"model": "m"}, as_agent(DEPLOYER, **{"x-quarter-task": "t1"}))
    FakeApi.reply = {"ok": True}
    call(url + "/github/repos/acme/app", headers=as_agent(READER))
    call(url + "/github/repos/acme/app", headers=as_agent(DEPLOYER), method="DELETE")
    ledger.set_owner("deployer", "Dana")
    ledger.pause("deployer", "made the same tool call 8 times in 5 minutes", task="t1")
    caps = [Cap(scope="customer", key="acme-corp", window="month", limit=10**8, approve_above=10**6)]
    month = datetime.now(UTC).strftime("%Y-%m")

    s = underwriting.statement(ledger, month, caps, load_rules(RULES).listed())
    assert s["schema"] == "quarter.underwriting/1"
    assert s["exposure"]["agents"] == 2 and s["exposure"]["tasks"] == 1
    assert s["exposure"]["cost_micros"] == sum(m["cost_micros"] for m in s["exposure"]["by_model"])
    assert s["authority"]["agents_with_owner"] == 1
    assert s["authority"]["caps"] == [{"scope": "customer", "every": False, "window": "month",
                                       "limit_micros": 10**8, "approve_above_micros": 10**6}]
    assert s["high_impact"]["writes"] == 1
    assert {"upstream": "github", "method": "DELETE", "decision": "denied", "count": 1} in s["high_impact"]["upstream_calls"]
    assert s["containment"]["loop_pauses"] == 1
    assert s["integrity"]["chain"]["entries"]["intact"] and s["integrity"]["heads"]["entries"]["hash"]
    assert s["exposure"]["calls"] == 1 and s["exposure"]["by_model"][0]["input"] == 1000
    text = json.dumps(s)
    assert "acme-corp" not in text and "Dana" not in text and "/repos/acme" not in text

    key = "ed25519:" + "07" * 32
    monkeypatch.setenv("QUARTER_SIGNING_KEY", key)
    out = tmp_path / "statement.json"
    cli.main(["--db", str(tmp_path / "ledger.db"), "underwriting", "--month", month, "--out", str(out)])
    written = json.loads(out.read_text())
    claims = receipts.verify(written["signed"], receipts.public_key(key))
    assert claims["sub"] == "quarter.underwriting/1"
    assert claims["exposure"] == written["statement"]["exposure"]


# The hardening on the new methods


def test_new_methods_keep_every_hardening_check(egress):
    url, _ = egress
    port = int(url.rsplit(":", 1)[1])
    head = f"DELETE /github/x HTTP/1.1\r\nhost: x\r\nauthorization: Bearer {DEPLOYER}\r\n"
    chunked = raw_exchange(port, (head + "transfer-encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n").encode())
    assert chunked.startswith(b"HTTP/1.1 411")
    huge = raw_exchange(port, (head + f"content-length: {9 * 1024 * 1024}\r\n\r\n").encode())
    assert huge.startswith(b"HTTP/1.1 413")
    for method in ("PUT", "PATCH", "DELETE"):
        assert call(url + "/github/%2e%2e/admin", {}, as_agent(DEPLOYER), method)[0] == 400
        assert call(url + "/github/x", {}, {"content-type": "application/json"}, method)[0] == 401
        other = as_agent(DEPLOYER, **{"x-quarter-agent": "reader"})
        assert call(url + "/github/x", {}, other, method)[0] == 403
        assert call(url + "/approvals/x", {}, as_agent(DEPLOYER), method)[0] == 404
    assert FakeApi.calls == []


def test_redirect_on_a_delete_is_relayed_not_followed(tmp_path):
    elsewhere = start(ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere))
    Elsewhere.seen = []

    class Redirecting(BaseHTTPRequestHandler):
        def do_DELETE(self):
            self.send_response(307)  # a 307 would be followed with the same method and headers
            self.send_header("location", f"http://127.0.0.1:{elsewhere.server_port}/steal")
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    api = start(ThreadingHTTPServer(("127.0.0.1", 0), Redirecting))
    gw = Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES,
                 {"github": f"http://127.0.0.1:{api.server_port}"}, {DEPLOYER},
                 vendor_keys={"github": "gh-real"})
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert call(url + "/github/repos/a/b", headers=as_agent(DEPLOYER), method="DELETE")[0] == 307
        assert Elsewhere.seen == []
    finally:
        server.shutdown()
        api.shutdown()
        elsewhere.shutdown()


# Old configs


def test_no_rules_file_allows_every_method_as_before(tmp_path):
    api = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
    FakeApi.calls, FakeApi.reply = [], {"ok": True}
    gw = Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES,
                 {"github": f"http://127.0.0.1:{api.server_port}"}, {SHARED},
                 vendor_keys={"github": "gh-real"})
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert call(url + "/github/repos/a/b", headers=as_agent(SHARED), method="DELETE")[0] == 200
        assert FakeApi.calls == [("DELETE", "/repos/a/b", b"", "Bearer gh-real")]
    finally:
        server.shutdown()
        api.shutdown()


def test_vendor_keys_in_either_form(tmp_path):
    keys = cli.vendor_keys_from('railway=rw-team, railway:deployer=rw-deployer==')
    assert keys == {"railway": "rw-team", "railway:deployer": "rw-deployer=="}
    assert cli.vendor_keys_from('{"local": "none"}') == {"local": "none"}
    path = tmp_path / "keys.json"
    path.write_text('{"github": "gh"}')
    assert cli.vendor_keys_from(str(path)) == {"github": "gh"}
    assert cli.vendor_keys_from(None) == {}


def test_bad_rules_are_refused_at_start():
    for bad, error in (
        ([{"upstream": "github", "action": "maybe"}], "action must be"),
        ([{"upstream": "github", "methods": ["GET"], "action": "deny"}], "unknown methods"),
        ([{"tool": "Bash", "command": "(", "action": "approve"}], "bad command pattern"),
        ([{"upstream": "github", "default": "approve"}], "a default is"),
    ):
        with pytest.raises(SystemExit, match=error):
            cli.rules_from(json.dumps(bad))
    with pytest.raises(ValueError, match="not set up"):
        Gateway(Ledger(":memory:"), [], PRICES, {"github": "https://api.github.com"},
                rules=load_rules([{"upstream": "githbu", "action": "deny"}]))


def test_rules_show_in_status_and_the_audit_pack(tmp_path, egress):
    import zipfile
    from datetime import UTC, datetime

    from quarter_ledger.evidence import audit_pack

    url, ledger = egress
    call(url + "/github/repos/acme/app", headers=as_agent(DEPLOYER), method="DELETE")
    shown = status.render(ledger, [], rules=load_rules(RULES).listed())
    assert "Rules: 5" in shown and "rule 4: deny github DELETE /repos/*" in shown
    out = str(tmp_path / "pack.zip")
    audit_pack(ledger, f"{datetime.now(UTC):%Y-%m}", out, rules=RULES)
    pack = zipfile.ZipFile(out)
    assert json.loads(pack.read("rules.json")) == RULES
    controls = pack.read("controls.md").decode()
    assert "### Egress" in controls and "rule 2: approve railway POST /graphql/v2 graphql *Delete" in controls
    assert "Upstream calls by decision: denied 1" in controls


def test_an_action_approval_reads_as_a_call_not_spend():
    text = approval_text("deployer", None, "railway", "POST /graphql/v2 volumeDelete", 0, "rule 2")
    assert text == "Approval needed: deployer wants to call railway POST /graphql/v2 volumeDelete. rule 2."


# Tool calls in model replies

TOOL_RULES = [
    {"tool": "Bash", "command": r"rm\s+-rf\s+/(\s|$)|drop\s+table|terraform\s+destroy", "action": "approve"},
    {"tool": "mcp__*__delete_*", "action": "deny"},
]
USAGE = {"input_tokens": 1000, "output_tokens": 500}


def anthropic_reply(*tools) -> dict:
    blocks = [{"type": "text", "text": "Cleaning up."}]
    blocks += [{"type": "tool_use", "id": f"t{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(tools)]
    return {"type": "message", "content": blocks, "stop_reason": "tool_use", "usage": USAGE}


@pytest.fixture
def held(tmp_path):
    api = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
    FakeApi.calls = []
    ledger = Ledger(str(tmp_path / "ledger.db"))
    base = f"http://127.0.0.1:{api.server_port}"
    gw = Gateway(ledger, [], PRICES, {"fake": base}, {DEPLOYER}, ADMIN_KEY,
                 key_agents={DEPLOYER: "deployer"}, vendor_keys={"fake": "sk-real"},
                 rules=load_rules(TOOL_RULES))
    server = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{server.server_port}", ledger
    server.shutdown()
    api.shutdown()


def tool_audit(ledger: Ledger) -> list[dict]:
    rows = ledger.db.execute("select detail from audit where action = 'tool_held' order by id")
    return [json.loads(d) for (d,) in rows]


def test_destructive_bash_call_waits_and_goes_through_once_approved(held):
    url, ledger = held
    FakeApi.reply = anthropic_reply(("Bash", {"command": "rm -rf / --no-preserve-root"}))
    person = approve_when_asked(url, ledger)
    status, body = call(url + "/fake/v1/messages", {"model": "m"}, as_agent(DEPLOYER, **{"x-quarter-wait": "10"}))
    person.join()
    assert status == 200 and json.loads(body) == FakeApi.reply
    assert tool_audit(ledger)[0]["decision"] == "approved"
    assert ledger.totals("agent") == [("deployer", "fake", 2000)]  # the model ran: charged


def test_denied_tool_call_is_taken_out_of_the_reply(held):
    url, ledger = held
    FakeApi.reply = anthropic_reply(("Bash", {"command": "psql -c 'DROP TABLE users'"}))
    person = approve_when_asked(url, ledger, "deny", by="Priya")
    status, body = call(url + "/fake/v1/messages", {"model": "m"}, as_agent(DEPLOYER, **{"x-quarter-wait": "10"}))
    person.join()
    reply = json.loads(body)
    assert status == 200 and reply["stop_reason"] == "end_turn"
    assert [b["type"] for b in reply["content"]] == ["text", "text"]
    assert "Priya denied it" in reply["content"][1]["text"]
    assert ledger.approvals("used") == [] and tool_audit(ledger)[0]["decision"] == "denied"
    assert "DROP TABLE" not in json.dumps(tool_audit(ledger))


def test_undecided_tool_call_is_held_and_an_approval_covers_the_same_call_on_retry(held):
    url, ledger = held
    FakeApi.reply = anthropic_reply(("Bash", {"command": "terraform destroy -auto-approve"}))
    status, body = call(url + "/fake/v1/messages", {"model": "m"}, as_agent(DEPLOYER))
    note = json.loads(body)["content"][-1]["text"]
    (waiting,) = ledger.approvals()
    assert status == 200 and waiting["id"] in note and "terraform destroy" in waiting["reason"]
    ledger.decide_approval(waiting["id"], True, by="Sam")
    retry = as_agent(DEPLOYER, **{"x-quarter-approval": waiting["id"]})
    assert json.loads(call(url + "/fake/v1/messages", {"model": "m"}, retry)[1]) == FakeApi.reply


def test_a_held_command_hides_what_looks_like_a_key(held):
    url, ledger = held
    command = "curl -X DELETE -H 'Authorization: Bearer abc' https://api.acme.dev/db; echo ghp_0123456789abcdefghijABCDEFGHIJ"
    FakeApi.reply = anthropic_reply(("Bash", {"command": "terraform destroy; " + command}))
    call(url + "/fake/v1/messages", {"model": "m"}, as_agent(DEPLOYER))
    (waiting,) = ledger.approvals()
    assert "terraform destroy" in waiting["reason"] and "Bearer [hidden]" in waiting["reason"]
    assert "abc'" not in waiting["reason"] and "ghp_" not in waiting["reason"]


def test_a_rule_that_denies_a_tool_strips_it_without_asking(held):
    url, ledger = held
    FakeApi.reply = {
        "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "mcp__railway__delete_volume", "arguments": '{"id": "v1"}'}},
            {"id": "c2", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
        ]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    reply = json.loads(call(url + "/fake/v1/chat/completions", {"model": "m"}, as_agent(DEPLOYER))[1])
    message = reply["choices"][0]["message"]
    assert [c["id"] for c in message["tool_calls"]] == ["c2"]
    assert "mcp__railway__delete_volume" in message["content"] and ledger.approvals() == []


def test_responses_api_tool_call_is_held_too(held):
    url, _ = held
    FakeApi.reply = {"output": [{"type": "function_call", "name": "Bash", "call_id": "c1",
                                 "arguments": json.dumps({"command": "rm -rf /"})}], "usage": USAGE}
    reply = json.loads(call(url + "/fake/v1/responses", {"model": "m"}, as_agent(DEPLOYER))[1])
    assert [item["type"] for item in reply["output"]] == ["message"]


def sse(*events) -> str:
    return "".join(f"event: {e.get('type', 'x')}\ndata: {json.dumps(e)}\n\n" for e in events)


def events_of(stream: bytes) -> list[dict]:
    out = []
    for message in stream.decode().split("\n\n"):
        data = [line[5:].strip() for line in message.splitlines() if line.startswith("data:")]
        if data and data[0] != "[DONE]":
            out.append(json.loads(data[0]))
    return out


def anthropic_stream(command: str) -> str:
    half = len(command) // 2
    return sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1000, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Cleaning up."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"command": "' + command[:half]}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": command[half:] + '"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path": "/"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 500}},
        {"type": "message_stop"},
    )


def test_harmless_tool_calls_and_streams_pass_unchanged(held):
    url, ledger = held
    FakeApi.reply = anthropic_reply(("Bash", {"command": "rm -rf /tmp/build"}), ("Read", {"path": "/"}))
    assert json.loads(call(url + "/fake/v1/messages", {"model": "m"}, as_agent(DEPLOYER))[1]) == FakeApi.reply
    FakeApi.reply = anthropic_stream("rm -rf /tmp/build")
    stream = call(url + "/fake/v1/messages", {"model": "m", "stream": True}, as_agent(DEPLOYER))[1]
    assert stream.decode() == FakeApi.reply
    assert ledger.approvals() == [] and tool_audit(ledger) == []


def test_a_streamed_tool_call_is_held_like_any_other(held):
    url, ledger = held
    FakeApi.reply = anthropic_stream("terraform destroy -auto-approve")
    stream = call(url + "/fake/v1/messages", {"model": "m", "stream": True}, as_agent(DEPLOYER))[1]
    events = events_of(stream)
    assert b"terraform" not in stream
    starts = [(e["index"], e["content_block"].get("name") or e["content_block"]["type"])
              for e in events if e["type"] == "content_block_start"]
    assert starts == [(0, "text"), (1, "Read"), (2, "text")]  # renumbered, so the SDK still reads it
    note = next(e["delta"]["text"] for e in events if e.get("index") == 2 and e["type"] == "content_block_delta")
    (waiting,) = ledger.approvals()
    assert waiting["id"] in note and "terraform destroy" in waiting["reason"]
    assert next(e for e in events if e["type"] == "message_delta")["delta"]["stop_reason"] == "tool_use"
    assert ledger.totals("agent") == [("deployer", "fake", 2000)]  # the model ran: charged

    ledger.decide_approval(waiting["id"], True, by="Sam")
    retry = as_agent(DEPLOYER, **{"x-quarter-approval": waiting["id"]})
    assert call(url + "/fake/v1/messages", {"model": "m", "stream": True}, retry)[1].decode() == FakeApi.reply


def test_a_streamed_chat_tool_call_a_rule_denies_is_taken_out(held):
    url, ledger = held
    chunk = lambda delta, finish=None: {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    call_part = lambda i, **fn: {"tool_calls": [{"index": i, "function": fn}]}
    FakeApi.reply = "".join(f"data: {json.dumps(c)}\n\n" for c in [
        chunk({"role": "assistant", "content": None}),
        chunk(call_part(0, name="mcp__railway__delete_volume", arguments='{"id": ')),
        chunk(call_part(0, arguments='"v1"}')),
        chunk(call_part(1, name="list_files", arguments="{}")),
        chunk({}, "tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    ]) + "data: [DONE]\n\n"
    stream = call(url + "/fake/v1/chat/completions", {"model": "m", "stream": True}, as_agent(DEPLOYER))[1]
    events = events_of(stream)
    calls = [t for e in events for c in e["choices"] for t in c["delta"].get("tool_calls", [])]
    assert [(t["index"], t["function"].get("name")) for t in calls] == [(0, "list_files")]
    last = next(c for e in events for c in e["choices"] if c["finish_reason"])
    assert last["finish_reason"] == "tool_calls" and "mcp__railway__delete_volume" in last["delta"]["content"]
    assert stream.endswith(b"data: [DONE]\n\n") and ledger.approvals() == []
    assert tool_audit(ledger)[0]["decision"] == "denied"


def test_a_streamed_responses_tool_call_is_held_too(held):
    url, _ = held
    item = {"type": "function_call", "name": "Bash", "call_id": "c1", "arguments": ""}
    args = json.dumps({"command": "rm -rf /"})
    FakeApi.reply = sse(
        {"type": "response.created", "response": {"output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": args},
        {"type": "response.output_item.done", "output_index": 0, "item": {**item, "arguments": args}},
        {"type": "response.completed", "response": {"output": [{**item, "arguments": args}], "usage": USAGE}},
    )
    events = events_of(call(url + "/fake/v1/responses", {"model": "m", "stream": True}, as_agent(DEPLOYER))[1])
    assert not any((e.get("item") or {}).get("type") == "function_call" for e in events)
    done = events[-1]["response"]["output"]
    assert [i["type"] for i in done] == ["message"] and "Bash" in done[0]["content"][0]["text"]
    assert [e["output_index"] for e in events if "output_index" in e] == [0, 0]


# APIs with their own auth header


class HeaderApi(FakeApi):
    """Records every header it receives."""

    seen: ClassVar[list] = []

    def answer(self):
        HeaderApi.seen.append({k.lower(): v for k, v in self.headers.items()})
        super().answer()

    do_GET = do_POST = answer


def test_an_api_with_its_own_auth_header_gets_the_real_key_there(tmp_path):
    api = start(ThreadingHTTPServer(("127.0.0.1", 0), HeaderApi))
    HeaderApi.seen, FakeApi.calls = [], []
    base = f"http://127.0.0.1:{api.server_port}"
    ledger = Ledger(str(tmp_path / "l.db"))
    upstreams = {
        "railway": {"url": base, "auth": "Project-Access-Token"},
        "github": {"url": base, "headers": ["X-GitHub-Api-Version"]},
    }
    gw = Gateway(ledger, [], PRICES, upstreams, {DEPLOYER}, ADMIN_KEY,
                 key_agents={DEPLOYER: "deployer"},
                 vendor_keys={"railway": "rw-project", "github": "gh-real"})
    assert upstreams["railway"] == {"url": base, "auth": "Project-Access-Token"}  # not rewritten
    server = start(serve(gw, port=0))
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        headers = {"Project-Access-Token": DEPLOYER, "content-type": "application/json"}
        assert call(f"{url}/railway/graphql/v2", {"query": "{ me { id } }"}, headers)[0] == 200
        sent = HeaderApi.seen[-1]
        assert sent["project-access-token"] == "rw-project" and "authorization" not in sent

        headers = as_agent(DEPLOYER, **{"X-GitHub-Api-Version": "2022-11-28", "X-Other": "no"})
        assert call(f"{url}/github/user", None, headers)[0] == 200
        sent = HeaderApi.seen[-1]
        assert sent["authorization"] == "Bearer gh-real"
        assert sent["x-github-api-version"] == "2022-11-28" and "x-other" not in sent
        assert DEPLOYER not in json.dumps(HeaderApi.seen)  # the agent's key never leaves
    finally:
        server.shutdown()
        api.shutdown()


@pytest.mark.parametrize("bad", [
    {"url": "https://x", "headers": ["cookie"]},
    {"url": "https://x", "auth": "x-quarter-key"},
    {"url": "https://x", "secret": "y"},
])
def test_an_upstream_cannot_forward_connection_or_quarter_headers(tmp_path, bad):
    with pytest.raises(ValueError):
        Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {"api": bad}, set(), ADMIN_KEY)
