import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from test_proxy import start

from quarter_ledger.cli import main
from quarter_ledger.margin import margins, read_revenue, stripe_revenue
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger


def spend(ledger, customer, usd):
    _, call = ledger.hold([], {"agent": "a", "customer": customer}, "openai", "m", to_micros(usd))
    ledger.settle(call, to_micros(usd))


def this_month(ledger):
    return ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]


def test_read_revenue_matches_headers_and_adds_repeats():
    text = '\ufeffCustomer ID,Amount Paid\nacme,"$1,200.50"\nacme,99.50\n,\nglobex,0\n'
    assert read_revenue(text) == {"acme": to_micros(1300), "globex": 0}


def test_read_revenue_names_the_bad_line():
    with pytest.raises(ValueError, match="line 3"):
        read_revenue("customer,amount\nacme,10\nglobex,ten\n")
    with pytest.raises(ValueError, match="line 2"):
        read_revenue("customer,amount\nacme,Infinity\n")
    with pytest.raises(ValueError, match="customer column"):
        read_revenue("name,price\nacme,10\n")


def test_margin_by_customer_worst_first(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    spend(ledger, "acme", 30)
    spend(ledger, "globex", 120)
    spend(ledger, "initech", 5)
    spend(ledger, None, 7)
    month = this_month(ledger)
    ledger.add_cost("seat", "cursor", to_micros(20), month, customer="acme")

    out = margins(
        ledger, month, {"acme": to_micros(100), "globex": to_micros(100), "hooli": to_micros(50)}
    )
    rows = {r["customer"]: r for r in out["rows"]}
    assert [r["customer"] for r in out["rows"]] == ["initech", "globex", "acme", "hooli"]
    assert rows["acme"]["ai_cost"] == to_micros(50) and rows["acme"]["margin_pct"] == 50
    assert rows["globex"]["margin"] == to_micros(-20)
    assert rows["initech"]["margin_pct"] is None
    assert out["unbilled_customers"] == 1 and out["unbilled_cost"] == to_micros(5)
    assert out["unassigned_cost"] == to_micros(7)
    assert out["margin"] == to_micros(250 - 175)
    assert out["heavy_customers"] == 1 and round(out["heavy_share_pct"]) == round(120 / 175 * 100)


def test_margin_command(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    spend(ledger, "acme", 150)
    month = this_month(ledger)
    revenue = tmp_path / "revenue.csv"
    revenue.write_text("customer,amount_usd\nacme,100\n")

    main(["--db", db, "margin", "--month", month, "--revenue", str(revenue)])
    text = capsys.readouterr().out
    assert "margin after AI cost -$50.00 -50.0%" in text
    assert "costs more than it pays" in text

    main(["--db", db, "margin", "--month", month, "--revenue", str(revenue), "--csv"])
    assert capsys.readouterr().out.splitlines()[1] == "acme,100.00,150.00,-50.00,-50.0"


class FakeStripe(BaseHTTPRequestHandler):
    """Two pages of paid invoices for October 2026, as Stripe's list answers them."""

    requests: ClassVar[list] = []

    def do_GET(self):
        url = urlsplit(self.path)
        FakeStripe.requests.append((url.path, parse_qs(url.query), self.headers.get("Authorization")))
        oct5, sep30 = 1791158400, 1790726400  # 2026-10-05 and 2026-09-30, UTC
        acme = {"id": "cus_1", "metadata": {"quarter_customer": "acme"}}
        first = [
            {"id": "in_1", "customer": acme, "customer_name": "Acme Inc", "currency": "usd", "livemode": False,
             "total_excluding_tax": 12_000, "post_payment_credit_notes_amount": 2_000,
             "status_transitions": {"paid_at": oct5}},
            {"id": "in_2", "customer": {"id": "cus_2", "metadata": {}}, "customer_name": "Globex",
             "currency": "usd", "livemode": False, "total_excluding_tax": 5_000,
             "status_transitions": {"paid_at": sep30}},  # paid in September: not this month
        ]
        second = [
            {"id": "in_3", "customer": acme, "currency": "usd", "livemode": False, "total_excluding_tax": 500,
             "status_transitions": {"paid_at": oct5}},
            {"id": "in_4", "customer": "cus_3", "currency": "eur", "livemode": False, "total_excluding_tax": 900,
             "status_transitions": {"paid_at": oct5}},
        ]
        page = second if "starting_after" in parse_qs(url.query) else first
        body = json.dumps({"object": "list", "has_more": page is first, "data": page}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def stripe_url():
    FakeStripe.requests = []
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeStripe))
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_stripe_revenue_reads_paid_invoices_of_the_month(stripe_url):
    revenue, notes = stripe_revenue("rk_test_x", "2026-10", stripe_url)
    assert revenue == {"acme": to_micros(105)}  # 120 - 20 credit note + 5; September and EUR left out
    assert notes == {"invoices": 2, "skipped_currency": 1, "test_mode": True}
    path, query, auth = FakeStripe.requests[0]
    assert path == "/v1/invoices" and query["status"] == ["paid"] and auth == "Bearer rk_test_x"
    assert FakeStripe.requests[1][1]["starting_after"] == ["in_2"]


def test_stripe_revenue_refuses_a_full_secret_key():
    with pytest.raises(ValueError, match="restricted key"):
        stripe_revenue("sk_live_x", "2026-10")


def test_margin_command_from_stripe(tmp_path, capsys, monkeypatch, stripe_url):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    spend(ledger, "acme", 30)
    month = this_month(ledger)
    monkeypatch.setattr("quarter_ledger.margin.STRIPE_URL", stripe_url)
    monkeypatch.setattr("quarter_ledger.cli.stripe_revenue",
                        lambda key, m: stripe_revenue(key, "2026-10", stripe_url))
    monkeypatch.setenv("STRIPE_REVENUE_KEY", "rk_test_x")
    main(["--db", db, "margin", "--month", month, "--stripe"])
    text = capsys.readouterr().out
    assert "Stripe: 2 invoice(s) paid" in text and "TEST MODE" in text
    assert "revenue $105.00" in text
