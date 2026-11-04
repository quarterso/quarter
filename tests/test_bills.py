import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, call, start

from quarter_ledger import bills
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

IDS = {"agent": "ap", "task": None, "customer": None}
RECIPIENT = "0b4f4f0e-6a3c-4c39-9a37-5b8d2f1c7e11"


def ledger_with_known_payee(tmp_path, caps=()):
    """A ledger where Acme's account has been paid once, so new bills to it need no person."""
    ledger = Ledger(str(tmp_path / "l.db"))
    b = bills.add(
        ledger, list(caps), {}, IDS, "Acme", "INV-0", to_micros(10), "021000021 123456789"
    )
    ledger.decide_approval(b["approval_id"], True, "dana")
    bills.pay(ledger, list(caps), b["id"], "sam", "manual", "bank-ref-0")
    return ledger


def test_first_account_waits_then_one_person_approves_another_pays(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    b = bills.add(
        ledger,
        [],
        {},
        IDS,
        "Acme",
        "INV-1",
        to_micros("120.50"),
        "021000021 123456789",
        "2026-11-20",
    )
    assert b["status"] == "waiting" and "first payment to Acme's account ending 6789" in b["reason"]
    assert b["account"].startswith("sha256:") and "123456789" not in json.dumps(b)
    with pytest.raises(ValueError, match="waiting"):
        bills.pay(ledger, [], b["id"], "sam", "manual", "r1")

    ledger.decide_approval(b["approval_id"], True, "dana")
    with pytest.raises(PermissionError, match="someone else"):
        bills.pay(ledger, [], b["id"], "Dana", "manual", "r1")
    with pytest.raises(PermissionError, match="someone else"):  # a space is not another person
        bills.pay(ledger, [], b["id"], " dana ", "manual", "r1")
    with pytest.raises(ValueError, match="reference"):
        bills.pay(ledger, [], b["id"], "sam", "manual")
    paid = bills.pay(ledger, [], b["id"], "sam", "manual", "r1")
    assert paid["status"] == "paid" and paid["paid_by"] == "sam"
    assert ledger.spent("all", "", None) == to_micros("120.50")
    assert "123456789" not in (tmp_path / "l.db").read_bytes().decode(errors="ignore")

    again = bills.add(ledger, [], {}, IDS, "acme", "INV-2", to_micros(30), "021000021-123456789")
    assert again["status"] == "approved" and again["call_id"]


def test_duplicates_and_changed_bank_details(tmp_path):
    ledger = ledger_with_known_payee(tmp_path)
    with pytest.raises(bills.Duplicate):
        bills.add(ledger, [], {}, IDS, "ACME", "INV-0", to_micros(10), "021000021 123456789")
    similar = bills.add(ledger, [], {}, IDS, "Acme", "INV-0b", to_micros(10), "021000021 123456789")
    assert similar["status"] == "waiting" and "possible duplicate" in similar["reason"]

    changed = bills.add(ledger, [], {}, IDS, "Acme", "INV-3", to_micros(55), "026009593 999988887")
    assert (
        changed["status"] == "waiting"
        and "changed from ending 6789 to ending 8887" in changed["reason"]
    )
    assert any(r["action"] == "payee_account_changed" for r in ledger.audit_log())

    denied = bills.add(
        ledger,
        [],
        {"invoice": ["Acme"], "unlisted": "deny"},
        IDS,
        "Initech",
        "9",
        to_micros(1),
        "x1234",
    )
    assert denied["status"] == "refused"


def test_caps_hold_bills_like_any_spend(tmp_path):
    caps = [Cap("agent", "ap", "month", to_micros(100))]
    ledger = ledger_with_known_payee(tmp_path, caps)
    assert (
        bills.add(ledger, caps, {}, IDS, "Acme", "INV-4", to_micros(80), "021000021 123456789")[
            "status"
        ]
        == "approved"
    )
    assert (
        bills.add(ledger, caps, {}, IDS, "Acme", "INV-5", to_micros(20), "021000021 123456789")[
            "status"
        ]
        == "refused"
    )
    with pytest.raises(ValueError, match="whole cents"):
        bills.add(ledger, caps, {}, IDS, "Acme", "INV-6", 1, "021000021 123456789")
    with pytest.raises(ValueError, match="billion"):
        bills.add(ledger, caps, {}, IDS, "Acme", "INV-7", to_micros("1000000000.01"), "021000021 123456789")


class FakeMercury(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    answer = (200, "pending")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeMercury.requests.append((self.path, self.headers["authorization"], body))
        code, status = FakeMercury.answer
        data = json.dumps({"id": "tx_" + body["idempotencyKey"], "status": status}).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_pays_through_the_customers_own_mercury_account(tmp_path):
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeMercury))
    mercury = {
        "token": "secret-token:test",
        "account": "acct-1",
        "url": f"http://127.0.0.1:{server.server_port}",
    }
    try:
        ledger = Ledger(str(tmp_path / "l.db"))
        b = bills.add(ledger, [], {}, IDS, "Acme", "INV-7", to_micros("99.10"), RECIPIENT.upper())
        ledger.decide_approval(b["approval_id"], True, "dana")

        FakeMercury.answer = (200, "failed")
        with pytest.raises(ValueError, match="did not send"):
            bills.pay(ledger, [], b["id"], "sam", "mercury", mercury=mercury)
        assert bills.bill(ledger, b["id"])["status"] == "approved"

        FakeMercury.answer = (409, "sent")  # already sent under this key: the same payment
        paid = bills.pay(
            ledger, [], b["id"], "sam", "mercury", mercury=mercury, payment_method="domesticWire"
        )
        assert paid["status"] == "paid" and paid["ref"] == "tx_" + b["id"]
        path, auth, body = FakeMercury.requests[-1]
        assert path == "/account/acct-1/transactions" and auth == "Bearer secret-token:test"
        assert body["recipientId"] == RECIPIENT and body["amount"] == 99.1
        assert (
            body["idempotencyKey"] == b["id"] and body["purpose"]["simple"]["category"] == "vendor"
        )
        with pytest.raises(ValueError, match="paid"):
            bills.pay(ledger, [], b["id"], "sam", "mercury", mercury=mercury)

        other = bills.add(ledger, [], {}, IDS, "Initech", "1", to_micros(5), "bank 4444")
        ledger.decide_approval(other["approval_id"], True, "dana")
        with pytest.raises(ValueError, match="recipient id"):
            bills.pay(ledger, [], other["id"], "sam", "mercury", mercury=mercury)
    finally:
        server.shutdown()


