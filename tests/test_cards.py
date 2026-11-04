import base64
import json
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from test_proxy import start

from quarter_ledger import cards, store
from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger

MERCURY_TOKEN = "secret-token:mercury-test-123"
RAMP_ID, RAMP_SECRET, RAMP_TOKEN = "ramp-client", "ramp-secret-456", "ramp_business_tok_test789"
AGENT_CARD, OTHER_CARD, FUND = "c0ffee00-0000-4000-8000-00000000000a", "c0ffee00-0000-4000-8000-00000000000f", "f00d0000-0000-4000-8000-000000000001"


def mercury_tx(n, at, amount, kind="debitCardTransaction", card=AGENT_CARD, status="sent", name="Exa"):
    """A transaction as Mercury's listTransactions documents it (fields this module reads,
    and a few it does not)."""
    return {"id": f"m{n:03d}", "amount": amount, "kind": kind, "status": status, "createdAt": at,
            "postedAt": at, "counterpartyName": name, "counterpartyId": "cp-1", "cardId": card,
            "accountId": "acct-1", "dashboardLink": "https://app.mercury.com/x", "details": None,
            "merchant": {"amount": None, "currency": "USD", "categoryCode": "5734"},
            "estimatedDeliveryDate": at, "attachments": [], "relatedTransactions": []}


def ramp_tx(n, at, cents, card="r-card-1", state="CLEARED", fund=FUND, original=None, name="Exa"):
    """A transaction as Ramp's OpenAPI document gives it."""
    return {"id": f"r{n:03d}", "amount": cents / 100, "currency_code": "USD", "card_id": card,
            "fund_id": fund, "limit_id": fund, "card_holder": {"user_id": "u-1", "first_name": "Ops"},
            "entity_amount": {"currency": "USD", "value": cents},
            "merchant_amount": {"currency": "USD", "value": cents}, "merchant_name": name,
            "state": state, "user_transaction_time": at, "settlement_date": at,
            "original_transaction_id": original}


class FakeCards(BaseHTTPRequestHandler):
    """Mercury's GET /transactions and Ramp's token and transactions endpoints."""

    mercury: ClassVar[list] = []
    ramp: ClassVar[list] = []
    page = 3
    seen: ClassVar[list] = []
    next_host = None  # set, Ramp's next page points at another host

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        FakeCards.seen.append((url.path, q, self.headers.get("authorization")))
        if url.path == "/api/v1/transactions":
            if self.headers.get("authorization") != f"Bearer {MERCURY_TOKEN}":
                return self.answer(401, {"error": f"bad token {self.headers.get('authorization')}"})
            items = FakeCards.mercury
            first = next((i + 1 for i, t in enumerate(items) if t["id"] == q.get("start_after")), 0)
            chunk = items[first:first + FakeCards.page]
            more = first + FakeCards.page < len(items)
            return self.answer(200, {"transactions": chunk, "page": {
                "nextPage": chunk[-1]["id"] if more else None, "previousPage": None}})
        if url.path == "/developer/v1/transactions":
            if self.headers.get("authorization") != f"Bearer {RAMP_TOKEN}":
                return self.answer(401, {"error": f"bad token {self.headers.get('authorization')}"})
            first = int(q.get("start", 0))
            chunk = FakeCards.ramp[first:first + FakeCards.page]
            more = first + FakeCards.page < len(FakeCards.ramp)
            nxt = f"http://{FakeCards.next_host or self.headers['host']}{url.path}?{urllib.parse.urlencode(q | {'start': first + len(chunk)})}"
            return self.answer(200, {"data": chunk, "page": {
                "next": nxt if more else None, "total_count": None, "total_count_capped": None}})
        return self.answer(404, {})

    def do_POST(self):
        body = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["content-length"])).decode()))
        basic = base64.b64encode(f"{RAMP_ID}:{RAMP_SECRET}".encode()).decode()
        if self.path != "/developer/v1/token" or self.headers.get("authorization") != f"Basic {basic}":
            return self.answer(401, {"error": f"bad client {self.headers.get('authorization')}"})
        assert body == {"grant_type": "client_credentials", "scope": "transactions:read"}
        return self.answer(200, {"access_token": RAMP_TOKEN, "token_type": "Bearer",
                                 "expires_in": 864000, "scope": "transactions:read"})

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def feed(monkeypatch):
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeCards))
    FakeCards.mercury, FakeCards.ramp, FakeCards.seen, FakeCards.page = [], [], [], 3
    FakeCards.next_host = None
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("QUARTER_MERCURY_URL", base + "/api/v1")
    monkeypatch.setenv("QUARTER_RAMP_URL", base)
    monkeypatch.delenv("QUARTER_MERCURY_TOKEN", raising=False)
    monkeypatch.delenv("QUARTER_MERCURY_READ_TOKEN", raising=False)
    yield base
    server.shutdown()


def authorise(ledger, monkeypatch, at, usd, agent="researcher", payee="exa.ai", customer="acme"):
    """A card payment authorised through POST /authorize at that time."""
    monkeypatch.setattr(store, "now", lambda: datetime.fromisoformat(at).isoformat())
    ids = {"agent": agent, "task": "t-" + at[:10], "customer": customer}
    _, call_id = ledger.hold([], ids, f"card:{payee}", "card", to_micros(usd))
    return call_id


