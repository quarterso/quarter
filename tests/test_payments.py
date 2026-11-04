import base64
import json

from quarter_ledger.payments import authorisation, authorised, stablecoin_micros, x402_options

KEY = "k" * 32


def test_authorisation_checks_payee_amount_and_expiry():
    token = authorisation("c1", {"agent": "bot"}, "x402", "0xAbC", 50_000, KEY)
    assert authorised(token, KEY, "0xabc", 50_000)
    assert not authorised(token, KEY, "0xabc", 50_001)
    assert not authorised(token, KEY, "0xdef", 10)
    assert not authorised(token, "x" * 32, "0xabc", 10)
    assert not authorised(token, KEY, "0xabc", 10, now=10**12)  # long expired


def test_x402_v1_body_and_v2_header():
    v1 = {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": "base",
                "maxAmountRequired": "10000",
                "payTo": "0xP",
                "asset": "0xUSDC",
                "resource": "/r",
            }
        ],
    }
    assert x402_options(json.dumps(v1).encode())[0]["amount"] == 10000
    v2 = {
        "x402Version": 2,
        "resource": "/r",
        "accepts": [
            {
                "scheme": "exact",
                "network": "base",
                "amount": "2500",
                "payTo": "0xP",
                "asset": "0xUSDC",
            }
        ],
    }
    header = base64.b64encode(json.dumps(v2).encode()).decode()
    opt = x402_options(header=header)[0]
    assert opt["amount"] == 2500 and opt["pay_to"] == "0xP" and opt["resource"] == "/r"


def test_stablecoin_units():
    assert stablecoin_micros(10_000) == 10_000  # USDC: 6 decimals, one atomic unit = $0.000001
    assert stablecoin_micros(10**18, 18) == 10**6
    assert stablecoin_micros(1, 18) == 1  # rounds up, never undercounts
    assert stablecoin_micros(5, 2) == 50_000


def test_x402_settlement_and_caip2():
    import base64
    import json

    from quarter_ledger.payments import caip2, x402_options, x402_settlement

    header = base64.b64encode(json.dumps({"success": True, "transaction": "0xabc",
                                          "network": "base", "payer": "0x1"}).encode()).decode()
    assert x402_settlement(header) == {"success": True, "tx": "0xabc", "network": "eip155:8453", "payer": "0x1"}
    assert caip2("eip155:8453") == "eip155:8453" and caip2("Base-Sepolia") == "eip155:84532"
    body = json.dumps({"accepts": [{"network": "base", "amount": "5", "payTo": "0x2"}]}).encode()
    assert x402_options(body)[0]["network"] == "eip155:8453"
