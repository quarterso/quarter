import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from test_proxy import ADMIN_KEY, AGENT_KEY, call, start

from quarter_ledger import status
from quarter_ledger.money import to_micros
from quarter_ledger.pricing import Price, PriceBook
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

# $10 in, $50 out, cache writes $12.50, reads $1 per million tokens
PRICES = PriceBook({"anthropic": {"m": Price(to_micros(10), to_micros(50), cache_write_per_million=to_micros("12.5"),
                                               cache_read_per_million=to_micros(1))}})
HEADERS = {"x-quarter-key": AGENT_KEY, "x-quarter-agent": "a1"}


class Anthropic(BaseHTTPRequestHandler):
    """Reports a cache read whenever the request asked for caching."""

    protocol_version = "HTTP/1.1"
    seen: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Anthropic.seen.append(body)
        cached = "cache_control" in json.dumps(body)
        usage = {"input_tokens": 100, "output_tokens": 10,
                 "cache_read_input_tokens": 90_000 if cached else 0, "cache_creation_input_tokens": 0}
        data = json.dumps({"content": [{"type": "text", "text": "ok"}], "usage": usage}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def gateway(tmp_path, **options):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), Anthropic))
    Anthropic.seen = []
    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {"anthropic": f"http://127.0.0.1:{vendor.server_port}"}, {AGENT_KEY},
                 ADMIN_KEY, **options)
    proxy = start(serve(gw, port=0))
    return f"http://127.0.0.1:{proxy.server_port}/anthropic/v1/messages", ledger, (proxy, vendor)


TURNS = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
         {"role": "user", "content": "go on"}]


def test_a_conversation_without_a_cache_marker_gets_one_and_the_saving_is_kept(tmp_path):
    url, ledger, servers = gateway(tmp_path)
    try:
        assert call(url, {"model": "m", "max_tokens": 10, "messages": TURNS[:1]}, HEADERS)[0] == 200
        assert "cache_control" not in Anthropic.seen[-1]  # a one-off call would pay the write for nothing
        assert call(url, {"model": "m", "max_tokens": 10, "messages": TURNS}, HEADERS)[0] == 200
        assert Anthropic.seen[-1]["cache_control"] == {"type": "ephemeral"}
        # 90,000 tokens read at $1 instead of $10 a million: $0.81 saved.
        assert ledger.saved_since(datetime(2000, 1, 1, tzinfo=UTC)) == (to_micros("0.81"), 1)
        assert "Prompt caching Quarter added saved $0.81 this month, on 1 calls" in status.render(ledger, [])
    finally:
        for server in servers:
            server.shutdown()


def test_a_call_with_its_own_marker_or_asking_not_to_is_left_alone(tmp_path):
    url, ledger, servers = gateway(tmp_path)
    try:
        own = [*TURNS[:2], {"role": "user", "content": [
            {"type": "text", "text": "go on", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}]
        call(url, {"model": "m", "max_tokens": 10, "messages": own}, HEADERS)
        assert "cache_control" not in Anthropic.seen[-1]  # its own 1h marker stays the only one
        call(url, {"model": "m", "max_tokens": 10, "messages": TURNS}, {**HEADERS, "x-quarter-cache": "off"})
        assert "cache_control" not in Anthropic.seen[-1]
        assert ledger.saved_since(datetime(2000, 1, 1, tzinfo=UTC)) == (0, 0)
    finally:
        for server in servers:
            server.shutdown()


def test_turned_off_for_the_whole_gateway(tmp_path):
    url, _, servers = gateway(tmp_path, auto_cache=False)
    try:
        call(url, {"model": "m", "max_tokens": 10, "messages": TURNS}, HEADERS)
        assert "cache_control" not in Anthropic.seen[-1]
    finally:
        for server in servers:
            server.shutdown()