def test_bills_over_http(tmp_path):
    from test_roles import PEOPLE

    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, people=PEOPLE)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        bill = {
            "payee": "Acme",
            "invoice": "INV-8",
            "amount_usd": "40",
            "account": "bank 1234",
            "due": "2026-12-01",
        }
        status, body = call(url + "/bills", bill)
        b = json.loads(body)
        assert status == 200 and b["status"] == "waiting"
        assert call(url + "/bills", bill)[0] == 409
        assert call(url + f"/bills/{b['id']}/pay", {"method": "manual", "ref": "r"})[0] == 401

        ledger.decide_approval(b["approval_id"], True, "dana")
        admin = {"x-quarter-key": ADMIN_KEY}
        status, body = call(url + "/bills", None, admin)
        assert status == 200 and json.loads(body)[0]["id"] == b["id"]
        assert call(url + f"/bills/{b['id']}/pay", {"method": "manual", "ref": "r"}, admin)[0] == 403
        finance = {"x-quarter-key": "finance-key"}
        for bad in ({"method": "cash"}, {"method": "manual"}, {"payment_method": "carrier pigeon"}):
            assert call(url + f"/bills/{b['id']}/pay", bad, finance)[0] == 400, bad
        status, body = call(url + f"/bills/{b['id']}/pay", {"method": "manual", "ref": "r"}, finance)
        assert status == 200 and json.loads(body)["status"] == "paid"
        assert call(url + f"/bills/{b['id']}/pay", {"method": "manual", "ref": "r"}, finance)[0] == 409
        assert call(url + "/bills", None, HEADERS)[0] == 401
    finally:
        server.shutdown()


def test_an_agent_cannot_settle_a_bills_hold_through_settle(tmp_path):
    ledger = ledger_with_known_payee(tmp_path)
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        bill = {"payee": "Acme", "invoice": "INV-9", "amount_usd": "40", "account": "021000021 123456789"}
        status, body = call(url + "/bills", bill, {**HEADERS, "x-quarter-agent": "ap"})
        b = json.loads(body)
        assert status == 200 and b["status"] == "approved"
        settle = {"call_id": b["call_id"], "amount_usd": "0"}
        assert call(url + "/settle", settle, {**HEADERS, "x-quarter-agent": "ap"})[0] == 404
        assert ledger.spent("agent", "ap", None) == to_micros(50)  # INV-0 paid, INV-9 still held

        # Settling 0 on a payment's own hold releases it with no charge.
        pay = {"rail": "x402", "payee": "0xP", "amount_usd": "0.25"}
        call_id = json.loads(call(url + "/authorize", pay)[1])["call_id"]
        assert call(url + "/settle", {"call_id": call_id, "amount_usd": "0"})[0] == 200
        assert ledger.spent("agent", "a1", None) == 0
        assert ledger.db.execute(
            "select count(*) from entries where call_id = ? and kind = 'charge'", (call_id,)
        ).fetchone()[0] == 0
    finally:
        server.shutdown()


def test_paying_with_a_bad_body_is_a_clean_400(tmp_path):
    from test_roles import PEOPLE

    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, people=PEOPLE)
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}/bills/bill_x/pay"
        admin = {"x-quarter-key": "finance-key"}
        assert call(url, [], admin)[0] == 400
        assert call(url, "manual", admin)[0] == 400
    finally:
        server.shutdown()


def test_a_payer_who_loses_the_race_to_ready_a_bill_leaves_no_hold(tmp_path, monkeypatch):
    ledger = Ledger(str(tmp_path / "l.db"))
    b = bills.add(ledger, [], {}, IDS, "Acme", "INV-1", to_micros(40), "021000021 123456789")
    ledger.decide_approval(b["approval_id"], True, "dana")
    stale_bill, stale_approval = bills.bill(ledger, b["id"]), ledger.approval(b["approval_id"])
    assert bills.ready(ledger, [], b["id"])["status"] == "approved"

    # A second payer read the bill and its approval before the first one took them.
    reads = iter([stale_bill])
    real_bill = bills.bill
    monkeypatch.setattr(bills, "bill", lambda led, i: next(reads, None) or real_bill(led, i))
    monkeypatch.setattr(ledger, "approval", lambda _id: stale_approval)
    assert bills.ready(ledger, [], b["id"])["status"] == "approved"
    assert ledger.spent("agent", "ap", None) == to_micros(40)  # one hold, not two
