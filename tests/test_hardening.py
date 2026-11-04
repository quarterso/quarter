"""Regression tests for the security and money-path review."""

import http.client
import json
import os
import socket
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, FakeVendor, call, start

from quarter_ledger import cli, dashboard, proxy
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.slack import Slack
from quarter_ledger.store import Ledger


def open_holds(ledger: Ledger) -> int:
    return ledger.db.execute(
        "select count(*) from entries h where kind = 'hold' and not exists"
        " (select 1 from entries r where r.call_id = h.call_id and r.kind = 'release')"
    ).fetchone()[0]


@pytest.fixture
def gw(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls, FakeVendor.auth = [], {}
    FakeVendor.usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    ledger = Ledger(str(tmp_path / "ledger.db"))
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.02"))]
    base = f"http://127.0.0.1:{vendor.server_port}"
    gateway = Gateway(
        ledger, caps, PRICES, {"fake": base}, {AGENT_KEY}, ADMIN_KEY,
        vendor_keys={"fake": "sk-real"}, signing_key="s" * 32,
    )
    server = start(serve(gateway, port=0))
    yield f"http://127.0.0.1:{server.server_port}", ledger, gateway, server
    server.shutdown()
    vendor.shutdown()


def raw_exchange(port: int, data: bytes, wait: float = 2.0) -> bytes:
    """Send bytes on one connection and read until the server closes it or goes quiet."""
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.sendall(data)
        out = b""
        try:
            while chunk := s.recv(65536):
                out += chunk
        except TimeoutError:
            pass
    return out


# A vendor redirect must not carry the vendor key to another host.


class Elsewhere(BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def do_GET(self):
        Elsewhere.seen.append(dict(self.headers))
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def test_vendor_redirect_is_relayed_not_followed(tmp_path):
    elsewhere = start(ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere))
    Elsewhere.seen = []

    class Redirecting(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(302)  # urllib follows a 302 as a GET, headers and all
            self.send_header("location", f"http://127.0.0.1:{elsewhere.server_port}/steal")
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), Redirecting))
    ledger = Ledger(str(tmp_path / "l.db"))
    base = f"http://127.0.0.1:{vendor.server_port}"
    gateway = Gateway(ledger, [], PRICES, {"fake": base}, {AGENT_KEY}, vendor_keys={"fake": "sk-real"})
    server = start(serve(gateway, port=0))
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/fake/v1/chat", b'{"model": "m"}', {"authorization": f"Bearer {AGENT_KEY}"})
        reply = conn.getresponse()
        assert reply.status == 302 and reply.getheader("location").endswith("/steal")
        conn.close()
        assert Elsewhere.seen == []  # the real key never left for the other host
        assert open_holds(ledger) == 0 and ledger.totals("agent") == []
    finally:
        server.shutdown()
        vendor.shutdown()
        elsewhere.shutdown()


def test_gateway_repr_shows_no_secret():
    secrets = {"agent": "agent-key-1", "admin": "admin-key-1", "hook": "https://hooks.example/T/B/x",
               "sign": "signing-key-1", "vendor": "sk-vendor-1"}
    gateway = Gateway(
        Ledger(":memory:"), [], PRICES, {}, {secrets["agent"]}, secrets["admin"],
        approval_webhook=secrets["hook"], signing_key=secrets["sign"],
        key_agents={secrets["agent"]: "bot"}, vendor_keys={"fake": secrets["vendor"]},
    )
    shown = repr(gateway)
    assert not [name for name, value in secrets.items() if value in shown]


# Keys and signatures with bytes outside ASCII are refused, not a crash.


def test_non_ascii_keys_get_401_not_a_dropped_connection(gw):
    _, _, _, server = gw
    for header in (b"x-quarter-key: \xe9\xe9", b"authorization: Bearer \xe9", b"x-api-key: \xe9"):
        reply = raw_exchange(
            server.server_port,
            b"POST /fake/v1/chat HTTP/1.1\r\nhost: x\r\ncontent-length: 2\r\n"
            + header + b"\r\nconnection: close\r\n\r\n{}",
        )
        assert reply.startswith(b"HTTP/1.1 401"), (header, reply[:80])
    reply = raw_exchange(
        server.server_port,
        b"GET /approvals HTTP/1.1\r\nhost: x\r\nx-quarter-key: \xe9\r\nconnection: close\r\n\r\n",
    )
    assert reply.startswith(b"HTTP/1.1 401")


