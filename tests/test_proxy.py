import base64
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.pricing import Price, PriceBook
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

PRICES = PriceBook(
    {
        "fake": {
            "m": Price(to_micros(1), to_micros(2)),  # $1 in, $2 out per million
            "mini": Price(to_micros("0.1"), to_micros("0.2")),
        },
        "search": {"*": Price(per_request=to_micros("0.01"))},
    }
)
AGENT_KEY, ADMIN_KEY = "agent-secret", "admin-secret"


class FakeVendor(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    usage: ClassVar[dict | None] = {"prompt_tokens": 1000, "completion_tokens": 500}
    calls: ClassVar[list] = []
    auth: ClassVar[dict] = {}

    def do_GET(self):
        FakeVendor.calls.append(("GET", self.path, None))
        self._send(200, "application/json", b'{"results": []}')

    def do_POST(self):
        FakeVendor.auth = {h: self.headers.get(h) for h in ("authorization", "x-api-key")}
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeVendor.calls.append(("POST", self.path, body))
        if body.get("stream") and "messages" in self.path:  # Anthropic style
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 2000}}},
                {"type": "content_block_delta", "delta": {"text": "hi"}},
                {"type": "message_delta", "usage": {"output_tokens": 300}},
            ]
            return self._stream([f"data: {json.dumps(e)}\n\n" for e in events])
        if body.get("stream"):  # OpenAI style, usage on the last chunk when asked
            chunks = [{"choices": [{"delta": {"content": "hi"}}]}]
            if body.get("stream_options", {}).get("include_usage"):
                chunks.append({"choices": [], "usage": FakeVendor.usage})
            return self._stream(
                [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
            )
        reply = json.dumps({"ok": True, "usage": FakeVendor.usage}).encode()
        self._send(200, "application/json", reply)

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, lines):
        self._send(200, "text/event-stream", "".join(lines).encode())

    def log_message(self, *args):
        pass


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def gateway(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls = []
    FakeVendor.usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    ledger = Ledger(str(tmp_path / "ledger.db"))
    caps = [
        Cap("customer", "acme", "month", to_micros("0.05")),
        Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.02")),
    ]
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(ledger, caps, PRICES, {"fake": base, "search": base}, {AGENT_KEY}, ADMIN_KEY)
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", ledger
    proxy.shutdown()
    vendor.shutdown()


HEADERS = {"x-quarter-key": AGENT_KEY, "x-quarter-agent": "a1", "x-quarter-customer": "acme"}
COST = {"category": "seat", "vendor": "x", "amount_usd": "1", "month": "2026-10"}


def call(url, body=None, headers=HEADERS, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def spend(ledger):
    return sum(t for _, _, t in ledger.totals("customer"))


def test_forwards_and_charges_reported_usage(gateway):
    url, ledger = gateway
    status, body = call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 600})
    assert status == 200 and json.loads(body)["ok"]
    assert ledger.totals("customer") == [("acme", "fake", to_micros("0.002"))]


def test_blocks_before_reaching_the_vendor(gateway):
    url, _ = gateway
    status, _ = call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 100_000})
    assert status in (402, 403)
    assert FakeVendor.calls == []


def test_cap_blocks_with_402(gateway):
    url, _ = gateway
    for _ in range(12):  # each about $0.0046 held, $0.002 charged; cap $0.05
        call(url + "/search/api", method="GET")
    status, body = call(url + "/search/api", method="GET")
    assert status == 402 and json.loads(body)["error"]["code"] == "cap_reached"


def test_agent_key_required(gateway):
    url, _ = gateway
    headers = {k: v for k, v in HEADERS.items() if k != "x-quarter-key"}
    assert call(url + "/fake/v1/chat", {"model": "m"}, headers)[0] == 401
    assert call(url + "/fake/v1/chat", {"model": "m"}, {**headers, "x-quarter-key": "x"})[0] == 401


def test_unpriced_model_is_charged_at_the_vendors_highest_price(gateway):
    url, ledger = gateway
    assert call(url + "/fake/x", {"model": "unknown"})[0] == 200
    assert call(url + "/fake/x", {"model": "unknown"})[0] == 200
    # 1000 in at $1 and 500 out at $2 per million: the prices of "m", the dearest fake model
    assert spend(ledger) == 2 * to_micros("0.002")
    assert [a["detail"] for a in ledger.audit_log() if a["action"] == "unknown_price"] == [
        "fake/unknown"
    ]


