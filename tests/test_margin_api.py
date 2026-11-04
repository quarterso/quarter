"""GET /margin, read by the Stripe App from a sandboxed frame, and the read-only keys it holds."""

import json
import urllib.error
import urllib.request

import pytest
from test_margin import FakeStripe, stripe_url  # noqa: F401  (stripe_url is a fixture)
from test_proxy import ADMIN_KEY, AGENT_KEY, PRICES, start

from quarter_ledger import connect, margin
from quarter_ledger.connect import Observed
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

PEOPLE = {
    "Vic": {"key": "viewer-key", "role": "viewer"},
    "Fay": {"key": "finance-key", "role": "finance"},
    "App": {"key": "stripe-app-key", "role": "finance", "read_only": True},
}


def ask(url, key=None, method="GET"):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                 headers={"x-quarter-key": key} if key else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.fixture
def gateway(tmp_path, monkeypatch, stripe_url):  # noqa: F811
    monkeypatch.setattr("quarter_ledger.proxy.stripe_revenue", lambda key, m: margin.stripe_revenue(key, m, stripe_url))
    monkeypatch.setattr("quarter_ledger.proxy.stripe_customer_named",
                        lambda key, cid: margin.stripe_customer_named(key, cid, stripe_url))
    ledger = Ledger(str(tmp_path / "l.db"))
    connect.book(ledger, "csv", [
        Observed("o1", "2026-10-13T08:00:00+00:00", "openai", 2_000_000, agent="bot", customer="acme"),
        Observed("o2", "2026-10-14T08:00:00+00:00", "openai", 7_000_000, agent="bot", customer="Initech"),
    ])
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, people=PEOPLE, stripe_revenue_key="rk_test_x")
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", ledger
    proxy.shutdown()


def test_one_stripe_customer_margin_named_as_revenue_names_it(gateway):
    there, _ = gateway
    status, headers, body = ask(f"{there}/margin?month=2026-10&customer=cus_1", "stripe-app-key")
    assert status == 200 and headers["access-control-allow-origin"] == "*"
    assert json.loads(body) == {"month": "2026-10", "stripe_customer": "cus_1", "customer": "acme",
                                "revenue": 105_000_000, "ai_cost": 2_000_000, "margin": 103_000_000,
                                "margin_pct": pytest.approx(98.0952, abs=1e-3), "test_mode": True}
    # no invoice paid this month: named from the customer, so its AI cost still shows
    row = json.loads(ask(f"{there}/margin?month=2026-10&customer=cus_9", "stripe-app-key")[2])
    assert (row["customer"], row["revenue"], row["ai_cost"], row["margin_pct"]) == ("Initech", 0, 7_000_000, None)
    whole = json.loads(ask(f"{there}/margin?month=2026-10", "finance-key")[2])
    assert [r["customer"] for r in whole["rows"]] == ["Initech", "acme"] and whole["stripe"]["ids"] == {"cus_1": "acme"}


def test_margin_refusals_are_readable_from_the_app(gateway):
    there, _ = gateway
    for key, code in ((None, 401), ("viewer-key", 403)):
        status, headers, _ = ask(f"{there}/margin?month=2026-10", key)
        assert status == code and headers["access-control-allow-origin"] == "*"
    assert ask(f"{there}/margin?month=2026-10&customer=cus_1/../x", "finance-key")[0] == 400
    status, headers, _ = ask(f"{there}/margin", method="OPTIONS")
    assert status == 204 and headers["access-control-allow-headers"] == "x-quarter-key"
    assert ask(f"{there}/audit", method="OPTIONS")[0] == 501  # no other route answers another origin


def test_margin_without_a_stripe_key_says_how_to_set_one(tmp_path):
    gw = Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, people=PEOPLE)
    proxy = start(serve(gw, port=0))
    try:
        status, _, body = ask(f"http://127.0.0.1:{proxy.server_port}/margin", "finance-key")
        assert status == 503 and "STRIPE_REVENUE_KEY" in json.loads(body)["error"]
    finally:
        proxy.shutdown()


def test_a_read_only_key_reads_but_never_writes(gateway):
    there, ledger = gateway
    ledger.record_config({"caps": []})
    status, _, body = ask(f"{there}/config/sign", "stripe-app-key", "POST")
    assert status == 403 and "read-only" in json.loads(body)["error"]
    assert ledger.db.execute("select count(*) from audit where action = 'role_refused'").fetchone()[0] == 1
    assert ask(f"{there}/config/sign", "finance-key", "POST")[0] == 200
    with pytest.raises(ValueError, match="read_only"):
        Gateway(ledger, [], PRICES, {}, set(), None, people={"X": {"key": "k", "role": "finance", "read_only": "yes"}})
