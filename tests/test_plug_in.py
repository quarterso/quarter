"""Real agent clients plugged into the gateway.

The fake vendor below answers in the exact wire formats of OpenAI (chat completions and
the Responses API) and Anthropic (Messages and count_tokens), streamed and not, with
real-shaped errors. Each test checks what the client receives and what the ledger kept.
"""

import gzip
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from quarter_ledger.cli import wrap_env
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.pricing import BUILT_IN, Price, PriceBook
from quarter_ledger.proxy import DEFAULT_MAX_TOKENS, Gateway, serve, usage_from
from quarter_ledger.store import Ledger

GPT = Price(to_micros("1.25"), to_micros(10))
EMBED = Price(to_micros("0.02"))
SONNET = Price(to_micros(3), to_micros(15))
CACHED = Price(to_micros(3), to_micros(15), cache_read_per_million=to_micros("0.3"),
               cache_write_per_million=to_micros("3.75"),
               cache_write_extended_per_million=to_micros(6))
PRICES = PriceBook(
    {
        "openai": {"gpt-5": GPT, "text-embedding-3-small": EMBED},
        "anthropic": {"claude-sonnet-4-5": SONNET, "claude-cached": CACHED},
    }
)
AGENT_KEY, ADMIN_KEY = "agent-secret", "admin-secret"
QUARTER = {"x-quarter-key": AGENT_KEY, "x-quarter-agent": "a1", "x-quarter-customer": "acme"}


def sse(events, named=True):
    """Server-sent events as the vendors write them, one event per write."""
    out = []
    for e in events:
        head = f"event: {e['type']}\n" if named else ""
        out.append(f"{head}data: {json.dumps(e, separators=(',', ':'))}\n\n".encode())
    return out


OPENAI_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 500,
    "total_tokens": 1500,
    "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0},
}
CHAT = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1761955200,
    "model": "gpt-5-2025-08-07",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi", "refusal": None, "annotations": []},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ],
    "usage": OPENAI_USAGE,
    "service_tier": "default",
    "system_fingerprint": None,
}


def chat_chunks(include_usage):
    base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1761955200,
            "model": "gpt-5-2025-08-07", "service_tier": "default", "system_fingerprint": None}
    extra = {"usage": None} if include_usage else {}
    deltas = [{"role": "assistant", "content": "", "refusal": None}, {"content": "Hi"}, {}]
    chunks = [
        {**base, "choices": [{"index": 0, "delta": d, "logprobs": None,
                              "finish_reason": None if d else "stop"}], **extra}
        for d in deltas
    ]
    if include_usage:
        chunks.append({**base, "choices": [], "usage": OPENAI_USAGE})
    return [f"data: {json.dumps(c)}\n\n".encode() for c in chunks] + [b"data: [DONE]\n\n"]


RESPONSES_USAGE = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 500,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 1500,
}


def response_obj(status, usage):
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1761955200,
        "status": status,
        "model": "gpt-5-2025-08-07",
        "output": [] if status != "completed" else [
            {"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": "Hi", "annotations": []}]}
        ],
        "usage": usage,
    }


RESPONSE_EVENTS = [
    {"type": "response.created", "sequence_number": 0, "response": response_obj("in_progress", None)},
    {"type": "response.in_progress", "sequence_number": 1,
     "response": response_obj("in_progress", None)},
    {"type": "response.output_text.delta", "sequence_number": 2, "item_id": "msg_1",
     "output_index": 0, "content_index": 0, "delta": "Hi"},
    {"type": "response.completed", "sequence_number": 3,
     "response": response_obj("completed", RESPONSES_USAGE)},
]

ANTHROPIC_USAGE = {
    "input_tokens": 2000,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 300,
    "service_tier": "standard",
}
MESSAGE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5-20250929",
    "content": [{"type": "text", "text": "Hi"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": ANTHROPIC_USAGE,
}


def message_events(usage=ANTHROPIC_USAGE, deltas=1):
    start_usage = {**usage, "output_tokens": 1}
    start = {k: v for k, v in MESSAGE.items() if k not in ("content", "usage")}
    events = [
        {"type": "message_start",
         "message": {**start, "content": [], "stop_reason": None, "usage": start_usage}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
    ]
    events += [
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}
    ] * deltas
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": usage["output_tokens"]}},
        {"type": "message_stop"},
    ]
    return events


