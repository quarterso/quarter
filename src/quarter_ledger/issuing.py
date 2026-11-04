"""Quarter cards on Stripe Issuing: each swipe is decided by the same caps as every call.

Stripe sends `issuing_authorization.request` and waits at most 2 seconds for an answer, so
no person can approve inside it. A swipe over the approval line is declined and opens an
approval; once a person approves, the agent's retry of the same purchase goes through.
"""

import hashlib
import hmac
import time

TOLERANCE = 300  # seconds a signed event stays valid, as Stripe's own libraries use
CLOSED = {"closed", "reversed", "expired"}  # the authorization will not change again


def verified(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    """Whether Stripe signed this payload with the endpoint's secret (Stripe-Signature)."""
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    stamps = [v for k, v in parts if k == "t"]
    signatures = [v for k, v in parts if k == "v1"]
    if len(stamps) != 1 or not signatures or not stamps[0].isdigit():
        return False
    if abs((now or time.time()) - int(stamps[0])) > TOLERANCE:
        return False
    signed = stamps[0].encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def swipe(auth: dict) -> dict:
    """What a card authorization asks for, in the ledger's terms.

    The card's metadata names the agent, task and customer (`quarter_agent`, and so on);
    a card without it counts as its own agent, `card-<last 4>`."""
    card = auth.get("card") or {}
    meta = card.get("metadata") or {}
    request = auth.get("pending_request") or {}
    merchant = auth.get("merchant_data") or {}
    return {
        "ids": {
            "agent": meta.get("quarter_agent") or f"card-{card.get('last4', 'unknown')}",
            "task": meta.get("quarter_task") or None,
            "customer": meta.get("quarter_customer") or None,
        },
        "payee": merchant.get("name") or merchant.get("network_id") or "unknown merchant",
        "currency": request.get("currency", ""),
        "micros": int(request.get("amount") or 0) * 10_000,  # cents to millionths of a dollar
    }


def captured_micros(auth: dict) -> int:
    """What a closed authorization actually took: captures are negative, refunds positive."""
    cents = -sum(int(t.get("amount") or 0) for t in auth.get("transactions") or [])
    return max(cents, 0) * 10_000
