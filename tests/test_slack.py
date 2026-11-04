import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, FakeVendor, call, start

from quarter_ledger import cli
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.slack import Slack
from quarter_ledger.store import Ledger

SECRET = "signing-secret"
BIG = {"model": "m", "max_tokens": 12_000}  # about $0.024, over the $0.02 approval line


class FakeSlack(BaseHTTPRequestHandler):
    """Records Web API calls and answers ok, like slack.com/api would."""

    protocol_version = "HTTP/1.1"
    calls: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        # Per server: a late call from an earlier test's server cannot land in this test.
        self.server.calls.append((self.path.rsplit("/", 1)[1], self.headers["authorization"], body))
        reply = json.dumps({"ok": True, "channel": "C1", "ts": "1.2"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def slack_gateway(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    fake_slack = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeSlack))
    fake_slack.calls = []
    FakeVendor.calls, FakeSlack.calls = [], fake_slack.calls
    FakeSlack.url = f"http://127.0.0.1:{fake_slack.server_port}"
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.set_owner("a1", "Dana")
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.02"))]
    slack = Slack(
        "xoxb-test", SECRET, "C1", {"U_SAM": "Sam", "U_DANA": "Dana"},
        api=f"http://127.0.0.1:{fake_slack.server_port}",
    )
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(ledger, caps, PRICES, {"fake": base}, {AGENT_KEY}, ADMIN_KEY, slack=slack)
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", ledger
    for server in (proxy, vendor, fake_slack):
        server.shutdown()


def slack_calls(method, count=1):
    """The fake Slack's calls to one method, once that many have arrived."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = [c for c in FakeSlack.calls if c[0] == method]
        if len(found) >= count:
            return found
        time.sleep(0.02)
    raise AssertionError(f"slack {method} not called; got {FakeSlack.calls}")


def click(url, approval_id, user, action="approve", secret=SECRET, age=0):
    payload = {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": "C1"},
        "message": {"ts": "1.2"},
        "actions": [{"action_id": action, "value": approval_id}],
    }
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()) - age)
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-slack-request-timestamp": ts,
        "x-slack-signature": sig,
    }
    req = urllib.request.Request(url + "/slack/actions", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def pending(url):
    status, body = call(url + "/fake/v1/chat", BIG)
    assert status == 403
    return json.loads(body)["approval_id"]


def test_approval_is_posted_with_buttons(slack_gateway):
    url, _ = slack_gateway
    approval = pending(url)
    (_, auth, body), = slack_calls("chat.postMessage")
    assert auth == "Bearer xoxb-test" and body["channel"] == "C1"
    buttons = body["blocks"][1]["elements"]
    assert [(b["action_id"], b["value"]) for b in buttons] == [
        ("approve", approval), ("deny", approval)
    ]


def test_signature_must_be_valid_and_fresh(slack_gateway):
    url, ledger = slack_gateway
    approval = pending(url)
    assert click(url, approval, "U_SAM", secret="wrong") == 401
    assert click(url, approval, "U_SAM", age=301) == 401
    assert ledger.approval(approval)["status"] == "pending"
    assert click(url, approval, "U_SAM") == 200
    assert ledger.approval(approval)["decided_by"] == "Sam"
    (_, _, update), = slack_calls("chat.update")
    assert update["ts"] == "1.2" and "Approved by Sam." in update["text"]


def test_unknown_slack_user_is_refused(slack_gateway):
    url, ledger = slack_gateway
    approval = pending(url)
    assert click(url, approval, "U_STRANGER") == 200  # Slack wants a 200; the click does nothing
    assert ledger.approval(approval)["status"] == "pending"
    (_, _, note), = slack_calls("chat.postEphemeral")
    assert note["user"] == "U_STRANGER"
    assert any(a["action"] == "slack_refused" for a in ledger.audit_log())


def test_owner_cannot_approve_own_agent_from_slack(slack_gateway):
    url, ledger = slack_gateway
    approval = pending(url)
    click(url, approval, "U_DANA")
    assert ledger.approval(approval)["status"] == "pending"
    assert "cannot approve" in slack_calls("chat.postEphemeral")[0][2]["text"]
    click(url, approval, "U_DANA", action="deny")  # an owner may still deny
    assert ledger.approval(approval)["status"] == "denied"


def waiting_call(url, seconds):
    result = {}

    def run():
        result["reply"] = call(url + "/fake/v1/chat", BIG, {**HEADERS, "x-quarter-wait": seconds})

    thread = threading.Thread(target=run)
    thread.start()
    (_, _, posted), = slack_calls("chat.postMessage")
    return thread, result, posted["blocks"][1]["elements"][0]["value"]


def test_approve_releases_a_waiting_call(slack_gateway):
    url, ledger = slack_gateway
    thread, result, approval = waiting_call(url, "30")
    # The gateway keeps serving, and the ledger takes writes, while the call waits.
    assert call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 10})[0] == 200
    assert FakeVendor.calls and thread.is_alive()
    assert click(url, approval, "U_SAM") == 200
    thread.join(5)
    status, body = result["reply"]
    assert status == 200 and json.loads(body)["ok"]
    assert len(FakeVendor.calls) == 2
    assert ledger.approval(approval)["status"] == "used"


def test_deny_returns_the_refusal(slack_gateway):
    url, _ = slack_gateway
    thread, result, approval = waiting_call(url, "30")
    click(url, approval, "U_SAM", action="deny")
    thread.join(5)
    status, body = result["reply"]
    assert status == 403
    assert json.loads(body) == {
        "error": "approval_denied", "approval_id": approval, "decided_by": "Sam"
    }
    assert FakeVendor.calls == []


def test_wait_ends_with_approval_required(slack_gateway):
    url, ledger = slack_gateway
    started = time.monotonic()
    status, body = call(url + "/fake/v1/chat", BIG, {**HEADERS, "x-quarter-wait": "0.5"})
    assert status == 403 and json.loads(body)["error"] == "approval_required"
    assert time.monotonic() - started >= 0.5
    assert ledger.approval(json.loads(body)["approval_id"])["status"] == "pending"
    assert call(url + "/fake/v1/chat", BIG, {**HEADERS, "x-quarter-wait": "soon"})[0] == 400


def test_dashboard_decision_updates_the_slack_message(slack_gateway):
    url, _ = slack_gateway
    approval = pending(url)
    slack_calls("chat.postMessage")
    assert call(url + f"/approvals/{approval}/approve", {}, {"x-quarter-key": ADMIN_KEY})[0] == 200
    (_, _, update), = slack_calls("chat.update")
    assert (update["channel"], update["ts"]) == ("C1", "1.2")
    assert "Approved by" in update["text"] and "actions" not in json.dumps(update["blocks"])


def test_cli_decision_updates_the_slack_message(slack_gateway, monkeypatch):
    url, ledger = slack_gateway
    approval = pending(url)
    slack_calls("chat.postMessage")
    deadline = time.monotonic() + 5
    while ledger.slack_message(approval) is None and time.monotonic() < deadline:
        time.sleep(0.02)  # the ts is stored just after the post returns
    fake = Slack("xoxb-test", SECRET, "C1", {}, api=FakeSlack.url)
    monkeypatch.setattr(cli.Slack, "from_env", lambda environ: fake)
    with pytest.raises(SystemExit) as done:
        cli.main(["--db", ledger.path, "deny", approval, "--as", "Lee"])
    assert done.value.code == 0
    (_, _, update), = slack_calls("chat.update")
    assert update["ts"] == "1.2" and "Denied by Lee." in update["text"]