def test_unpriced_model_with_no_token_prices_anywhere_is_refused(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls = []
    flat = PriceBook({"search": {"api": Price(per_request=to_micros("0.01"))}})
    base = f"http://127.0.0.1:{vendor.server_port}"
    proxy = start(serve(Gateway(Ledger(str(tmp_path / "l.db")), [], flat, {"search": base}), port=0))
    try:
        url = f"http://127.0.0.1:{proxy.server_port}"
        assert call(url + "/search/x", {"model": "other"})[0] == 400
        assert FakeVendor.calls == []
    finally:
        proxy.shutdown()
        vendor.shutdown()


def test_agent_comes_from_the_key_name_then_the_header_then_default(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    ledger = Ledger(str(tmp_path / "l.db"))
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(
        ledger, [], PRICES, {"fake": base}, {AGENT_KEY, "plain-key"}, key_agents={AGENT_KEY: "bot"}
    )
    proxy = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{proxy.server_port}/fake/v1/chat"
        call(url, {"model": "m"}, {"x-quarter-key": "plain-key", "x-quarter-agent": "named"})
        call(url, {"model": "m"}, {"x-quarter-key": AGENT_KEY, "x-claude-code-session-id": "s1"})
        call(url, {"model": "m"}, {"x-quarter-key": AGENT_KEY, "x-quarter-agent": "bot"})
        call(url, {"model": "m"}, {"x-quarter-key": "plain-key"})
        status, body = call(url, {"model": "m"}, {"x-quarter-key": AGENT_KEY,
                                                  "x-quarter-agent": "someone-else"})
        assert status == 403 and b"belongs to agent bot" in body
        assert {a for a, _, _ in ledger.totals("agent")} == {"named", "bot", "default"}
        assert {t for t, _, _ in ledger.totals("task")} == {None, "s1"}
    finally:
        proxy.shutdown()
        vendor.shutdown()


@pytest.fixture
def keyed_gateway(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls, FakeVendor.auth = [], {}
    ledger = Ledger(str(tmp_path / "l.db"))
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(
        ledger,
        [],
        PRICES,
        {"fake": base, "anthropic": base, "nokey": base},
        {AGENT_KEY},
        key_agents={AGENT_KEY: "bot"},
        vendor_keys={"fake": "sk-real", "anthropic": "sk-ant-real"},
    )
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", ledger
    proxy.shutdown()
    vendor.shutdown()


def test_agent_key_sent_as_the_vendor_key_is_swapped_for_the_real_one(keyed_gateway):
    url, ledger = keyed_gateway
    bearer = {"authorization": f"Bearer {AGENT_KEY}"}
    assert call(url + "/fake/v1/chat", {"model": "m"}, bearer)[0] == 200
    assert FakeVendor.auth == {"authorization": "Bearer sk-real", "x-api-key": None}
    assert call(url + "/anthropic/v1/messages", {"model": "m"}, {"x-api-key": AGENT_KEY})[0] == 200
    assert FakeVendor.auth == {"authorization": None, "x-api-key": "sk-ant-real"}
    assert call(url + "/anthropic/v1/messages", {"model": "m"}, bearer)[0] == 200
    assert FakeVendor.auth == {"authorization": None, "x-api-key": "sk-ant-real"}
    assert {a for a, _, _ in ledger.totals("agent")} == {"bot"}


def test_vendor_key_not_ours_passes_through(keyed_gateway):
    url, _ = keyed_gateway
    headers = {"x-quarter-key": AGENT_KEY, "authorization": "Bearer sk-agents-own"}
    assert call(url + "/fake/v1/chat", {"model": "m"}, headers)[0] == 200
    assert FakeVendor.auth["authorization"] == "Bearer sk-agents-own"
    assert call(url + "/fake/v1/chat", {"model": "m"}, {"authorization": "Bearer x"})[0] == 401


def test_agent_key_as_vendor_key_without_a_vendor_key_is_refused(keyed_gateway):
    url, ledger = keyed_gateway
    status, body = call(url + "/nokey/v1/chat", {"model": "m"}, {"x-api-key": AGENT_KEY})
    assert status == 503 and "nokey" in json.loads(body)["error"]["message"]
    assert FakeVendor.calls == [] and ledger.totals("agent") == []


def test_missing_usage_charges_the_hold(gateway):
    url, ledger = gateway
    FakeVendor.usage = None
    assert call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 100})[0] == 200
    assert spend(ledger) > 0


def test_per_request_vendor_with_get_and_query(gateway):
    url, ledger = gateway
    assert call(url + "/search/api?q=agents", method="GET")[0] == 200
    assert FakeVendor.calls[-1][:2] == ("GET", "/api?q=agents")
    assert spend(ledger) == to_micros("0.01")


def test_openai_stream_is_relayed_and_usage_read(gateway):
    url, ledger = gateway
    body = {"model": "m", "stream": True, "max_tokens": 600}
    status, raw = call(url + "/fake/v1/chat/completions", body)
    assert status == 200 and b"[DONE]" in raw
    assert FakeVendor.calls[-1][2]["stream_options"] == {"include_usage": True}
    assert spend(ledger) == to_micros("0.002")


def test_anthropic_stream_usage(gateway):
    url, ledger = gateway
    assert (
        call(url + "/fake/v1/messages", {"model": "m", "stream": True, "max_tokens": 600})[0] == 200
    )
    assert spend(ledger) == to_micros("0.0026")  # 2000 in at $1/M + 300 out at $2/M


def test_approval_flow(gateway):
    url, _ = gateway
    big = {"model": "m", "max_tokens": 12_000}  # about $0.024, over the $0.02 approval line
    status, raw = call(url + "/fake/v1/chat", big)
    assert status == 403
    approval = json.loads(raw)["approval_id"]
    assert FakeVendor.calls == []

    assert call(url + f"/approvals/{approval}/approve", {}, {"x-quarter-key": ADMIN_KEY})[0] == 200
    retry = {**HEADERS, "x-quarter-approval": approval}
    assert call(url + "/fake/v1/chat", big, retry)[0] == 200
    assert call(url + "/fake/v1/chat", big, retry)[0] == 403  # single use


def test_approvals_need_admin(gateway):
    url, _ = gateway
    assert call(url + "/approvals/apr_x/approve", {}, HEADERS)[0] == 401
    assert call(url + "/approvals", headers=HEADERS)[0] == 401


def test_dashboard_login_and_page(gateway):
    url, _ = gateway
    call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 600})
    assert call(url + "/dashboard", headers={})[0] == 401
    token = base64.b64encode(f"finance:{ADMIN_KEY}".encode()).decode()
    month = f"{datetime.now(UTC):%Y-%m}"  # without one, the first 7 days show last month
    status, page = call(url + f"/dashboard?month={month}", headers={"authorization": f"Basic {token}"})
    assert status == 200 and b"Agent spend" in page and b"acme" in page