def run(tmp_path, capsys, *extra, source="mercury"):
    (tmp_path / "cards.json").write_text(json.dumps({AGENT_CARD: "researcher", FUND.upper(): "writer"}))
    with pytest.raises(SystemExit) as e:
        main(["--db", str(tmp_path / "l.db"), "cards", "reconcile", "--month", "2026-10",
              "--source", source, "--cards", str(tmp_path / "cards.json"), *extra])
    out = capsys.readouterr()
    return e.value.code, out.out, out.err


def test_tolerance_is_five_percent_or_a_dollar():
    assert cards.allowed(to_micros("10")) == to_micros("11")
    assert cards.allowed(to_micros("100")) == to_micros("105")
    assert cards.allowed(to_micros("0.40")) == to_micros("1.40")


def test_mercury_matches_flags_and_settles(tmp_path, capsys, monkeypatch, feed):
    ledger = Ledger(str(tmp_path / "l.db"))
    paid = authorise(ledger, monkeypatch, "2026-10-05T10:00:00+00:00", "20")
    over = authorise(ledger, monkeypatch, "2026-10-12T09:00:00+00:00", "10")
    edge = authorise(ledger, monkeypatch, "2026-10-15T09:00:00+00:00", "100")
    past_edge = authorise(ledger, monkeypatch, "2026-10-16T09:00:00+00:00", "100")
    stale = authorise(ledger, monkeypatch, "2026-10-20T00:00:00+00:00", "30")
    authorise(ledger, monkeypatch, "2026-10-21T00:00:00+00:00", "30", agent="writer")  # not this card
    FakeCards.mercury = [
        mercury_tx(1, "2026-10-05T10:05:00Z", -19.5),
        mercury_tx(2, "2026-10-05T10:06:00Z", -19.5),  # the same authorisation, charged twice
        mercury_tx(3, "2026-10-08T12:00:00Z", -7.0, name="Unknown SaaS"),  # never authorised
        mercury_tx(4, "2026-10-09T12:00:00Z", 5.0, kind="debitCardCredit"),  # a refund
        mercury_tx(5, "2026-10-09T13:00:00Z", -100.0, card=OTHER_CARD),  # not an agent card
        mercury_tx(6, "2026-10-10T13:00:00Z", -50.0, status="reversed"),  # never moved money
        mercury_tx(7, "2026-10-12T09:30:00Z", -12.0),  # $1 over the $1 tolerance
        mercury_tx(8, "2026-10-15T09:30:00Z", -105.0),  # exactly at 5%
        mercury_tx(9, "2026-10-16T09:30:00Z", -105.01),  # a cent past it
        mercury_tx(10, "2026-10-23T00:01:00Z", -30.0),  # 72 hours and a minute after `stale`
        mercury_tx(11, "2026-10-26T00:00:00Z", -1.0, kind="cardInternationalTransactionFee", card=None),
    ]
    monkeypatch.setenv("QUARTER_MERCURY_TOKEN", MERCURY_TOKEN)
    code, out, err = run(tmp_path, capsys)
    assert code == 1
    assert "7 charges on agent cards, 2 match" in out
    matched, rest = out.split("Charged outside Quarter")
    assert "m001" in matched and f"call {paid}" in matched and "task t-2026-10-05" in matched
    assert "customer acme" in matched and "m008" in matched and f"call {edge}" in matched
    outside, rest = rest.split("Charged more than authorised:")
    assert all(f"m{n:03d}" in outside for n in (2, 3, 10))
    over_part, rest = rest.split("Refunds and credits")
    assert "m007" in over_part and f"call {over}" in over_part and f"call {past_edge}" in over_part
    refunds, never = rest.split("Authorised and not charged")
    assert "m004" in refunds and "5.00  mercury m004" in refunds
    assert f"call {stale}" in never
    assert "m005" not in out and "m006" not in out and "m011" not in out
    assert MERCURY_TOKEN not in out + err
    pages = [q.get("start_after") for path, q, _ in FakeCards.seen]
    assert pages == [None, "m003", "m006", "m009"]
    assert FakeCards.seen[0][1]["start"] == "2026-09-28" and FakeCards.seen[0][1]["order"] == "asc"

    code, out, _ = run(tmp_path, capsys, "--settle")
    assert "Recorded 2 matched charge(s)" in out
    charged = dict(ledger.db.execute("select call_id, amount from entries where kind = 'charge'").fetchall())
    assert charged == {paid: to_micros("19.50"), edge: to_micros("105")}
    audit = [d for (d,) in ledger.db.execute("select detail from audit where action = 'payment_settled'")]
    assert f"{paid} card 19500000 tx=mercury:m001" in audit
    assert any(edge in d for (d,) in ledger.db.execute(
        "select detail from audit where action = 'over_authorised'"))
    assert all(t["intact"] for t in ledger.verify_chain().values())
    _, out, _ = run(tmp_path, capsys, "--settle")
    assert "Recorded 0 matched charge(s)" in out  # already settled: never twice


