"""Signed approval receipts: proof of what a person authorised an agent to spend.

A receipt is a compact JWT signed with QUARTER_SIGNING_KEY. A plain secret signs HS256,
which only holders of the secret can check. A key written "ed25519:<64 hex seed>" signs
EdDSA, which anyone can check with the public key ("ed25519-pub:<64 hex>"). It follows the idea of
an AP2 intent mandate (who, for what, up to how much, approved by whom) but is not an
AP2 credential.
"""

import base64
import hashlib
import hmac
import json

from . import ed25519


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def claims(approval: dict) -> dict:
    return {
        "iss": "quarter-ledger",
        "sub": approval["id"],
        "agent": approval["agent"],
        "task": approval.get("task"),
        "customer": approval.get("customer"),
        "vendor": approval["vendor"],
        "model": approval.get("model"),
        "max_amount_micros": approval["estimate"],
        "status": approval["status"],
        "decided_by": approval.get("decided_by"),
        "decided_at": approval.get("decided_at"),
    }


def sign(approval: dict, key: str) -> str:
    return sign_claims(claims(approval), key)


def _seed(key: str) -> bytes | None:
    return bytes.fromhex(key[8:]) if key.startswith("ed25519:") else None


def public_key(key: str) -> str:
    """The shareable verify key for an ed25519 signing key."""
    seed = _seed(key)
    if seed is None:
        raise ValueError("only an ed25519: key has a public key")
    return "ed25519-pub:" + ed25519.public_key(seed).hex()


def sign_claims(claims: dict, key: str, kid: str | None = None) -> str:
    seed = _seed(key)
    alg = "EdDSA" if seed else "HS256"
    header = {"alg": alg, "typ": "JWT"} | ({"kid": kid} if kid else {})
    head = _b64(json.dumps(header, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    data = f"{head}.{body}".encode()
    sig = ed25519.sign(seed, data) if seed else hmac.new(key.encode(), data, hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def verify(token: str, key: str) -> dict | None:
    """The claims if the signature holds, else None. Key: secret, ed25519 or ed25519-pub."""
    try:
        head, body, sig = token.split(".")
        data, alg = f"{head}.{body}".encode(), json.loads(_unb64(head)).get("alg")
        if key.startswith("ed25519"):
            public = bytes.fromhex(key[12:]) if key.startswith("ed25519-pub:") else ed25519.public_key(_seed(key))
            ok = alg == "EdDSA" and ed25519.verify(public, data, _unb64(sig))
        else:
            want = hmac.new(key.encode(), data, hashlib.sha256).digest()
            ok = alg == "HS256" and hmac.compare_digest(want, _unb64(sig))
        return json.loads(_unb64(body)) if ok else None
    except (ValueError, json.JSONDecodeError):
        return None