ERRORS = {
    429: (
        {"type": "error", "error": {"type": "rate_limit_error",
                                    "message": "Number of request tokens has exceeded your rate limit."},
         "request_id": "req_011"},
        {"retry-after": "17", "x-should-retry": "true", "request-id": "req_011",
         "anthropic-ratelimit-tokens-remaining": "0"},
    ),
    500: (
        {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}},
        {"request-id": "req_012"},
    ),
    400: (
        {"error": {"message": "Unsupported parameter: 'max_tokens'.", "type": "invalid_request_error",
                   "param": "max_tokens", "code": "unsupported_parameter"}},
        {"x-request-id": "req_013"},
    ),
}


class Vendor(BaseHTTPRequestHandler):
    """Answers like api.openai.com and api.anthropic.com."""

    protocol_version = "HTTP/1.1"
    seen: ClassVar[list] = []
    error: ClassVar[int | None] = None
    gzip_body: ClassVar[bool] = False
    anthropic_usage: ClassVar[dict] = ANTHROPIC_USAGE
    wait_after_first: ClassVar[threading.Event | None] = None  # pauses a stream mid-way
    last_body: ClassVar[bytes] = b""

    def do_GET(self):
        Vendor.seen.append(("GET", self.path, self._headers(), None))
        if self.path.startswith("/v1/models"):
            body = {"object": "list", "data": [{"id": "gpt-5", "object": "model",
                                                "created": 1, "owned_by": "system"}]}
            return self._send(200, json.dumps(body).encode())
        self._send(404, b'{"error": "not found"}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["content-length"]))
        body = json.loads(raw)
        Vendor.seen.append(("POST", self.path, self._headers(), body))
        path = self.path.split("?")[0]
        if Vendor.error:
            reply, headers = ERRORS[Vendor.error]
            return self._send(Vendor.error, json.dumps(reply).encode(), headers)
        if path == "/v1/messages/count_tokens":
            return self._send(200, b'{"input_tokens":2095}')
        if path == "/v1/messages":
            if body.get("stream"):
                return self._stream(sse(message_events(Vendor.anthropic_usage, deltas=60)))
            return self._send(200, json.dumps({**MESSAGE, "usage": Vendor.anthropic_usage}).encode())
        if path == "/v1/chat/completions":
            if body.get("stream"):
                include = (body.get("stream_options") or {}).get("include_usage")
                return self._stream(chat_chunks(include))
            return self._send(200, json.dumps(CHAT).encode())
        if path == "/v1/responses":
            if body.get("stream"):
                return self._stream(sse(RESPONSE_EVENTS))
            return self._send(200, json.dumps(response_obj("completed", RESPONSES_USAGE)).encode())
        if path == "/v1/embeddings":
            reply = {"object": "list", "data": [{"object": "embedding", "index": 0,
                                                 "embedding": [0.1, -0.2]}],
                     "model": "text-embedding-3-small",
                     "usage": {"prompt_tokens": 8, "total_tokens": 8}}
            return self._send(200, json.dumps(reply).encode())
        self._send(404, b'{"error": "not found"}')

    def _headers(self):
        return {k.lower(): v for k, v in self.headers.items()}

    def _send(self, status, body, headers=None):
        if Vendor.gzip_body:
            body = gzip.compress(body)
        Vendor.last_body = body
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("request-id", "req_vendor")
        if Vendor.gzip_body:
            self.send_header("content-encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, events):
        """Chunked, one write per event, like the real APIs."""
        Vendor.last_body = b"".join(events)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        try:
            for n, e in enumerate(events):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(e), e))
                self.wfile.flush()
                if n == 0 and Vendor.wait_after_first is not None:
                    Vendor.wait_after_first.wait(5)
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            self.close_connection = True  # the gateway hung up

    def log_message(self, *args):
        pass


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def gw(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), Vendor))
    Vendor.seen, Vendor.error, Vendor.gzip_body = [], None, False
    Vendor.anthropic_usage, Vendor.wait_after_first = ANTHROPIC_USAGE, None
    ledger = Ledger(str(tmp_path / "ledger.db"))
    base = f"http://127.0.0.1:{vendor.server_port}"
    caps = [Cap("customer", "acme", "month", to_micros(50))]
    gateway = Gateway(
        ledger, caps, PRICES, {"openai": base, "anthropic": base},
        agent_keys={AGENT_KEY}, admin_key=ADMIN_KEY,
    )
    proxy = start(serve(gateway, port=0))
    yield proxy.server_port, ledger, gateway
    proxy.shutdown()
    vendor.shutdown()
    ledger.db.close()