def test_reserved_vendor_names(tmp_path):
    for name in ("dashboard", "standing", "bills", "issuing", "openapi.json"):
        with pytest.raises(ValueError):
            Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {name: "http://x"})


def test_costs_through_the_gateway(gateway):
    url, ledger = gateway
    admin = {"x-quarter-key": ADMIN_KEY}
    assert call(url + "/costs", COST, HEADERS)[0] == 401
    assert call(url + "/costs", COST, admin)[0] == 200
    assert call(url + "/costs", {"vendor": "x"}, admin)[0] == 400
    assert len(ledger.costs_in("2026-10")) == 1


def test_cross_site_post_refused(gateway):
    url, ledger = gateway
    admin = {"x-quarter-key": ADMIN_KEY, "origin": "https://evil.example"}
    assert call(url + "/approvals/apr_x/approve", {}, admin)[0] == 403
    assert call(url + "/costs", COST, admin)[0] == 403
    assert ledger.costs_in("2026-10") == []


def test_bad_paths_and_amounts(gateway):
    url, _ = gateway
    assert call(url + "/fake/v1/../admin", {"model": "m"})[0] == 400
    admin = {"x-quarter-key": ADMIN_KEY}
    assert call(url + "/costs", {**COST, "amount_usd": "-5"}, admin)[0] == 400


def test_oversized_body_refused(gateway):
    url, _ = gateway
    host, port = url.removeprefix("http://").split(":")
    import http.client

    conn = http.client.HTTPConnection(host, int(port))
    conn.putrequest("POST", "/fake/v1/chat")
    conn.putheader("content-length", str(64 * 1024 * 1024))
    conn.endheaders()
    assert conn.getresponse().status == 413


def test_dashboard_security_headers(gateway):
    url, _ = gateway
    token = base64.b64encode(f"finance:{ADMIN_KEY}".encode()).decode()
    req = urllib.request.Request(url + "/dashboard", headers={"authorization": f"Basic {token}"})
    with urllib.request.urlopen(req) as resp:
        assert resp.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_upstream_must_be_https(tmp_path):
    with pytest.raises(ValueError):
        Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {"x": "http://vendor.example"})


def test_wrong_admin_keys_lock_out_and_are_audited(gateway):
    url, ledger = gateway
    for _ in range(10):
        assert call(url + "/approvals", headers={"x-quarter-key": "guess"})[0] == 401
    assert call(url + "/approvals", headers={"x-quarter-key": ADMIN_KEY})[0] == 429
    assert sum(r["action"] == "login_failed" for r in ledger.audit_log()) == 10


def test_behind_a_proxy_one_strangers_guesses_lock_out_only_the_stranger(gateway, tmp_path):
    url, ledger = gateway
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, client_ip_header="x-forwarded-for")
    behind = start(serve(gw, port=0))
    url = f"http://127.0.0.1:{behind.server_port}"
    stranger = {"x-quarter-key": "guess", "x-forwarded-for": "203.0.113.9"}
    for _ in range(10):
        assert call(url + "/approvals", headers=stranger)[0] == 401
    assert call(url + "/approvals", headers=stranger)[0] == 429
    finance = {"x-quarter-key": ADMIN_KEY, "x-forwarded-for": "198.51.100.1, 198.51.100.7"}
    assert call(url + "/approvals", headers=finance)[0] == 200
    assert call(url + "/approvals/apr_x/approve", {}, finance)[0] == 409  # a POST still signs in
    failed = [r for r in ledger.audit_log() if r["action"] == "login_failed"]
    assert {r["client"] for r in failed} == {"203.0.113.9"}
    behind.shutdown()


