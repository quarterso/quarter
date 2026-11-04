import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, call, start

from quarter_ledger import mandate
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.receipts import sign_claims
from quarter_ledger.store import Ledger

KEY = "ed25519:" + "11" * 32
OTHER = "ed25519:" + "22" * 32
VERIFIER = Path(__file__).parent.parent / "sellers" / "verify-spend-mandate.mjs"
VECTORS = Path(__file__).parent.parent / "sellers" / "test-vectors.json"
# RFC 8032 section 7.1 test 1: a published key, so anyone can recompute the vectors.
RFC8032 = "ed25519:9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"


def token(**over):
    args = {
        "org": "example.com",
        "call_id": "c1",
        "rail": "x402",
        "payee": "0xAbC",
        "max_micros": 50_000,
        "policy": "p",
        "entry": "e",
        "approval": None,
        "signing_key": KEY,
    } | over
    return mandate.mandate(**args)


def test_seller_checks_key_payee_amount_and_expiry():
    keys = mandate.keys(KEY)
    claims = mandate.verified(token(), keys, "0xabc", 50_000)
    assert claims["iss"] == "example.com" and claims["sub"] == "c1"
    assert "agent" not in claims and "customer" not in claims
    assert mandate.verified(token(), keys, "0xabc", 50_001) is None
    assert mandate.verified(token(), keys, "0xdef", 10) is None
    assert mandate.verified(token(), mandate.keys(OTHER), "0xabc", 10) is None
    assert mandate.verified(token(now=1), keys, "0xabc", 10) is None
    head, body, sig = token().split(".")
    assert mandate.verified(f"{head}.{body}x.{sig}", keys) is None
    assert mandate.issuer(token()) == "example.com" and mandate.issuer("junk") is None
    with pytest.raises(ValueError, match="domain"):
        token(org="https://example.com/x")
    with pytest.raises(ValueError, match="ed25519"):
        token(signing_key="plain-secret")


