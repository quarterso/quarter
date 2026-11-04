import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from quarter_ledger import cli
from quarter_ledger.failopen import FailOpenLog, ingest
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.pricing import Price, PriceBook
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.register import RegisterRule
from quarter_ledger.rules import Rules, ToolRule
from quarter_ledger.store import Ledger

PRICES = PriceBook({"fake": {"m": Price(to_micros(1), to_micros(2))}})  # one micro-dollar a token in
BODY = json.dumps({"model": "m", "max_tokens": 10}).encode()
HELD = len(BODY) + 2 * 10
CHARGE = 10 + 2 * 5


class Vendor(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls: ClassVar[int] = 0

    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        Vendor.calls += 1
        reply = json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def ledger_down(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


@pytest.fixture
def run(tmp_path, monkeypatch):
    Vendor.calls = 0
    vendor = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
    threading.Thread(target=vendor.serve_forever, daemon=True).start()
    servers = [vendor]

    def start(fail_open=True, down=True, **kw):
        ledger = Ledger(str(tmp_path / "ledger.db"))
        log = FailOpenLog(kw.pop("log", str(tmp_path / "ledger.db.fail-open.jsonl"))) if fail_open else None
        gw = Gateway(ledger, kw.pop("caps", []), PRICES, {"fake": f"http://127.0.0.1:{vendor.server_port}"},
                     fail_open=log, **kw)
        if down:
            monkeypatch.setattr(ledger, "hold", ledger_down)
        proxy = serve(gw, port=0)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        servers.append(proxy)
        return f"http://127.0.0.1:{proxy.server_port}", gw

    yield start
    for s in servers:
        s.shutdown()


def call(base, path="/fake/v1/chat/completions", headers=None, body=BODY):
    req = urllib.request.Request(base + path, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def lines(gw):
    with open(gw.fail_open.path) as fh:
        return [json.loads(line) for line in fh]


def test_default_is_closed(run):
    base, _ = run(fail_open=False)
    status, _, body = call(base)
    assert status == 503 and json.loads(body)["error"] == "ledger_unavailable"
    assert Vendor.calls == 0


def test_plain_model_call_passes_and_is_logged(run):
    base, gw = run()
    status, headers, _ = call(base)
    assert status == 200 and headers["x-quarter-fail-open"] == "model"
    assert Vendor.calls == 1
    passed, settled = lines(gw)
    assert passed["held"] == HELD and passed["agent"] == "default" and "locked" in passed["reason"]
    assert settled == {"id": passed["id"], "charged": CHARGE}
    assert headers["x-quarter-call"] == passed["id"]


def test_ledger_up_writes_no_fallback(run):
    base, gw = run(down=False)
    assert call(base)[0] == 200
    assert gw.ledger.spent("all", "all", None) == CHARGE
    with pytest.raises(FileNotFoundError):
        lines(gw)


@pytest.mark.parametrize("setting", ["tool rule", "approval line", "approval header", "register"])
def test_anything_that_could_ask_a_person_stays_closed(run, setting):
    kw, headers = {}, {}
    if setting == "tool rule":
        kw["rules"] = Rules(tools=[ToolRule("bash", "approve")])
    if setting == "approval line":
        kw["caps"] = [Cap("all", "*", "month", to_micros(100), approve_above=HELD - 1)]
    if setting == "approval header":
        headers["x-quarter-approval"] = "apr_x"
    base, gw = run(**kw)
    if setting == "register":
        gw.ledger.register = RegisterRule()
    status, _, _ = call(base, headers=headers)
    assert status == 503 and Vendor.calls == 0


def test_payments_stay_closed(run):
    base, gw = run(signing_key="k" * 32)
    payment = json.dumps({"amount_usd": "1", "rail": "x402", "payee": "0xabc"}).encode()
    with pytest.raises((urllib.error.URLError, ConnectionError)):  # refused by hanging up, as before
        call(base, "/authorize", body=payment)
    with pytest.raises(FileNotFoundError):
        lines(gw)


def test_unwritable_fallback_stays_closed(run, tmp_path):
    base, _ = run(log=str(tmp_path / "missing" / "log.jsonl"))
    assert call(base)[0] == 503 and Vendor.calls == 0


def test_ingest_books_each_call_once_and_never_undercounts(run, tmp_path):
    base, gw = run()
    call(base)
    gw.fail_open.passed({"agent": "a2"}, "fake", "m", 500, "disk I/O error")  # sent, never settled
    ledger = Ledger(str(tmp_path / "ledger.db"))
    result = ingest(ledger, gw.fail_open.path)
    assert result["booked"] == 2 and result["charged_in_full"] == 1
    assert ledger.spent("all", "all", None) == CHARGE + 500
    audit = [json.loads(r["detail"]) for r in ledger.audit_log() if r["action"] == "fail_open"]
    assert sorted(a["charged"] for a in audit) == [CHARGE, 500]
    assert ingest(ledger, result["moved_to"])["booked"] == 0  # the same file again books nothing
    assert ledger.spent("all", "all", None) == CHARGE + 500
    assert all(t["intact"] for t in ledger.verify_chain().values())


def test_serve_setting(monkeypatch):
    monkeypatch.setenv("QUARTER_FAIL_OPEN", "everything")
    with pytest.raises(SystemExit, match="QUARTER_FAIL_OPEN"):
        cli.main(["--db", ":memory:", "serve"])