def test_admin_actions_are_audited(gateway):
    url, _ = gateway
    admin = {"x-quarter-key": ADMIN_KEY}
    call(url + "/costs", COST, admin)
    status, body = call(url + "/audit", headers=admin)
    assert status == 200 and json.loads(body)[0]["action"] == "cost"


def test_waiting_approval_is_announced(tmp_path, monkeypatch):
    from quarter_ledger import proxy

    sent, real = [], urllib.request.urlopen

    def fake_urlopen(req, *a, **kw):
        if req.full_url.startswith("https://hooks.example"):
            sent.append(json.loads(req.data)["text"])
            raise urllib.error.URLError("offline")
        return real(req, *a, **kw)

    monkeypatch.setattr(proxy.urllib.request, "urlopen", fake_urlopen)
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.001"))]
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(
        ledger,
        caps,
        PRICES,
        {"fake": base},
        {AGENT_KEY},
        ADMIN_KEY,
        approval_webhook="https://hooks.example/x",
        dashboard_url="https://q.example",
    )
    proxy_server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{proxy_server.server_port}"
        assert call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 600})[0] == 403
        for _ in range(50):
            if any(r["action"] == "webhook_failed" for r in ledger.audit_log()):
                break
            threading.Event().wait(0.05)
        assert "a1 for acme" in sent[0] and "https://q.example/dashboard" in sent[0]
        assert any(r["action"] == "webhook_failed" for r in ledger.audit_log())
    finally:
        proxy_server.shutdown()
        vendor.shutdown()
    with pytest.raises(ValueError):
        Gateway(ledger, [], PRICES, {}, approval_webhook="http://hooks.example")


def test_forecast_endpoint(gateway):
    url, _ = gateway
    call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 600})
    admin = {"x-quarter-key": ADMIN_KEY}
    status, body = call(url + "/forecast", headers=admin)
    assert status == 200 and json.loads(body)["so_far"] > 0
    assert call(url + "/forecast", headers=HEADERS)[0] == 401


def test_receipt_endpoint(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.001"))]
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(ledger, caps, PRICES, {"fake": base}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        admin = {"x-quarter-key": ADMIN_KEY}
        status, body = call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 600})
        apr = json.loads(body)["approval_id"]
        assert call(url + f"/approvals/{apr}/receipt", headers=admin)[0] == 404  # still pending
        call(url + f"/approvals/{apr}/approve", {}, admin)
        status, body = call(url + f"/approvals/{apr}/receipt", headers=admin)
        from quarter_ledger.receipts import verify

        assert status == 200 and verify(json.loads(body)["receipt"], "s" * 32)["sub"] == apr
        headers = {**HEADERS, "x-quarter-approval": apr}
        assert call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 600}, headers)[0] == 200
    finally:
        server.shutdown()
        vendor.shutdown()


def test_budget_alert_sent_once(tmp_path, monkeypatch):
    from quarter_ledger import proxy

    sent = []
    monkeypatch.setattr(proxy.Gateway, "_notify", lambda self, text, ref: sent.append(text))
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("customer", "acme", "month", to_micros("0.01"))]
    gw = Gateway(ledger, caps, PRICES, {}, approval_webhook="https://hooks.example/x")
    ledger.db.execute(
        "insert into entries (call_id, kind, at, agent, customer, vendor, amount)"
        " values ('c', 'charge', ?, 'bot', 'acme', 'fake', ?)",
        (
            __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
            to_micros("0.02"),
        ),
    )
    gw.check_budgets()
    gw._last_budget_check = -1e9
    gw.check_budgets()
    assert len(sent) == 1 and "customer acme" in sent[0] and "has been reached" in sent[0]


def test_fallback_moves_a_capped_call_to_the_cheaper_model(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls = []
    FakeVendor.usage = {"prompt_tokens": 100, "completion_tokens": 100}
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("customer", "acme", "month", to_micros("0.005"))]
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(
        ledger, caps, PRICES, {"fake": base}, {AGENT_KEY}, fallbacks={"fake": {"m": "mini"}}
    )
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        body = {"model": "m", "max_tokens": 4000}  # $0.008 worst case on m, over the cap
        req = urllib.request.Request(url + "/fake/v1/chat", json.dumps(body).encode(), HEADERS)
        with urllib.request.urlopen(req) as resp:
            assert resp.headers["x-quarter-model"] == "m -> mini"
        assert FakeVendor.calls[-1][2]["model"] == "mini"
        off = {**HEADERS, "x-quarter-fallback": "off"}
        assert call(url + "/fake/v1/chat", body, off)[0] == 402
    finally:
        server.shutdown()
        vendor.shutdown()