def test_slack_signature_with_non_ascii_is_refused():
    slack = Slack("t", "secret", "C1", {})
    assert slack.verified(str(int(time.time())), "v0=\xe9", b"payload=") is False


# x-quarter-wait: nan used to spin a thread on the database forever.


def test_wait_that_is_not_a_finite_number_is_refused(gw):
    url, ledger, _, _ = gw
    big = {"model": "m", "max_tokens": 12_000}
    for value in ("nan", "inf", "-nan"):
        started = time.monotonic()
        req = urllib.request.Request(
            url + "/fake/v1/chat", json.dumps(big).encode(), {**HEADERS, "x-quarter-wait": value}
        )
        with pytest.raises(urllib.error.HTTPError) as reply:
            urllib.request.urlopen(req, timeout=5)
        assert reply.value.code == 400 and time.monotonic() - started < 5, value
    assert ledger.approvals() == []


# A settle may never record a negative charge.


def test_settle_refuses_a_negative_amount(gw):
    url, ledger, _, _ = gw
    status, body = call(url + "/authorize", {"rail": "x402", "payee": "0xP", "amount_usd": "0.01"})
    assert status == 200
    call_id = json.loads(body)["call_id"]
    status, _ = call(url + "/settle", {"call_id": call_id, "amount_micros": -5_000_000})
    assert status == 400
    assert ledger.spent("agent", "a1", None) == to_micros("0.01")  # still the hold
    with pytest.raises(ValueError):
        ledger.settle(call_id, -1)
    assert call(url + "/settle", {"call_id": call_id, "amount_usd": "0.01"})[0] == 200
    assert ledger.spent("agent", "a1", None) == to_micros("0.01")


def test_an_amount_too_large_to_store_is_a_clean_400(gw):
    url, _, gateway, _ = gw
    gateway.caps = []  # no cap to refuse it first
    for pay in ({"amount_usd": "1e30"}, {"amount_micros": 2**63}):
        assert call(url + "/authorize", {"rail": "x402", "payee": "0xP", **pay})[0] == 400
    body = call(url + "/authorize", {"rail": "x402", "payee": "0xP", "amount_usd": "0.01"})[1]
    call_id = json.loads(body)["call_id"]
    assert call(url + "/settle", {"call_id": call_id, "amount_micros": 2**63})[0] == 400
    assert call(url + "/settle", {"call_id": call_id, "amount_usd": "0.01"})[0] == 200


# A failure inside settle must still close the hold.


def test_hold_is_closed_when_recording_the_charge_fails(gw, monkeypatch):
    url, ledger, _, _ = gw
    real, failures = Ledger.settle, []

    def flaky(self, call_id, actual):
        if not failures:
            failures.append(call_id)
            raise sqlite3.OperationalError("database is locked")
        return real(self, call_id, actual)

    monkeypatch.setattr(Ledger, "settle", flaky)
    try:
        call(url + "/fake/v1/chat", {"model": "m", "max_tokens": 100})
    except OSError:
        pass  # the agent may see the connection drop
    assert failures and open_holds(ledger) == 0
    assert ledger.spent("agent", "a1", None) > 0  # charged in full: outcome unknown


# Bodies the gateway does not read must not become the next request.


def test_chunked_body_is_refused_and_cannot_smuggle_a_request(gw):
    _, _, _, server = gw
    smuggled = b"GET /healthz HTTP/1.1\r\nhost: x\r\n\r\n"
    body = b"%x\r\n%s\r\n0\r\n\r\n" % (len(smuggled), smuggled)
    reply = raw_exchange(
        server.server_port,
        b"POST /fake/v1/chat HTTP/1.1\r\nhost: x\r\nx-quarter-key: " + AGENT_KEY.encode()
        + b"\r\ntransfer-encoding: chunked\r\n\r\n" + body,
    )
    assert reply.startswith(b"HTTP/1.1 411") and reply.count(b"HTTP/1.1 ") == 1
    assert FakeVendor.calls == []


def test_get_with_a_body_cannot_smuggle_a_request(gw):
    _, _, _, server = gw
    smuggled = b"GET /healthz HTTP/1.1\r\nhost: x\r\n\r\n"
    reply = raw_exchange(
        server.server_port,
        b"GET /healthz HTTP/1.1\r\nhost: x\r\ncontent-length: %d\r\n\r\n%s" % (len(smuggled), smuggled),
    )
    assert reply.count(b"HTTP/1.1 200") == 1