def test_payments_carry_a_mandate_with_policy_entry_and_approval(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.record_config({"caps": "one cap"})
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.5"))]
    gw = Gateway(
        ledger, caps, PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key=KEY, org="example.com"
    )
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        pay = {"rail": "x402", "payee": "0xP", "amount_usd": "0.3"}
        status, body = call(url + "/authorize", pay)
        reply = json.loads(body)
        claims = mandate.verified(reply["mandate"], mandate.keys(KEY), "0xP", 300_000)
        assert status == 200 and claims["approval"] is None
        assert claims["policy"] == ledger.policy_ref() and claims["entry"] == ledger.entry_hash(
            reply["call_id"]
        )

        status, body = call(url + "/authorize", {**pay, "amount_usd": "0.6"})
        approval = json.loads(body)["approval_id"]
        ledger.decide_approval(approval, True, "dana")
        status, body = call(
            url + "/authorize",
            {**pay, "amount_usd": "0.6"},
            {**HEADERS, "x-quarter-approval": approval},
        )
        claims = mandate.verified(json.loads(body)["mandate"], mandate.keys(KEY), "0xP", 600_000)
        assert status == 200 and claims["approval"] == approval and "dana" not in json.dumps(claims)
    finally:
        server.shutdown()


def test_no_mandate_without_an_org(tmp_path):
    gw = Gateway(
        Ledger(str(tmp_path / "l.db")), [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key=KEY
    )
    server = start(serve(gw, port=0))
    try:
        status, body = call(
            f"http://127.0.0.1:{server.server_port}/authorize",
            {"rail": "x402", "payee": "0xP", "amount_usd": "0.1"},
        )
        assert status == 200 and "mandate" not in json.loads(body)
    finally:
        server.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_javascript_verifier_agrees():
    good = token()
    head, body, sig = good.split(".")
    script = f"""
    import {{ verifySpendMandate }} from {json.dumps(VERIFIER.as_uri())};
    const keys = {json.dumps(mandate.keys(KEY))};
    const other = {json.dumps(mandate.keys(OTHER))};
    const ok = await verifySpendMandate({json.dumps(good)}, {{ payee: '0xabc', amountMicros: 50000, keys }});
    const results = [
      ok?.iss,
      await verifySpendMandate({json.dumps(good)}, {{ payee: '0xabc', amountMicros: 50001, keys }}),
      await verifySpendMandate({json.dumps(good)}, {{ payee: '0xdef', keys }}),
      await verifySpendMandate({json.dumps(good)}, {{ keys: other }}),
      await verifySpendMandate({json.dumps(f"{head}.{body}x.{sig}")}, {{ keys }}),
      await verifySpendMandate({json.dumps(good)}, {{ keys, now: 10 ** 12 }}),
      await verifySpendMandate({json.dumps(f"bnVsbA.{body}.{sig}")}, {{ keys }}),
      await verifySpendMandate({json.dumps(f"{head}.bnVsbA.{sig}")}, {{ keys }}),
    ];
    console.log(JSON.stringify(results));
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == ["example.com", None, None, None, None, None, None, None]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_seller_guard_refuses_missing_forged_untrusted_and_reused():
    good, other = token(), token(org="other.com", call_id="c2")
    script = f"""
    import {{ spendMandateGuard }} from {json.dumps(VERIFIER.as_uri())};
    const keys = {json.dumps(mandate.keys(KEY))};
    const req = (t) => new Request('https://seller.test/data', {{ headers: t ? {{ 'spend-mandate': t }} : {{}} }});
    const guard = spendMandateGuard({{ payee: '0xabc', trust: ['Example.com'], keys }});
    const status = async (t, amount = 40000) => {{ const r = await guard(req(t), amount); return r.refused ? r.refused.status : r.claims.iss; }};
    const open = spendMandateGuard({{ payee: '0xabc', required: false, keys }});
    const broken = spendMandateGuard({{ payee: '0xabc', keys: {{ get keys() {{ throw new Error('keys down'); }} }} }});
    console.log(JSON.stringify([
      await status(null),
      await status({json.dumps(good)}, 60000),
      await status({json.dumps(good)}),
      await status({json.dumps(good)}),
      await status({json.dumps(other)}),
      (await open(req(null), 1)).refused,
      (await broken(req({json.dumps(good)}), 1)).refused.status,
    ]));
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == [402, 403, "example.com", 409, 403, None, 503]


def vectors() -> dict:
    """sellers/test-vectors.json: mandates signed with a published key at a fixed time, what
    a seller must decide for each, and one x402 v2 payment carrying a mandate."""
    at, payee = 1767225600, "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"  # 2026-01-01T00:00:00Z

    def signed(over=None, key=RFC8032, **args):
        args = {"org": "buyer.example.com", "call_id": "call_1", "rail": "x402", "payee": payee,
                "max_micros": 10_000, "policy": "sha256:" + "ab" * 32, "entry": "cd" * 32,
                "approval": None, "signing_key": key, "now": at} | args
        good = mandate.mandate(**args)
        if not over:
            return good
        claims = json.loads(mandate._unb64(good.split(".")[1])) | over
        return sign_claims(claims, key, kid=json.loads(mandate._unb64(good.split(".")[0]))["kid"])

    good = signed()
    head, body, sig = good.split(".")
    flipped = sig[:10] + ("A" if sig[10] != "A" else "B") + sig[11:]

    def case(name, token, valid, now=at + 1, payee=payee, amount=10_000):
        return {"name": name, "token": token, "now": now, "payee": payee,
                "amount_micros": amount, "valid": valid}

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "required": {"type": "boolean"},
            "mandate": {"type": "string",
                        "pattern": "^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$"},
        },
        "required": ["required"],
    }
    accepted = {"scheme": "exact", "network": "eip155:84532", "amount": "10000",
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "payTo": payee,
                "maxTimeoutSeconds": 60, "extra": {"name": "USDC", "version": "2"}}
    resource = {"url": "https://api.example.com/data", "description": "", "mimeType": ""}
    payload = {"x402Version": 2, "resource": resource, "accepted": accepted,
               "payload": {"signature": "0x00"},
               "extensions": {"company-policy": {"info": {"required": True, "mandate": good},
                                                 "schema": schema}}}
    return {
        "about": "Spend mandate version 0 test vectors. The key is RFC 8032 section 7.1 test 1,"
                 " published, so never use it for real. Check each case at its now (Unix"
                 " seconds) against keys, payee and amount_micros: valid says whether a seller"
                 " must accept it. Replay (one payment per iss and sub) is not covered here.",
        "signing_seed_hex": RFC8032[8:],
        "keys": mandate.keys(RFC8032),
        "cases": [
            case("valid", good, True),
            case("valid, payee in another letter case", good, True, payee=payee.lower()),
            case("valid, amount under the cap", good, True, amount=1),
            case("amount over the cap", good, False, amount=10_001),
            case("another payee", good, False, payee="0x0000000000000000000000000000000000000001"),
            case("expired", good, False, now=at + mandate.TTL),
            case("issued more than 60 seconds ahead", good, False, now=at - 61),
            case("signature changed", f"{head}.{body}.{flipped}", False),
            case("signed by another key", signed(key=KEY), False),
            case("version 1", signed({"v": 1}), False),
            case("wrong typ", signed({"typ": "payment-authorisation"}), False),
        ],
        "payment_required": {"x402Version": 2, "error": "Payment required", "resource": resource,
                             "accepts": [accepted],
                             "extensions": {"company-policy": {"info": {"required": True},
                                                               "schema": schema}}},
        "payment_payload": payload,
        "payment_signature_header": base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode()).decode(),
    }


def test_the_published_vectors_hold():
    expected = vectors()
    assert json.loads(VECTORS.read_text()) == expected, "regenerate sellers/test-vectors.json"
    for c in expected["cases"]:
        claims = mandate.verified(c["token"], expected["keys"], c["payee"], c["amount_micros"],
                                  now=c["now"])
        assert (claims is not None) == c["valid"], c["name"]
    assert mandate.from_payment(expected["payment_signature_header"]) == expected["cases"][0]["token"]
    assert mandate.from_payment(None) is None and mandate.from_payment("not base64 json") is None