def test_paused_agent_gets_423_until_resumed(gateway):
    url, ledger = gateway
    ledger.pause("a1", "spent $9 in 5 minutes")
    status, body = call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 10})
    assert status == 423 and json.loads(body)["error"]["code"] == "agent_paused"
    admin = {"x-quarter-key": ADMIN_KEY}
    assert call(url + "/agents/a1/resume", {}, admin)[0] == 200
    assert call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 10})[0] == 200


def test_one_policy_for_payments_on_any_rail(tmp_path):
    from quarter_ledger.payments import authorised

    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [
        Cap("customer", "acme", "month", to_micros("1")),
        Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.5")),
    ]
    gw = Gateway(
        ledger,
        caps,
        PRICES,
        {},
        {AGENT_KEY},
        ADMIN_KEY,
        signing_key="s" * 32,
        payees={"x402": ["0xGood"]},
    )
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        pay = {"rail": "x402", "payee": "0xgood", "amount_usd": "0.3"}
        status, body = call(url + "/authorize", pay)
        reply = json.loads(body)
        assert status == 200 and authorised(reply["authorisation"], "s" * 32, "0xGood", 300_000)

        status, body = call(url + "/authorize", {**pay, "payee": "0xBad"})
        assert status == 403 and json.loads(body)["error"] == "approval_required"  # unlisted
        assert call(url + "/authorize", {**pay, "amount_usd": "0.6"})[0] == 403  # approval
        assert call(url + "/authorize", {**pay, "rail": "card", "amount_usd": "0.45"})[0] == 200
        status, body = call(url + "/authorize", {**pay, "amount_usd": "0.4"})
        assert status == 402 and json.loads(body)["error"] == "cap_reached"  # 0.3+0.45+0.4 > 1

        other = {**HEADERS, "x-quarter-agent": "someone-else"}
        settle = {"call_id": reply["call_id"], "amount_usd": "0.35", "tx": "0xhash"}
        assert call(url + "/settle", settle, other)[0] == 404
        status, body = call(url + "/settle", settle)
        assert status == 200 and json.loads(body)["over_authorised"] is True
        assert call(url + "/settle", settle)[0] == 409
        actions = [r["action"] for r in ledger.audit_log()]
        assert "payment_settled" in actions and "over_authorised" in actions
        assert spend(ledger) == to_micros("0.35")
    finally:
        server.shutdown()


def test_a_payee_not_on_the_list_waits_for_a_person(tmp_path):
    from quarter_ledger.payments import authorised

    ledger = Ledger(str(tmp_path / "l.db"))
    payees = {"x402": ["0xGood"]}
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32, payees=payees)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        new = {"rail": "x402", "payee": "0xNew", "amount_usd": "0.3"}
        status, body = call(url + "/authorize", new)
        reply = json.loads(body)
        assert status == 403 and reply["reason"] == "0xNew is not on the x402 payee list"
        ledger.decide_approval(reply["approval_id"], True, "dana")
        approved = {**HEADERS, "x-quarter-approval": reply["approval_id"]}
        # The approval is for that payee: another one asks again, and it stays unused.
        other = {**new, "payee": "0xOther"}
        assert json.loads(call(url + "/authorize", other, approved)[1])["error"] == "approval_required"
        status, body = call(url + "/authorize", new, approved)
        assert status == 200 and authorised(json.loads(body)["authorisation"], "s" * 32, "0xNew", 300_000)

        payees["unlisted"] = "deny"
        status, body = call(url + "/authorize", new)
        assert status == 403 and json.loads(body)["error"] == "payee_not_allowed"
    finally:
        server.shutdown()


def test_authorize_waits_for_a_person_when_asked(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    payees = {"x402": ["0xGood"]}
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32, payees=payees)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        new = {"rail": "x402", "payee": "0xNew", "amount_usd": "0.3"}
        waiting = {**HEADERS, "x-quarter-wait": "10"}
        for approve, expected in ((True, 200), (False, 403)):
            result = {}
            thread = threading.Thread(target=lambda r=result: r.update(reply=call(url + "/authorize", new, waiting)))
            thread.start()
            while not ledger.approvals():
                thread.join(0.02)
            approval = ledger.approvals()[0]["id"]
            ledger.decide_approval(approval, approve, "dana")
            gw.decided(approval)
            thread.join(5)
            status, body = result["reply"]
            assert status == expected
            if approve:
                assert json.loads(body)["call_id"] and ledger.approval(approval)["status"] == "used"
            else:
                assert json.loads(body) == {"error": "approval_denied", "approval_id": approval, "decided_by": "dana"}
        assert call(url + "/authorize", new, {**HEADERS, "x-quarter-wait": "soon"})[0] == 400
    finally:
        server.shutdown()


