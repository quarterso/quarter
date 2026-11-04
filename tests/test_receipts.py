
from quarter_ledger.receipts import sign, verify


def test_receipt_signs_and_detects_tampering():
    approval = {
        "id": "apr_1",
        "agent": "bot",
        "customer": "acme",
        "vendor": "openai",
        "model": "m",
        "estimate": 120000,
        "status": "approved",
        "decided_by": "admin",
    }
    token = sign(approval, "k" * 32)
    claims = verify(token, "k" * 32)
    assert claims["sub"] == "apr_1" and claims["max_amount_micros"] == 120000
    assert verify(token, "x" * 32) is None
    head, body, sig = token.split(".")
    assert verify(f"{head}.{body[:-2]}AA.{sig}", "k" * 32) is None
    assert verify("not-a-token", "k" * 32) is None