def test_idle_client_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "CLIENT_TIMEOUT", 0.5)
    server = start(serve(Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {}), port=0))
    try:
        started = time.monotonic()
        raw_exchange(server.server_port, b"GET /healthz HTTP/1.1\r\n", wait=10)
        assert time.monotonic() - started < 5
    finally:
        server.shutdown()


# An encoded ../ must not climb out of an upstream's base path.


def test_encoded_dot_segments_are_refused(gw):
    url, _, _, _ = gw
    for path in ("/fake/%2e%2e/x", "/fake/v1/%2E%2E%2Fadmin", "/fake/.%2e/x"):
        assert call(url + path, {"model": "m"})[0] == 400, path
    assert FakeVendor.calls == []


# The dashboard's Resume button must post to that agent, whatever its name.


def test_resume_button_cannot_be_aimed_at_another_route(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.pause("../approvals/apr_0123456789abcdef/approve?", "loop")
    page = dashboard.render(ledger, [], "Ann (approver)", True)
    assert "/agents/../" not in page and "approve?/resume" not in page
    assert "/agents/..%2Fapprovals%2Fapr_0123456789abcdef%2Fapprove%3F/resume" in page


def test_agent_with_a_slash_in_its_name_can_be_resumed(gw):
    url, ledger, _, _ = gw
    ledger.pause("team/a b", "loop")
    status, _ = call(url + "/agents/team%2Fa%20b/resume", {}, {"x-quarter-key": ADMIN_KEY})
    assert status == 200 and ledger.paused("team/a b") is None


# An approval is spent only by a call it paid for.


def test_approval_survives_a_retry_that_a_cap_blocked(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [Cap("agent", "*", "day", to_micros(1), approve_above=to_micros("0.10"))]
    ids = {"agent": "a1", "task": None, "customer": None}
    decision, apr = ledger.hold(caps, ids, "fake", "m", to_micros("0.5"))
    assert decision.outcome == "approve"
    ledger.decide_approval(apr, True, by="Ann")
    for _ in range(9):  # fill the cap with calls still in flight
        ledger.hold(caps, ids, "fake", "m", to_micros("0.06"))
    decision, _ = ledger.hold(caps, ids, "fake", "m", to_micros("0.5"), apr)
    assert decision.outcome == "block"
    assert ledger.approval(apr)["status"] == "approved"
    for (call_id,) in ledger.db.execute("select call_id from entries where kind = 'hold'").fetchall():
        ledger.settle(call_id, None)
    decision, _ = ledger.hold(caps, ids, "fake", "m", to_micros("0.5"), apr)
    assert decision.outcome == "allow" and ledger.approval(apr)["status"] == "used"


# Files that hold keys or the books are private to their owner.


def private(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_ledger_files_are_private(tmp_path):
    old = os.umask(0o022)
    try:
        ledger = Ledger(str(tmp_path / "ledger.db"))
        ledger.audit("x", None)
    finally:
        os.umask(old)
    assert private(tmp_path / "ledger.db") and private(tmp_path / "ledger.db-wal")


def test_init_never_writes_keys_to_a_readable_file(tmp_path, monkeypatch):
    seen = []
    real = Path.write_text

    def spy(self, text, *a, **k):
        result = real(self, text, *a, **k)
        if "QUARTER_ADMIN_KEY" in text:
            seen.append(stat.S_IMODE(self.stat().st_mode))
        return result

    monkeypatch.setattr(Path, "write_text", spy)
    old = os.umask(0o022)
    try:
        cli.init(tmp_path, "100", "5")
    finally:
        os.umask(old)
    assert all(mode & 0o077 == 0 for mode in seen)
    assert private(tmp_path / ".env") and "QUARTER_ADMIN_KEY=" in (tmp_path / ".env").read_text()
    with pytest.raises(SystemExit):
        cli.init(tmp_path, "100", "5")


# The chain check and the backup, from the command line.


def test_verify_chain_command_exits_1_on_a_tampered_ledger(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    ids = {"agent": "a1", "task": None, "customer": None}
    _, call_id = ledger.hold([], ids, "fake", "m", 100)
    ledger.settle(call_id, 80)
    cli.main(["--db", db, "verify-chain"])
    ledger.db.execute("update entries set amount = 1 where kind = 'charge'")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--db", db, "verify-chain"])
    assert exit_.value.code == 1


def test_verify_chain_refuses_a_missing_ledger(tmp_path):
    typo = tmp_path / "lerdger.db"
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--db", str(typo), "verify-chain"])
    assert "no ledger" in str(exit_.value.code) and not typo.exists()


def test_backup_is_a_consistent_private_copy(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    ids = {"agent": "a1", "task": None, "customer": None}
    for _ in range(3):
        _, call_id = ledger.hold([], ids, "fake", "m", 100)
        ledger.settle(call_id, 80)
    ledger.audit("x", None)
    stop = threading.Event()

    def busy():  # writes keep landing while the backup runs
        writer = Ledger(db)
        while not stop.wait(0.001):
            writer.audit("busy", None)

    thread = threading.Thread(target=busy)
    thread.start()
    try:
        cli.main(["--db", db, "backup", str(tmp_path / "copy.db")])
    finally:
        stop.set()
        thread.join()
    copy = Ledger(str(tmp_path / "copy.db"))
    assert private(tmp_path / "copy.db")
    assert all(t["intact"] for t in copy.verify_chain().values())
    assert copy.totals("agent") == ledger.totals("agent")
    with pytest.raises(SystemExit):  # never overwrites
        cli.main(["--db", db, "backup", str(tmp_path / "copy.db")])
    cli.main(["--db", db, "backup", str(tmp_path / "backups" / "copy.db")])  # makes the folder
    assert private(tmp_path / "backups" / "copy.db")


# Behind Cloudflare a request is cut at about 125 s, so QUARTER_MAX_WAIT caps x-quarter-wait.


def test_wait_is_capped_at_max_wait(gw):
    url, ledger, gateway, _ = gw
    gateway.max_wait = 1
    started = time.monotonic()
    req = urllib.request.Request(
        url + "/fake/v1/chat", json.dumps({"model": "m", "max_tokens": 12_000}).encode(),
        {**HEADERS, "x-quarter-wait": "30"},
    )
    with pytest.raises(urllib.error.HTTPError) as reply:
        urllib.request.urlopen(req, timeout=10)
    assert reply.value.code == 403 and json.loads(reply.value.read())["error"] == "approval_required"
    assert time.monotonic() - started < 5
    assert [a["status"] for a in ledger.approvals()] == ["pending"]


# A caller with no valid key cannot make the gateway read a large body.


def test_large_body_without_a_key_is_refused_unread(gw):
    url, _, _, server = gw
    with socket.create_connection(("127.0.0.1", server.server_port)) as s:
        s.sendall(b"POST /fake/v1/chat/completions HTTP/1.1\r\nHost: x\r\nx-quarter-key: guess\r\n"
                  b"Content-Length: 8000000\r\n\r\n")
        s.settimeout(5)
        assert s.recv(100).startswith(b"HTTP/1.1 401")  # answered before a byte of the body
    big = {"model": "m", "messages": [{"role": "user", "content": "x" * 100_000}]}
    assert call(url + "/fake/v1/chat/completions", big, HEADERS)[0] == 403  # read: held for approval


# A slow client cannot hold a connection open by sending a byte at a time, and past the
# connection limit new ones are closed at once instead of each taking a thread.


def test_a_trickling_client_is_cut_at_the_request_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "REQUEST_DEADLINE", 2)
    gateway = Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY)
    server = start(serve(gateway, port=0))
    with socket.create_connection(("127.0.0.1", server.server_port)) as s:
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")
        s.settimeout(1)
        start_at, closed = time.monotonic(), False
        while time.monotonic() - start_at < 6 and not closed:
            try:
                s.sendall(b"X")
                closed = s.recv(1) == b""
            except TimeoutError:
                continue
            except OSError:
                closed = True
        assert closed and time.monotonic() - start_at < 5
    assert urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz").status == 200
    server.shutdown()


def test_connections_past_the_limit_are_closed_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "MAX_CONNECTIONS", 3)
    gateway = Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY)
    server = start(serve(gateway, port=0))
    held = [socket.create_connection(("127.0.0.1", server.server_port)) for _ in range(3)]
    time.sleep(0.2)
    with socket.create_connection(("127.0.0.1", server.server_port)) as extra:
        extra.settimeout(2)
        try:
            assert extra.recv(1) == b""
        except ConnectionResetError:
            pass
    for s in held:
        s.close()
    time.sleep(0.2)
    assert urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz").status == 200
    server.shutdown()


def test_cli_approve_says_who_decided(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    caps = [Cap("agent", "*", "day", to_micros(1), approve_above=to_micros("0.10"))]
    _, apr = ledger.hold(caps, {"agent": "a1", "task": None, "customer": None}, "fake", "m", to_micros("0.5"))
    monkeypatch.delenv("QUARTER_SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as done:
        cli.main(["--db", db, "approve", apr, "--as", "Ann"])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == f"{apr} approved by Ann"
