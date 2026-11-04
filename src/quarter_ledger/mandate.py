"""Spend mandates: proof, for the seller, that a company's spend policy authorised a payment.

The payment authorisation in payments.py is for the buyer's own wallet. A mandate is what
the agent shows the seller. It names the company by its domain, not the agent or the
customer, and says which policy was in force, whether a person approved, and where the
spend sits in the company's ledger. The seller checks it against the public keys the
company publishes at https://<domain>/.well-known/spend-mandate-keys.json.

With x402 v2 the agent sends it inside the payment, as the company-policy extension
(PaymentPayload.extensions["company-policy"].info.mandate). The Spend-Mandate header is
deprecated and goes in the next release.
"""

import base64
import hashlib
import json
import re
import time
import urllib.request

from . import ed25519
from .receipts import _b64, _seed, _unb64, sign_claims

TYP = "spend-mandate"
VERSION = 0
TTL = 600  # seconds, as for payment authorisations
KEYS_PATH = "/.well-known/spend-mandate-keys.json"
EXTENSION = "company-policy"
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def same_payee(a: str, b: str) -> bool:
    """Wallet addresses (0x...) match whatever their letter case; anything else exactly."""
    if a.lower().startswith("0x") and b.lower().startswith("0x"):
        return a.lower() == b.lower()
    return a == b


def key_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


def keys(signing_key: str) -> dict:
    """The key set the company publishes at KEYS_PATH on its domain (JWKS)."""
    seed = _seed(signing_key)
    if seed is None:
        raise ValueError("spend mandates need an ed25519: signing key")
    public = ed25519.public_key(seed)
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": _b64(public), "kid": key_id(public)}
    return {"keys": [jwk | {"alg": "EdDSA", "use": "sig"}]}


def mandate(
    org: str,
    call_id: str,
    rail: str,
    payee: str,
    max_micros: int,
    policy: str | None,
    entry: str | None,
    approval: str | None,
    signing_key: str,
    now: float | None = None,
) -> str:
    if not DOMAIN.match(org):
        raise ValueError(
            f"QUARTER_ORG must be the company's domain, such as example.com, not {org!r}"
        )
    seed = _seed(signing_key)
    if seed is None:
        raise ValueError("spend mandates need an ed25519: signing key")
    issued = int(now or time.time())
    claims = {
        "v": VERSION,
        "typ": TYP,
        "iss": org,
        "sub": call_id,
        "aud": payee,
        "rail": rail,
        "max_amount_micros": max_micros,
        "currency": "USD",
        "policy": policy,
        "entry": entry,
        "approval": approval,
        "iat": issued,
        "exp": issued + TTL,
    }
    return sign_claims(claims, signing_key, kid=key_id(ed25519.public_key(seed)))


def issuer(token: str) -> str | None:
    """The domain a mandate claims to come from, unchecked: where to fetch its keys."""
    try:
        iss = json.loads(_unb64(token.split(".")[1])).get("iss")
    except (ValueError, IndexError, json.JSONDecodeError, AttributeError):
        return None
    return iss if isinstance(iss, str) and DOMAIN.match(iss) else None


def from_payment(header: str | None) -> str | None:
    """For the seller: the mandate in an x402 v2 payment, from its base64 PAYMENT-SIGNATURE
    header. None if the payment carries none."""
    try:
        found = json.loads(base64.b64decode(header or ""))["extensions"][EXTENSION]["info"]["mandate"]
    except (ValueError, KeyError, TypeError):
        return None
    return found if isinstance(found, str) else None


def fetch_keys(domain: str, timeout: float = 3) -> dict:
    """The key set a company publishes. Raises if the domain does not answer with one."""
    if not DOMAIN.match(domain):
        raise ValueError(f"not a domain: {domain!r}")
    with urllib.request.urlopen(f"https://{domain}{KEYS_PATH}", timeout=timeout) as response:
        return json.loads(response.read())


def verified(
    token: str,
    key_set: dict,
    payee: str | None = None,
    amount_micros: int | None = None,
    now: float | None = None,
) -> dict | None:
    """For the seller: the claims if this company's key signed the mandate, it has not
    expired, it names this payee, and the amount is within it. Otherwise None.
    Accept one payment per mandate: its sub is unique."""
    try:
        head, body, sig = token.split(".")
        header, claims = json.loads(_unb64(head)), json.loads(_unb64(body))
        jwk = next(
            k
            for k in key_set.get("keys", [])
            if k.get("kid") == header.get("kid")
            and k.get("kty") == "OKP"
            and k.get("crv") == "Ed25519"
        )
        at = now or time.time()
        ok = (
            header.get("alg") == "EdDSA"
            and claims.get("typ") == TYP
            and claims.get("v") == VERSION
            and claims.get("exp", 0) > at
            and claims.get("iat", at) <= at + 60
            and (payee is None or same_payee(str(claims.get("aud", "")), payee))
            and (amount_micros is None or amount_micros <= claims.get("max_amount_micros", -1))
            and ed25519.verify(_unb64(jwk["x"]), f"{head}.{body}".encode(), _unb64(sig))
        )
    except (ValueError, KeyError, TypeError, StopIteration, json.JSONDecodeError, AttributeError):
        return None
    return claims if ok else None