def test_a_cap_set_to_ask_lets_a_person_pass_one_call(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros("1"), over="approve"),
            Cap("customer", "acme", "month", to_micros("5"))]
    gw = Gateway(ledger, caps, PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        pay = {"rail": "card", "payee": "shop", "amount_usd": "0.8"}
        assert call(url + "/authorize", pay)[0] == 200
        status, body = call(url + "/authorize", pay)
        reply = json.loads(body)
        assert status == 403 and reply["reason"] == "agent a1: day cap reached"
        ledger.decide_approval(reply["approval_id"], True, "dana")
        approved = {**HEADERS, "x-quarter-approval": reply["approval_id"]}
        assert call(url + "/authorize", pay, approved)[0] == 200
        # A cap left to block still blocks, and no one is asked.
        status, body = call(url + "/authorize", {**pay, "amount_usd": "6"}, {**HEADERS, "x-quarter-customer": "acme"})
        assert status == 402 and json.loads(body)["reason"] == "customer acme: month cap reached"
    finally:
        server.shutdown()


def test_same_request_over_and_over_pauses_the_agent(tmp_path):
    from quarter_ledger.runaway import RunawayRule

    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, runaway=RunawayRule(repeats=3))
    gw.check_repeats("bot", b'{"q": 1}')
    gw.check_repeats("bot", b'{"q": 2}')
    gw.check_repeats("bot", b'{"q": 1}')
    assert ledger.paused("bot") is None
    gw.check_repeats("bot", b'{"q": 1}')
    assert "same request 3 times" in ledger.paused("bot")


def test_a_route_sends_one_customers_calls_to_another_upstream(tmp_path):
    from quarter_ledger.rules import load_rules

    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls = []
    ledger = Ledger(str(tmp_path / "l.db"))
    base = f"http://127.0.0.1:{vendor.server_port}"
    rules = load_rules([{"upstream": "fake", "customer": "eu-*", "to": "fake-eu"}])
    with pytest.raises(ValueError, match="fake-eu"):
        Gateway(ledger, [], PRICES, {"fake": base}, rules=rules)
    gw = Gateway(ledger, [], PRICES, {"fake": base, "fake-eu": base + "/eu"}, {AGENT_KEY}, ADMIN_KEY,
                 vendor_keys={"fake": "key-us", "fake-eu": "key-eu"}, rules=rules)
    proxy = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{proxy.server_port}/fake/v1/chat"
        bearer = {"authorization": f"Bearer {AGENT_KEY}", "x-quarter-agent": "a1"}
        assert call(url, {"model": "m", "max_tokens": 10}, {**bearer, "x-quarter-customer": "eu-acme"})[0] == 200
        assert FakeVendor.calls[-1][1] == "/eu/v1/chat" and FakeVendor.auth["authorization"] == "Bearer key-eu"
        assert call(url, {"model": "m", "max_tokens": 10}, {**bearer, "x-quarter-customer": "us-acme"})[0] == 200
        assert FakeVendor.calls[-1][1] == "/v1/chat" and FakeVendor.auth["authorization"] == "Bearer key-us"
        went = ledger.db.execute("select vendor, upstream from calls order by at").fetchall()
        assert went == [("fake", "fake-eu"), ("fake", "fake")]
        assert rules.listed() == ["rule 1: route fake to fake-eu for customer eu-*"]
        # With no key of its own, a routed call is refused rather than sent the vendor's key.
        del gw.vendor_keys["fake-eu"]
        status, body = call(url, {"model": "m", "max_tokens": 10}, {**bearer, "x-quarter-customer": "eu-acme"})
        assert status == 503 and "fake-eu" in json.loads(body)["error"]["message"]
    finally:
        proxy.shutdown()
        vendor.shutdown()


def test_spend_checks_run_on_one_background_thread_and_still_pause(tmp_path, monkeypatch):
    import time
    from datetime import UTC, datetime

    from quarter_ledger.runaway import RunawayRule

    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, runaway=RunawayRule())
    ledger.db.execute(  # $10 in the last minute, against no usual spend at all
        "insert into entries (call_id, kind, at, agent, vendor, amount) values ('c', 'charge', ?, 'bot', 'fake', ?)",
        (datetime.now(UTC).isoformat(), to_micros(10)),
    )
    threads, checked = set(), []
    real = Gateway.check_runaway

    def watched(self, agent):
        threads.add(threading.get_ident())
        checked.append(agent)
        real(self, agent)

    monkeypatch.setattr(Gateway, "check_runaway", watched)
    for i in range(30):
        gw.after_spend("bot" if i % 2 else "calm")
    deadline = time.monotonic() + 5
    while (ledger.paused("bot") is None or "calm" not in checked) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "spent $10" in ledger.paused("bot") and ledger.paused("calm") is None
    assert len(threads) == 1 and threading.get_ident() not in threads
    assert len(checked) <= 30