def post(port, path, body, headers=None, raw=None):
    """POST like an SDK: returns status, headers and the decoded body bytes."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else json.dumps(body).encode()
    conn.request("POST", path, data, {"content-type": "application/json", **QUARTER,
                                      **(headers or {})})
    resp = conn.getresponse()
    out = resp.status, resp.headers, resp.read()
    conn.close()
    return out


def rows(ledger):
    return ledger.db.execute("select call_id, kind, amount from entries order by id").fetchall()


def charged(ledger):
    return sum(a for _, k, a in rows(ledger) if k == "charge")


def assert_settled_once(ledger, calls=1):
    """Every hold released exactly once, at most one charge per call, nothing left open."""
    by_call = {}
    for call_id, kind, _ in rows(ledger):
        by_call.setdefault(call_id, []).append(kind)
    assert len(by_call) == calls
    for kinds in by_call.values():
        assert kinds.count("hold") == 1 and kinds.count("release") == 1
        assert kinds.count("charge") <= 1


def held(ledger):
    return [a for _, k, a in rows(ledger) if k == "hold"]


# OpenAI


def test_openai_chat(gw):
    port, ledger, _ = gw
    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "Hi"}],
            "max_completion_tokens": 20000}
    status, headers, reply = post(port, "/openai/v1/chat/completions", body,
                                  {"authorization": "Bearer sk-proj-x"})
    assert status == 200 and reply == Vendor.last_body
    assert headers["request-id"] == "req_vendor" and headers["x-quarter-call"]
    _, path, sent, received = Vendor.seen[-1]
    assert path == "/v1/chat/completions" and received == body
    assert sent["authorization"] == "Bearer sk-proj-x"
    assert not any(h.lower().startswith("x-quarter") for h in sent)  # Quarter's key stays here
    assert charged(ledger) == GPT.cost(1000, 500)
    assert held(ledger) == [GPT.cost(len(json.dumps(body)), 20000)]  # the real output limit
    assert_settled_once(ledger)


@pytest.mark.parametrize("asked", [False, True])
def test_openai_chat_stream(gw, asked):
    port, ledger, _ = gw
    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "Hi"}], "stream": True}
    if asked:
        body["stream_options"] = {"include_usage": True}
    status, headers, reply = post(port, "/openai/v1/chat/completions", body)
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    assert reply == Vendor.last_body and reply.endswith(b"data: [DONE]\n\n")
    assert Vendor.seen[-1][3]["stream_options"] == {"include_usage": True}
    assert charged(ledger) == GPT.cost(1000, 500)
    assert_settled_once(ledger)


def test_openai_responses(gw):
    port, ledger, _ = gw
    body = {"model": "gpt-5", "input": "Hi", "max_output_tokens": 30000}
    status, _, reply = post(port, "/openai/v1/responses", body)
    assert status == 200 and reply == Vendor.last_body
    assert held(ledger) == [GPT.cost(len(json.dumps(body)), 30000)]
    assert charged(ledger) == GPT.cost(1000, 500)
    assert_settled_once(ledger)


def test_openai_responses_stream_reads_usage_from_completed(gw):
    port, ledger, _ = gw
    body = {"model": "gpt-5", "input": "Hi", "stream": True}
    status, _, reply = post(port, "/openai/v1/responses", body)
    assert status == 200 and reply == Vendor.last_body
    assert b"event: response.completed" in reply
    assert "stream_options" not in Vendor.seen[-1][3]  # only chat completions take it
    assert charged(ledger) == GPT.cost(1000, 500)  # not the whole hold
    assert_settled_once(ledger)


def test_openai_embeddings_are_priced(gw):
    port, ledger, _ = gw
    body = {"model": "text-embedding-3-small", "input": "Hi"}
    status, _, reply = post(port, "/openai/v1/embeddings", body)
    assert status == 200 and reply == Vendor.last_body
    assert charged(ledger) == EMBED.cost(8, 0)
    assert_settled_once(ledger)


def test_models_list_passes_through_without_a_price(gw):
    port, ledger, _ = gw
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/openai/v1/models", headers={**QUARTER, "authorization": "Bearer sk"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.read() == Vendor.last_body
    conn.close()
    assert Vendor.seen[-1][1] == "/v1/models" and rows(ledger) == []


# Anthropic


def test_anthropic_messages(gw):
    port, ledger, _ = gw
    body = {"model": "claude-sonnet-4-5", "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hi"}]}
    status, headers, reply = post(port, "/anthropic/v1/messages", body,
                                  {"x-api-key": "sk-ant-x", "anthropic-version": "2023-06-01"})
    assert status == 200 and reply == Vendor.last_body
    assert headers["request-id"] == "req_vendor"
    assert Vendor.seen[-1][2]["x-api-key"] == "sk-ant-x"
    assert charged(ledger) == SONNET.cost(2000, 300)
    assert_settled_once(ledger)


def test_anthropic_stream_is_byte_faithful_and_keeps_pings(gw):
    port, ledger, _ = gw
    body = {"model": "claude-sonnet-4-5", "max_tokens": 1024, "stream": True,
            "messages": [{"role": "user", "content": "Hi"}]}
    status, _, reply = post(port, "/anthropic/v1/messages", body)
    assert status == 200 and reply == Vendor.last_body
    assert b'event: ping\ndata: {"type":"ping"}\n\n' in reply
    assert charged(ledger) == SONNET.cost(2000, 300)  # max of start and delta, not the sum
    assert_settled_once(ledger)


def test_stream_is_not_buffered(gw):
    port, ledger, _ = gw
    Vendor.wait_after_first = threading.Event()
    body = {"model": "claude-sonnet-4-5", "max_tokens": 1024, "stream": True, "messages": []}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/anthropic/v1/messages", json.dumps(body), QUARTER)
    resp = conn.getresponse()
    first = resp.readline()  # arrives while the vendor still holds the rest back
    assert first == b"event: message_start\n"
    Vendor.wait_after_first.set()
    rest = resp.read()
    conn.close()
    assert first + rest == Vendor.last_body
    assert charged(ledger) == SONNET.cost(2000, 300)


def test_anthropic_cached_input_is_charged(gw):
    port, ledger, _ = gw
    Vendor.anthropic_usage = {**ANTHROPIC_USAGE, "input_tokens": 12,
                              "cache_creation_input_tokens": 3000,
                              "cache_read_input_tokens": 40000}
    for stream in (False, True):
        body = {"model": "claude-sonnet-4-5", "max_tokens": 1024, "stream": stream,
                "messages": [{"role": "user", "content": "Hi"}]}
        assert post(port, "/anthropic/v1/messages", body)[0] == 200
    # Cached input is billed by Anthropic; at least the input price, never left out.
    assert charged(ledger) == 2 * SONNET.cost(12 + 3000 + 40000, 300)
    assert_settled_once(ledger, calls=2)


def test_anthropic_cache_tokens_use_cache_prices(gw):
    port, ledger, _ = gw
    Vendor.anthropic_usage = {**ANTHROPIC_USAGE, "input_tokens": 12,
                              "cache_creation_input_tokens": 3000,
                              "cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                                 "ephemeral_1h_input_tokens": 2000},
                              "cache_read_input_tokens": 40000}
    for stream in (False, True):
        body = {"model": "claude-cached", "max_tokens": 1024, "stream": stream,
                "messages": [{"role": "user", "content": "Hi"}]}
        assert post(port, "/anthropic/v1/messages", body)[0] == 200
    one = CACHED.cost(43012, 300, cache_read=40000, cache_write=1000, cache_write_extended=2000)
    assert one == (12 * 3 + 40000 * 0.3 + 1000 * 3.75 + 2000 * 6 + 300 * 15)  # micros
    assert charged(ledger) == 2 * one
    assert_settled_once(ledger, calls=2)


def test_anthropic_cache_write_of_unknown_lifetime_pays_the_longer_price(gw):
    port, ledger, _ = gw
    Vendor.anthropic_usage = {**ANTHROPIC_USAGE, "input_tokens": 0,
                              "cache_creation_input_tokens": 1000}
    body = {"model": "claude-cached", "max_tokens": 1024, "messages": []}
    assert post(port, "/anthropic/v1/messages", body)[0] == 200
    assert charged(ledger) == CACHED.cost(1000, 300, cache_write_extended=1000)


def test_openai_cached_tokens_are_part_of_the_prompt_count():
    usage = {"prompt_tokens": 1000, "completion_tokens": 10,
             "prompt_tokens_details": {"cached_tokens": 800}}
    assert usage_from({"usage": usage}) == {
        "input": 1000, "output": 10, "cache_read": 800, "cache_write": 0,
        "cache_write_extended": 0, "cache_write_total": 0}
    assert usage_from({"usage": {"prompt_tokens": 5, "completion_tokens": 1}}) == {
        "input": 5, "output": 1}


def test_missing_cache_price_falls_back_to_input_price():
    assert SONNET.cost(1000, 0, cache_read=900) == SONNET.cost(1000, 0)
    assert CACHED.cost(1000, 0, cache_read=2000) == 2000 * 0.3  # never below zero uncached


def test_built_in_prices_follow_the_vendor_rate_cards():
    book = PriceBook.load(json.loads(BUILT_IN.read_text()))
    fable = book.price("anthropic", "claude-fable-5-1")
    assert (fable.cache_read, fable.cache_write, fable.cache_write_extended) == (
        to_micros("0.25"), to_micros("12.5"), to_micros(20))
    sol = book.price("openai", "gpt-6.1-sol")
    assert (sol.cache_read, sol.cache_write, sol.cache_write_extended) == (
        to_micros("0.1"), to_micros("2.5"), to_micros(5))


def claude_code_headers(port, auth):
    """What Claude Code sends when started by `quarter-ledger wrap`."""
    env = wrap_env({"QUARTER_AGENT_KEYS": AGENT_KEY}, f"http://127.0.0.1:{port}", "claude-code")
    custom = dict(
        line.split(": ", 1) for line in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    )
    return env["ANTHROPIC_BASE_URL"], {
        "accept": "application/json",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,"
        "fine-grained-tool-streaming-2025-05-14",
        "anthropic-dangerous-direct-browser-access": "true",
        "user-agent": "claude-cli/2.0.31 (external, cli)",
        "x-app": "cli",
        "x-stainless-lang": "js",
        "x-stainless-retry-count": "0",
        "x-stainless-timeout": "600",
        **auth,
        **custom,
    }


@pytest.mark.parametrize(
    "auth", [{"authorization": "Bearer sk-ant-oat01-x"}, {"x-api-key": "sk-ant-api03-x"}]
)
def test_claude_code(gw, auth):
    port, ledger, _ = gw
    base, headers = claude_code_headers(port, auth)
    prefix = base.removeprefix(f"http://127.0.0.1:{port}")
    body = {"model": "claude-sonnet-4-5", "max_tokens": 32000, "stream": True,
            "system": [{"type": "text", "text": "You are Claude Code.",
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "Hi"}], "tools": [],
            "metadata": {"user_id": "user_x"}}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

    # count_tokens is free: no hold, no charge, and not blocked by caps.
    count = {k: v for k, v in body.items() if k in ("model", "messages", "system", "tools")}
    conn.request("POST", prefix + "/v1/messages/count_tokens?beta=true",
                 json.dumps(count), headers)
    resp = conn.getresponse()
    assert resp.status == 200 and resp.read() == b'{"input_tokens":2095}'
    assert rows(ledger) == []

    conn.request("POST", prefix + "/v1/messages?beta=true", json.dumps(body), headers)
    resp = conn.getresponse()
    assert resp.status == 200 and resp.read() == Vendor.last_body
    subscription = "authorization" in auth  # a claude.ai login, not an API key
    assert resp.getheader("x-quarter-billing") == ("subscription" if subscription else None)
    conn.close()  # both calls rode one keep-alive connection

    _method, path, sent, received = Vendor.seen[-1]
    assert path == "/v1/messages?beta=true" and received == body
    assert sent["anthropic-beta"] == headers["anthropic-beta"]
    assert sent["anthropic-version"] == "2023-06-01"
    for name, value in auth.items():
        assert sent[name] == value
    assert sent["anthropic-dangerous-direct-browser-access"] == "true"  # anthropic-* is open
    assert not any(h.startswith(("x-quarter", "x-claude-code", "x-stainless")) for h in sent)
    # The subscription pays for a claude.ai login's calls; an API key is billed per token.
    assert charged(ledger) == (0 if subscription else SONNET.cost(2000, 300))
    assert_settled_once(ledger)
    agents = {r[0] for r in ledger.db.execute("select agent from entries")}
    assert agents == {"claude-code"}


def test_count_tokens_is_free_even_at_the_cap(gw):
    port, ledger, gateway = gw
    gateway.caps[:] = [Cap("agent", "a1", "month", 0)]
    gateway.runaway = None
    for _ in range(3):
        status, _, reply = post(port, "/anthropic/v1/messages/count_tokens",
                                {"model": "claude-sonnet-4-5", "messages": []})
        assert status == 200 and reply == b'{"input_tokens":2095}'
    assert rows(ledger) == []
    status, _, _ = post(port, "/anthropic/v1/messages",
                        {"model": "claude-sonnet-4-5", "max_tokens": 10, "messages": []})
    assert status == 402  # a paid call is still capped


def test_doubled_v1_in_the_path(gw):
    """Vercel AI SDK's Anthropic base URL already ends in /v1."""
    port, ledger, _ = gw
    body = {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": []}
    status, _, reply = post(port, "/anthropic/v1/v1/messages", body)
    assert status == 200 and reply == Vendor.last_body
    assert Vendor.seen[-1][1] == "/v1/messages"
    assert charged(ledger) == SONNET.cost(2000, 300)


# Failures


@pytest.mark.parametrize("code", [429, 500, 400])
def test_vendor_errors_pass_back_unchanged(gw, code):
    port, ledger, _ = gw
    Vendor.error = code
    body = {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": []}
    status, headers, reply = post(port, "/anthropic/v1/messages", body)
    expected_body, expected_headers = ERRORS[code]
    assert status == code and reply == json.dumps(expected_body).encode()
    for name, value in expected_headers.items():
        assert headers[name] == value  # retry-after and request ids reach the SDK
    assert charged(ledger) == 0
    assert_settled_once(ledger)


def test_client_hangs_up_mid_stream(gw):
    port, ledger, _ = gw
    Vendor.wait_after_first = threading.Event()
    body = {"model": "claude-sonnet-4-5", "max_tokens": 1024, "stream": True, "messages": []}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/anthropic/v1/messages", json.dumps(body), QUARTER)
    resp = conn.getresponse()
    assert resp.readline() == b"event: message_start\n"
    conn.sock.shutdown(2)
    conn.close()
    Vendor.wait_after_first.set()
    for _ in range(100):
        if any(k == "release" for _, k, _ in rows(ledger)):
            break
        threading.Event().wait(0.05)
    # Outcome unknown, so the hold is charged in full, once.
    assert_settled_once(ledger)
    assert charged(ledger) == held(ledger)[0]


def test_gzip_upstream_is_decoded(gw):
    port, ledger, _ = gw
    Vendor.gzip_body = True
    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "Hi"}]}
    status, headers, reply = post(port, "/openai/v1/chat/completions", body)
    assert status == 200 and json.loads(reply) == CHAT
    assert reply == gzip.decompress(Vendor.last_body)
    assert "content-encoding" not in headers
    assert charged(ledger) == GPT.cost(1000, 500)  # usage read, not the whole hold
    assert held(ledger) == [GPT.cost(len(json.dumps(body)), DEFAULT_MAX_TOKENS)]
    assert_settled_once(ledger)


def test_new_anthropic_and_openai_headers_reach_the_vendor(gw):
    port, _, _ = gw
    extra = {"anthropic-workspace-id": "wrkspc_1", "anthropic-new-capability": "x",
             "openai-organization": "org_1", "openai-project": "proj_1",
             "x-claude-code-session-id": "s1", "cookie": "secret"}
    body = {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": []}
    assert post(port, "/anthropic/v1/messages", body, extra)[0] == 200
    sent = Vendor.seen[-1][2]
    for name in ("anthropic-workspace-id", "anthropic-new-capability",
                 "openai-organization", "openai-project"):
        assert sent[name] == extra[name]
    assert "cookie" not in sent and "x-claude-code-session-id" not in sent
