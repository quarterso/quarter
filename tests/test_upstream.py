import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from quarter_ledger import upstream
from quarter_ledger.money import to_micros
from quarter_ledger.pricing import Price, PriceBook
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger
from quarter_ledger.upstream import Pool, Sent, Unsent


class Vendor(BaseHTTPRequestHandler):
    """/ok answers; /reset answers, then resets the connection; /swallow reads the request
    and hangs up without a reply; /stream sends three chunked events."""

    protocol_version = "HTTP/1.1"
    connections: ClassVar[list] = []
    requests: ClassVar[list] = []
    release: ClassVar[threading.Event] = threading.Event()

    def setup(self):
        super().setup()
        Vendor.connections.append(self.client_address)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        Vendor.requests.append((self.path, body))
        if self.path == "/swallow":
            self.close_connection = True
            return
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for n in range(3):
                event = f"data: {json.dumps({'n': n})}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                self.wfile.flush()
                if n == 0:
                    Vendor.release.wait(5)
            self.wfile.write(b"0\r\n\r\n")
            return
        reply = json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)
        if self.path == "/reset":  # the vendor drops it while it sits idle, with a reset
            self.wfile.flush()
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    Vendor.connections, Vendor.requests = [], []
    Vendor.release = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def post(pool, url, body=b"{}"):
    with pool.open("POST", url, body, {"content-type": "application/json"}) as resp:
        return resp.status, resp.read()


def idle(pool):
    return sum(len(v) for v in pool._idle.values())


def test_reuses_one_connection(vendor):
    pool = Pool()
    for _ in range(5):
        assert post(pool, vendor + "/ok")[0] == 200
    assert pool.connects == 1
    assert len(Vendor.connections) == 1


def test_never_shares_between_hosts(vendor):
    pool = Pool()
    post(pool, vendor + "/ok")
    post(pool, vendor.replace("127.0.0.1", "localhost") + "/ok")
    assert pool.connects == 2


def test_closed_idle_connection_is_dropped_before_use(vendor):
    pool = Pool()
    post(pool, vendor + "/reset")
    time.sleep(0.1)
    assert post(pool, vendor + "/ok")[0] == 200
    assert pool.connects == 2
    assert [p for p, _ in Vendor.requests] == ["/reset", "/ok"]  # nothing sent twice


def test_stale_socket_is_retried_once_on_a_new_connection(vendor, monkeypatch):
    """The vendor resets an idle connection just after the liveness check: the send fails
    before a byte is written, so the call goes once more, on a new connection."""
    monkeypatch.setattr(upstream, "_alive", lambda conn: True)
    pool = Pool()
    with pool.open("POST", vendor + "/reset", b"{}", {}) as a, pool.open("POST", vendor + "/reset", b"{}", {}) as b:
        a.read(), b.read()
    time.sleep(0.2)  # both resets have arrived
    assert idle(pool) == 2 and pool.connects == 2
    assert post(pool, vendor + "/ok", b'{"n": 1}')[0] == 200
    assert pool.connects == 3  # one retry, on a new connection, not the second stale one
    assert [b for p, b in Vendor.requests if p == "/ok"] == [b'{"n": 1}']


def test_no_retry_once_the_request_was_delivered(vendor):
    pool = Pool()
    post(pool, vendor + "/ok")
    with pytest.raises(Sent):
        post(pool, vendor + "/swallow", b'{"pay": 1}')
    assert pool.connects == 1  # no new connection was made to send it again
    assert [p for p, _ in Vendor.requests].count("/swallow") == 1
    assert idle(pool) == 0


def test_unreachable_is_unsent_and_not_retried():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pool = Pool()
    with pytest.raises(Unsent):
        post(pool, f"http://127.0.0.1:{port}/ok")
    assert pool.connects == 1


def test_stream_returns_its_connection_only_when_read_to_the_end(vendor):
    pool = Pool()
    with pool.open("POST", vendor + "/stream", b"{}", {}) as resp:
        first = resp.readline()
        assert first.startswith(b"data:")
        assert idle(pool) == 0
        Vendor.release.set()
        rest = list(resp)
    assert len([line for line in rest if line.startswith(b"data:")]) == 2
    assert idle(pool) == 1
    post(pool, vendor + "/ok")
    assert pool.connects == 1


def test_stream_left_early_is_closed(vendor):
    pool = Pool()
    with pytest.raises(RuntimeError), pool.open("POST", vendor + "/stream", b"{}", {}) as resp:
        resp.readline()
        raise RuntimeError("the agent hung up")
    Vendor.release.set()
    assert idle(pool) == 0


def test_pool_bound(vendor):
    pool = Pool(size=2)
    opened = [pool.open("POST", vendor + "/ok", b"{}", {}) for _ in range(4)]
    for cm in opened:
        cm.__enter__().read()
    for cm in opened:
        cm.__exit__(None, None, None)
    assert pool.connects == 4
    assert idle(pool) == 2


def test_idle_timeout(vendor):
    pool = Pool(idle=0.05)
    post(pool, vendor + "/ok")
    time.sleep(0.1)
    post(pool, vendor + "/ok")
    assert pool.connects == 2


def test_gateway_charges_in_full_and_answers_502_when_the_reply_is_lost(vendor, tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    prices = PriceBook({"fake": {"m": Price(to_micros(1), to_micros(2))}})
    gw = Gateway(ledger, [], prices, {"fake": vendor})
    proxy = serve(gw, port=0)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{proxy.server_port}/fake"
    body = json.dumps({"model": "m", "max_tokens": 10}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/ok", data=body)).read()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/swallow", data=body))
        assert e.value.code == 502 and b"not retried" in e.value.read()
    finally:
        proxy.shutdown()
    held = len(body) + 2 * 10  # $1 per million in, $2 out: in micro-dollars, one each
    assert ledger.spent("all", "all", None) == 10 + 2 * 5 + held
    assert gw.vendors.connects == 1
