import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, FakeVendor, start

from quarter_ledger import dashboard, status
from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger


@pytest.fixture
def gateway(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    ledger = Ledger(str(tmp_path / "ledger.db"))
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros(20))]
    upstreams = {"fake": f"http://127.0.0.1:{vendor.server_port}"}
    gw = Gateway(ledger, caps, PRICES, upstreams, {AGENT_KEY}, ADMIN_KEY, signing_key="k" * 32)
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", ledger
    proxy.shutdown()
    vendor.shutdown()


def send(url, body, headers=HEADERS):
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def model_call(url, headers=HEADERS):
    return send(url + "/fake/v1/chat", {"model": "m", "max_tokens": 10}, headers)


def pay(url, usd, headers=HEADERS):
    return send(url + "/authorize", {"rail": "x402", "payee": "0x" + "1" * 40, "amount_usd": usd}, headers)


def month() -> str:
    return f"{datetime.now(UTC):%Y-%m}"


def noted(ledger):
    return [a for a in ledger.audit_log() if a["action"] == "free_limit"]


def test_unset_counts_nothing_and_marks_nothing(gateway):
    url, ledger = gateway
    assert model_call(url)[2]["x-quarter-free-limit"] is None
    assert ledger.decisions(month()) == 0 and not ledger.over_free_limit()


def test_past_the_limit_calls_are_still_decided_and_say_so(gateway):
    url, ledger = gateway
    ledger.free_decisions = 2
    for _ in range(2):
        code, _, headers = model_call(url)
        assert code == 200 and headers["x-quarter-free-limit"] is None
    code, _, headers = model_call(url)
    assert code == 200 and headers["x-quarter-free-limit"] == "exceeded"
    code, body, headers = pay(url, "1")
    assert code == 200 and body["authorisation"] and headers["x-quarter-free-limit"] == "exceeded"
    assert ledger.decisions(month()) == 4
    assert [a["detail"] for a in noted(ledger)] == [month()]  # once a month, however far past


def test_a_retry_with_its_approval_is_not_counted_again(gateway):
    url, ledger = gateway
    ledger.free_decisions = 10
    code, body, _ = pay(url, "50")  # over the approval line
    assert code == 403 and body["error"] == "approval_required"
    ledger.decide_approval(body["approval_id"], True, "dana")
    code, _, _ = pay(url, "50", {**HEADERS, "x-quarter-approval": body["approval_id"]})
    assert code == 200 and ledger.decisions(month()) == 1


def test_a_lower_limit_mid_month_is_still_noted(gateway):
    url, ledger = gateway
    ledger.free_decisions = 5
    for _ in range(3):
        model_call(url)
    ledger.free_decisions = 1
    assert model_call(url)[2]["x-quarter-free-limit"] == "exceeded" and len(noted(ledger)) == 1


def test_status_and_dashboard_show_the_count(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "l.db")
    monkeypatch.setenv("QUARTER_FREE_DECISIONS", "1")
    main(["--db", db, "status"])
    assert "Decisions this month: 0 of 1 free" in capsys.readouterr().out
    ledger = Ledger(db)
    ledger.free_decisions = 1
    ids = {"agent": "a", "task": None, "customer": None}
    for _ in range(2):
        ledger.hold([], ids, "fake", "m", 1)
    out = status.render(ledger, [])
    assert "Decisions this month: 2 of 1 free" in out and "Over the free limit" in out
    assert "Over the free limit." in dashboard.render(ledger, [])
    ledger.free_decisions = None
    assert "Decisions this month" not in status.render(ledger, [])
    assert "Over the free limit" not in dashboard.render(ledger, [])


def test_a_bad_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("QUARTER_FREE_DECISIONS", "lots")
    with pytest.raises(SystemExit, match="whole number of decisions"):
        main(["--db", str(tmp_path / "l.db"), "status"])
