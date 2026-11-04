import hashlib
import hmac
import json
import time
import urllib.request

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, PRICES, start

from quarter_ledger.issuing import captured_micros, swipe, verified
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

SECRET = "whsec_test"


def signed(payload: bytes, secret: str = SECRET, at: int | None = None) -> str:
    t = str(at or int(time.time()))
    mac = hmac.new(secret.encode(), t.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def authorization(cents: int, merchant: str = "OpenAI", agent: str | None = "researcher", **extra):
    meta = {"quarter_agent": agent, "quarter_customer": "acme"} if agent else {}
    return {
        "id": "iauth_1",
        "status": "pending",
        "card": {"id": "ic_1", "last4": "4242", "metadata": meta},
        "merchant_data": {"name": merchant, "network_id": "123"},
        "pending_request": {"amount": cents, "currency": "usd"},
        **extra,
    }


def post(url: str, event: dict, signature: str | None = None):
    raw = json.dumps(event).encode()
    req = urllib.request.Request(
        url + "/issuing/stripe", raw, {"stripe-signature": signature or signed(raw)}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), resp.headers.get("stripe-version")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), None


def request(cents: int, **kw):
    return {
        "type": "issuing_authorization.request",
        "api_version": "2025-03-31.basil",
        "data": {"object": authorization(cents, **kw)},
    }


@pytest.fixture
def cards(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    caps = [
        Cap("customer", "acme", "month", to_micros(50)),
        Cap("agent", "*", "day", to_micros(100), approve_above=to_micros(20)),
    ]
    gw = Gateway(
        ledger,
        caps,
        PRICES,
        {},
        {AGENT_KEY},
        ADMIN_KEY,
        stripe_secret=SECRET,
        payees={"card": ["OpenAI", "Anthropic"]},
    )
    server = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{server.server_port}", ledger
    server.shutdown()


def test_signature_checks_secret_body_and_age():
    body = b'{"a": 1}'
    assert verified(body, signed(body), SECRET)
    assert not verified(body, signed(body, "whsec_other"), SECRET)
    assert not verified(b'{"a": 2}', signed(body), SECRET)
    assert not verified(body, signed(body, at=int(time.time()) - 600), SECRET)
    assert not verified(body, "garbage", SECRET)


def test_swipe_reads_the_card_and_amount():
    ask = swipe(authorization(1250))
    assert ask["ids"] == {"agent": "researcher", "task": None, "customer": "acme"}
    assert ask["payee"] == "OpenAI" and ask["micros"] == to_micros("12.50")
    assert swipe(authorization(100, agent=None))["ids"]["agent"] == "card-4242"
    closed = {"transactions": [{"amount": -1000}, {"amount": -250}, {"amount": 300}]}
    assert captured_micros(closed) == to_micros("9.50")


def test_swipe_within_caps_is_approved_then_settled_at_what_was_captured(cards):
    url, ledger = cards
    status, body, version = post(url, request(1500))
    assert status == 200 and body["approved"] is True and version == "2025-03-31.basil"
    call_id = body["metadata"]["quarter_call"]

    done = authorization(
        0, status="closed", metadata={"quarter_call": call_id}, transactions=[{"amount": -1400}]
    )
    event = {"type": "issuing_authorization.updated", "data": {"object": done}}
    assert post(url, event)[0] == 200
    assert post(url, event)[0] == 200  # Stripe may send it twice
    assert ledger.totals("customer") == [("acme", "card:OpenAI", to_micros(14))]


def test_over_cap_unlisted_and_foreign_currency_are_declined(cards):
    url, _ = cards
    for _ in range(3):
        assert post(url, request(1500))[1]["approved"] is True  # $45 of the $50 cap
    status, body, _ = post(url, request(1000))
    assert (
        status == 200 and body["approved"] is False and "cap" in body["metadata"]["quarter_reason"]
    )

    unlisted = post(url, request(100, merchant="Unknown Shop"))[1]
    assert unlisted["approved"] is False and unlisted["metadata"]["quarter_approval"].startswith(
        "apr_"
    )

    euro = request(100)
    euro["data"]["object"]["pending_request"]["currency"] = "eur"
    assert post(url, euro)[1]["approved"] is False


def test_big_swipe_waits_for_a_person_and_the_retry_goes_through(cards):
    url, ledger = cards
    first = post(url, request(3000))[1]  # $30, over the $20 approval line
    assert first["approved"] is False
    approval_id = first["metadata"]["quarter_approval"]
    assert post(url, request(3000))[1]["approved"] is False  # still pending

    assert ledger.decide_approval(approval_id, True, "finance@example.com")
    retry = post(url, request(3000))[1]
    assert retry["approved"] is True and ledger.approval(approval_id)["status"] == "used"
    assert post(url, request(3000))[1]["approved"] is False  # one approval, one purchase


def test_unsigned_or_unconfigured_events_are_refused(cards, tmp_path):
    url, _ = cards
    assert post(url, request(100), signature="t=1,v1=bad")[0] == 400

    bare = Gateway(Ledger(str(tmp_path / "b.db")), [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY)
    server = start(serve(bare, port=0))
    try:
        assert post(f"http://127.0.0.1:{server.server_port}", request(100))[0] == 503
    finally:
        server.shutdown()
