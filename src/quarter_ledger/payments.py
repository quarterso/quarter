"""One policy for every way an agent pays: x402, MPP, cards, anything with an amount.

Quarter never holds keys or money. Before paying, the agent asks Quarter to authorise
the payment. Quarter applies the same caps, approvals and pauses as for API calls,
holds the amount, and returns a short-lived signed authorisation.
The customer's own wallet or signer checks it with `authorised()` before it signs
anything, and the agent reports what it really paid with /settle.
"""

import base64
import json
import time

from .receipts import sign_claims, verify

AUTH_TTL = 600  # seconds an authorisation stays valid
RAILS = {"x402", "mpp", "card", "invoice", "other"}


def payee_rule(payees: dict, rail: str, payee: str) -> str | None:
    """None if the payee may be paid, "ask" if a person must agree, "deny" if refused."""
    allowed = payees.get(rail)
    if allowed is None or payee.lower() in {p.lower() for p in allowed}:
        return None
    return "deny" if payees.get("unlisted") == "deny" else "ask"


def authorisation(call_id: str, ids: dict, rail: str, payee: str, max_micros: int, key: str) -> str:
    now = int(time.time())
    return sign_claims(
        {
            "iss": "quarter-ledger",
            "typ": "payment-authorisation",
            "sub": call_id,
            "agent": ids["agent"],
            "customer": ids.get("customer"),
            "rail": rail,
            "payee": payee,
            "max_amount_micros": max_micros,
            "iat": now,
            "exp": now + AUTH_TTL,
        },
        key,
    )


def authorised(
    token: str, key: str, payee: str, amount_micros: int, now: float | None = None
) -> bool:
    """For the customer's wallet or signer: may it pay this payee this amount?

    True only if Quarter signed the token, it has not expired, it names this payee,
    and the amount is within what was authorised."""
    claims = verify(token, key)
    return bool(
        claims
        and claims.get("typ") == "payment-authorisation"
        and claims["exp"] > (now or time.time())
        and claims["payee"].lower() == payee.lower()
        and 0 <= amount_micros <= claims["max_amount_micros"]
    )


# x402 v1 names networks; v2 uses CAIP-2 ids. Everything is stored as CAIP-2.
CAIP2 = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "ethereum": "eip155:1",
    "polygon": "eip155:137",
    "avalanche": "eip155:43114",
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
}


def caip2(network: str | None) -> str | None:
    return None if network is None else CAIP2.get(network.lower(), network)


def x402_settlement(header: str) -> dict:
    """The x402 v2 PAYMENT-RESPONSE header: did the payment settle, and where."""
    raw = json.loads(base64.b64decode(header))
    return {
        "success": bool(raw.get("success")),
        "tx": raw.get("transaction") or "",
        "network": caip2(raw.get("network")),
        "payer": raw.get("payer"),
    }


def x402_options(body: bytes | None = None, header: str | None = None) -> list[dict]:
    """The ways to pay an x402 402 reply offers, from its JSON body (v1) or its
    base64 PAYMENT-REQUIRED header (v2). Amounts stay in the asset's atomic units."""
    raw = json.loads(base64.b64decode(header)) if header else json.loads(body or b"{}")
    return [
        {
            "scheme": a.get("scheme"),
            "network": caip2(a.get("network")),
            "asset": a.get("asset"),
            "pay_to": a.get("payTo"),
            "amount": int(a.get("amount", a.get("maxAmountRequired", 0))),
            "resource": a.get("resource", raw.get("resource")),
        }
        for a in raw.get("accepts", [])
    ]


def stablecoin_micros(atomic: int, decimals: int = 6) -> int:
    """Atomic units of a USD stablecoin in micro-dollars. USDC has 6 decimals, so they
    are the same number; other decimals are scaled, rounding up."""
    if decimals >= 6:
        return -(-atomic // 10 ** (decimals - 6))
    return atomic * 10 ** (6 - decimals)