def test_a_charge_before_its_authorisation_is_outside(tmp_path, capsys, monkeypatch, feed):
    ledger = Ledger(str(tmp_path / "l.db"))
    authorise(ledger, monkeypatch, "2026-10-05T10:10:00+00:00", "20")
    FakeCards.mercury = [mercury_tx(1, "2026-10-05T10:00:00Z", -20.0)]
    monkeypatch.setenv("QUARTER_MERCURY_READ_TOKEN", MERCURY_TOKEN)
    code, out, _ = run(tmp_path, capsys)
    assert code == 1 and "Charged outside Quarter" in out


def test_clean_month_exits_zero(tmp_path, capsys, monkeypatch, feed):
    ledger = Ledger(str(tmp_path / "l.db"))
    authorise(ledger, monkeypatch, "2026-10-05T10:00:00+00:00", "20")
    FakeCards.mercury = [mercury_tx(1, "2026-10-05T10:01:00Z", -20.0)]
    monkeypatch.setenv("QUARTER_MERCURY_READ_TOKEN", MERCURY_TOKEN)
    code, out, _ = run(tmp_path, capsys)
    assert code == 0 and "1 charges on agent cards, 1 match" in out


def test_ramp_token_pages_cents_and_refunds(tmp_path, capsys, monkeypatch, feed):
    ledger = Ledger(str(tmp_path / "l.db"))
    first = authorise(ledger, monkeypatch, "2026-10-02T08:00:00+00:00", "40", agent="writer")
    authorise(ledger, monkeypatch, "2026-10-03T08:00:00+00:00", "15", agent="writer")
    FakeCards.ramp = [
        ramp_tx(1, "2026-10-02T08:10:00+00:00", 3999, card="single-use-1"),  # a fresh card, same fund
        ramp_tx(2, "2026-10-03T08:10:00+00:00", 1500, card="single-use-2"),
        ramp_tx(3, "2026-10-04T08:10:00+00:00", -1000, card="single-use-1", original="r001"),
        ramp_tx(4, "2026-10-05T08:10:00+00:00", 9900, state="DECLINED"),
        ramp_tx(5, "2026-10-06T08:10:00+00:00", 2500, fund="another-fund"),  # not an agent's
        ramp_tx(6, "2026-10-07T08:10:00+00:00", 800, state="PENDING"),  # writer, never authorised
    ]
    monkeypatch.setenv("QUARTER_RAMP_CLIENT_ID", RAMP_ID)
    monkeypatch.setenv("QUARTER_RAMP_CLIENT_SECRET", RAMP_SECRET)
    code, out, err = run(tmp_path, capsys, "--settle", source="ramp")
    assert code == 1
    assert "3 charges on agent cards, 2 match" in out and f"call {first}" in out
    assert "10.00  ramp r003  refunds r001" in out
    assert "r006  pending" in out.split("Charged outside Quarter")[1]
    assert "r004" not in out and "r005" not in out
    assert "Recorded 2 matched charge(s)" in out
    assert ledger.db.execute("select amount from entries where call_id = ? and kind = 'charge'",
                             (first,)).fetchone() == (to_micros("39.99"),)
    for secret in (RAMP_SECRET, RAMP_TOKEN):
        assert secret not in out + err
    starts = [q.get("start") for path, q, _ in FakeCards.seen if path == "/developer/v1/transactions"]
    assert starts == [None, "3"]


def test_errors_never_show_a_secret(tmp_path, capsys, monkeypatch, feed):
    monkeypatch.setenv("QUARTER_MERCURY_TOKEN", "secret-token:wrong-one")
    code, out, err = run(tmp_path, capsys)
    assert "answered 401" in str(code) and "wrong-one" not in str(code) + out + err
    monkeypatch.setenv("QUARTER_RAMP_CLIENT_ID", RAMP_ID)
    monkeypatch.setenv("QUARTER_RAMP_CLIENT_SECRET", "not-the-secret")
    code, out, err = run(tmp_path, capsys, source="ramp")
    assert "answered 401" in str(code)
    basic = base64.b64encode(f"{RAMP_ID}:not-the-secret".encode()).decode()
    assert "not-the-secret" not in str(code) + out + err and basic not in str(code)


def test_missing_credentials_and_next_page_elsewhere(tmp_path, capsys, monkeypatch, feed):
    code, _, _ = run(tmp_path, capsys)
    assert "QUARTER_MERCURY_READ_TOKEN" in str(code)
    code, _, _ = run(tmp_path, capsys, source="ramp")
    assert "QUARTER_RAMP_CLIENT_ID" in str(code)
    start, end = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    FakeCards.ramp = [ramp_tx(n, "2026-10-02T08:10:00+00:00", 100) for n in range(4)]
    FakeCards.next_host = "evil.example"
    with pytest.raises(ValueError, match="another host"):
        cards.ramp(RAMP_TOKEN, start, end, feed)
    assert all("evil" not in path for path, _, _ in FakeCards.seen)