def answer(url, body=None, headers=HEADERS, method=None):
    """The status, headers and JSON body of a call, refused or not."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, json.loads(e.read() or b"null")


def test_refusals_are_in_openais_error_shape_so_its_sdk_reads_them(gateway):
    url, _ = gateway
    for _ in range(12):
        call(url + "/search/api", method="GET")
    status, _, body = answer(url + "/search/api", method="GET")
    assert status == 402 and body["reason"] == body["error"]["reason"]  # the detail stays beside it too
    assert body["error"] == {
        "type": "billing_error", "code": "cap_reached", "reason": body["reason"],
        "message": f"Quarter refused this call: {body['reason']}. It did not reach the vendor. It can go"
                   " once spend is back under the cap, or the cap is raised.",
    }
    big = {"model": "m", "max_tokens": 12_000}  # over the $0.02 approval line
    status, _, body = answer(url + "/fake/v1/chat", big, {**HEADERS, "x-quarter-customer": "big"})
    approval = body["approval_id"]
    assert status == 403 and body["error"]["code"] == "approval_required" and body["error"]["approval_id"] == approval
    assert body["error"]["type"] == "permission_error"
    assert f"send the same request again with the header x-quarter-approval: {approval}" in body["error"]["message"]
    status, _, body = answer(url + "/fake/v1/chat", {"model": "m"}, {})
    assert status == 401 and body["error"]["type"] == "authentication_error" and body["error"]["code"] is None
    assert body["error"]["message"].startswith("Quarter refused this call: a Quarter agent key is required")
    status, _, body = answer(url + "/nope/v1/chat", {"model": "m"})
    assert status == 404 and body["error"]["message"] == "Quarter refused this call: unknown vendor nope"


def test_anthropic_calls_are_refused_in_anthropics_error_shape(keyed_gateway):
    url, _ = keyed_gateway
    status, _, body = answer(url + "/anthropic/v1/messages", {"model": "m"}, {"x-api-key": "wrong"})
    assert status == 401 and body["type"] == "error" and body["error"]["type"] == "authentication_error"
    assert "x-quarter-key" in body["error"]["message"]
    assert "type" not in answer(url + "/fake/v1/chat", {"model": "m"}, {"x-api-key": "wrong"})[2]


def test_quarters_own_routes_keep_their_own_error_shape(gateway):
    url, _ = gateway
    status, _, body = answer(url + "/approvals", headers={"x-quarter-key": "guess"})
    assert status == 401 and body == {"error": "a person's key or the admin key is required"}


def test_a_lockout_tells_the_sdk_not_to_retry_inside_the_window(gateway):
    url, _ = gateway
    for _ in range(10):
        call(url + "/approvals", headers={"x-quarter-key": "guess"})
    status, headers, body = answer(url + "/fake/v1/models", headers=HEADERS)
    assert status == 429 and headers["retry-after"] == "300" and headers["x-should-retry"] == "false"
    assert body["error"]["type"] == "rate_limit_error"


def test_no_vendor_key_tells_the_sdk_not_to_retry(keyed_gateway):
    url, _ = keyed_gateway
    status, headers, _ = answer(url + "/nokey/v1/chat", {"model": "m"}, {"x-api-key": AGENT_KEY})
    assert status == 503 and headers["x-should-retry"] == "false"


class HangsUp(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        self.close_connection = True  # no reply: the vendor may have done the work

    def log_message(self, *args):
        pass


def test_a_vendor_that_hangs_up_is_charged_once_and_the_sdk_told_not_to_resend(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), HangsUp))
    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {"fake": f"http://127.0.0.1:{vendor.server_port}"}, {AGENT_KEY})
    proxy = start(serve(gw, port=0))
    try:
        request = {"model": "m", "max_tokens": 10}
        status, headers, body = answer(f"http://127.0.0.1:{proxy.server_port}/fake/v1/chat", request)
        assert status == 502 and headers["x-should-retry"] == "false"
        assert body["error"]["message"].startswith("Quarter could not complete this call: vendor connection lost")
        assert spend(ledger) == len(json.dumps(request)) + 2 * 10  # the hold, in full: a resend would pay again
    finally:
        proxy.shutdown()
        vendor.shutdown()


def ask(url, method="GET", body=None, headers=HEADERS):
    """answer(), with the method first."""
    return answer(url, body, headers, method)


def test_a_wrong_method_on_a_quarter_route_is_405_not_a_vendor(gateway):
    url, _ = gateway
    for method, path, allow in [("GET", "/authorize", "POST"), ("GET", "/costs", "POST"),
                                ("POST", "/forecast", "GET"), ("POST", "/approvals", "GET"),
                                ("PUT", "/bills", "GET, POST"), ("DELETE", "/standing", "GET, POST"),
                                ("GET", "/approvals/apr_x/approve", "POST")]:
        status, headers, body = ask(url + path, method, {} if method != "GET" else None)
        assert (status, headers["allow"]) == (405, allow), (method, path)
        assert method in body["error"] and "vendor" not in body["error"]
    for path in ("/approvals/a/b/approve", "/costs/x", "/slack/other"):
        status, _, body = ask(url + path, "POST", {})
        assert status == 404 and "no Quarter route" in body["error"], path


def test_slack_actions_without_slack_says_so(gateway):
    url, _ = gateway
    status, _, body = ask(url + "/slack/actions", "POST", {})
    assert status == 503 and "Slack approvals are not set up" in body["error"]


def test_settle_needs_no_signing_key(gateway):
    url, ledger = gateway
    status, _, body = ask(url + "/authorize", "POST", {"rail": "x402", "payee": "0xP", "amount_usd": "0.01"})
    assert status == 503 and "QUARTER_SIGNING_KEY" in body["error"]
    ids = {"agent": "a1", "task": None, "customer": "acme"}
    _, ref = ledger.hold([], ids, "x402:0xP", "x402", to_micros("0.01"))
    status, _, body = ask(url + "/settle", "POST", {"call_id": ref, "amount_micros": 10_000})
    assert status == 200 and body["ok"] is True


def test_refusals_use_the_error_object(gateway):
    url, _ = gateway
    admin = {"x-quarter-key": ADMIN_KEY}
    status, _, body = ask(url + "/standing/std_nope/revoke", "POST", {}, admin)
    assert status == 404 and body["id"] == "std_nope" and body["error"]

    approval = json.loads(call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 12_000})[1])["approval_id"]
    assert ask(url + f"/approvals/{approval}/approve", "POST", {}, admin)[0] == 200
    status, _, body = ask(url + f"/approvals/{approval}/deny", "POST", {}, admin)
    assert status == 409 and body == {"ok": False, "error": f"{approval} is already approved"}
    status, _, body = ask(url + "/approvals/apr_nope/approve", "POST", {}, admin)
    assert status == 409 and body["error"] == "no approval apr_nope"
    status, _, body = ask(url + "/agents/a1/resume", "POST", {}, admin)
    assert status == 409 and body == {"ok": False, "error": "agent a1 is not paused"}

    status, headers, body = ask(url + "/dashboard", headers={})
    assert status == 401 and headers["www-authenticate"] == 'Basic realm="Quarter"' and body["error"]


def test_costs_month_is_checked(gateway):
    url, ledger = gateway
    admin = {"x-quarter-key": ADMIN_KEY}
    for month in ("2026-13", "Oct 2026", "2026-1", 202610):
        status, _, body = ask(url + "/costs", "POST", {**COST, "month": month}, admin)
        assert status == 400 and "YYYY-MM" in body["error"], month
    assert ledger.costs_in("2026-10") == []


def test_receipt_path_has_exactly_one_id(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.02"))]
    gw = Gateway(ledger, caps, PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32)
    server = start(serve(gw, port=0))
    try:
        url, admin = f"http://127.0.0.1:{server.server_port}", {"x-quarter-key": ADMIN_KEY}
        pay = {"rail": "x402", "payee": "0xP", "amount_usd": "0.5"}
        approval = json.loads(call(url + "/authorize", pay)[1])["approval_id"]
        ledger.decide_approval(approval, True, "dana")
        assert ask(url + f"/approvals/{approval}/receipt", headers=admin)[0] == 200
        status, _, body = ask(url + f"/approvals/{approval}/extra/receipt", headers=admin)
        assert status == 404 and "receipt" not in body
    finally:
        server.shutdown()


def test_approvals_listed_by_status(gateway):
    url, ledger = gateway
    admin = {"x-quarter-key": ADMIN_KEY}
    big = {"model": "m", "max_tokens": 12_000}
    first, second, third = (json.loads(call(url + "/fake/v1/chat", big)[1])["approval_id"] for _ in range(3))
    ledger.decide_approval(first, True, "dana")
    ledger.decide_approval(second, False, "dana")

    def listed(query=""):
        status, _, body = ask(url + "/approvals" + query, headers=admin)
        assert status == 200
        return [(a["id"], a["status"]) for a in body]

    assert listed() == listed("?status=pending") == [(third, "pending")]
    assert listed("?status=approved") == [(first, "approved")]
    assert listed("?status=denied") == [(second, "denied")]
    assert {a for a, _ in listed("?status=all")} == {first, second, third}
    assert ask(url + "/approvals?status=used", headers=admin)[0] == 400


def test_authorize_answers_both_spellings_and_the_call_header(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        pay = {"rail": "x402", "payee": "0xP", "amount_usd": "0.25"}
        status, headers, body = ask(url + "/authorize", "POST", pay)
        assert status == 200 and body["authorization"] == body["authorisation"]
        assert headers["x-quarter-call"] == body["call_id"]
    finally:
        server.shutdown()
