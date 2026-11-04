"""Codex signed in with a ChatGPT plan, routed through Quarter as OpenAI's gateway guide says."""

import http.client
import json
from http.server import ThreadingHTTPServer

import pytest
from test_plug_in import ADMIN_KEY, AGENT_KEY, PRICES, Vendor, rows, start

from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

TOKEN = "Bearer chatgpt-user-token"


class ChatGPT(Vendor):
    """Answers at chatgpt.com/backend-api/codex like the OpenAI Responses API."""

    def do_GET(self):
        self.path = self.path.replace("/backend-api/codex/", "/v1/")
        super().do_GET()

    def do_POST(self):
        self.path = self.path.replace("/backend-api/codex/", "/v1/")
        super().do_POST()


@pytest.fixture
def codex(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), ChatGPT))
    Vendor.seen, Vendor.error = [], None
    ledger = Ledger(str(tmp_path / "l.db"))
    gateway = Gateway(ledger, [], PRICES, {"chatgpt": f"http://127.0.0.1:{vendor.server_port}"},
                      agent_keys={AGENT_KEY}, admin_key=ADMIN_KEY)
    proxy = start(serve(gateway, port=0))
    yield proxy.server_port, ledger
    proxy.shutdown()
    vendor.shutdown()
    ledger.db.close()


def request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"authorization": TOKEN, "chatgpt-account-id": "acct_1", "x-quarter-key": AGENT_KEY,
               "x-quarter-agent": "codex", "content-type": "application/json"}
    conn.request(method, path, json.dumps(body).encode() if body else None, headers)
    resp = conn.getresponse()
    out = resp.status, resp.headers, resp.read()
    conn.close()
    return out


def test_codex_on_a_chatgpt_plan_is_recorded_at_no_cost(codex):
    port, ledger = codex
    status, _, _ = request(port, "GET", "/chatgpt/backend-api/codex/models")
    assert status == 200
    status, headers, _ = request(port, "POST", "/chatgpt/backend-api/codex/responses",
                                 {"model": "gpt-5", "input": "hi", "stream": True})
    assert status == 200 and headers["x-quarter-billing"] == "subscription"
    method, path, sent, _ = Vendor.seen[-1]
    assert (method, path) == ("POST", "/v1/responses")  # reached chatgpt.com/backend-api/codex/responses
    assert sent["authorization"] == TOKEN and sent["chatgpt-account-id"] == "acct_1"
    assert "x-quarter-key" not in sent
    assert sum(a for _, k, a in rows(ledger) if k == "charge") == 0
    route, cost, list_cost = ledger.db.execute("select route, cost, list_cost from calls").fetchone()
    assert route == "subscription" and cost == 0 and list_cost > 0  # worth something at OpenAI's list price
