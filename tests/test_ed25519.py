from quarter_ledger import ed25519
from quarter_ledger.receipts import public_key, sign_claims, verify

# RFC 8032 section 7.1, tests 1 and 2.
VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


def test_rfc_vectors():
    for seed, pub, msg, sig in VECTORS:
        seed, pub, msg, sig = map(bytes.fromhex, (seed, pub, msg, sig))
        assert ed25519.public_key(seed) == pub
        assert ed25519.sign(seed, msg) == sig
        assert ed25519.verify(pub, msg, sig)
        assert not ed25519.verify(pub, msg + b"x", sig)


def test_receipt_verifies_with_public_key_only():
    key = "ed25519:" + VECTORS[0][0]
    token = sign_claims({"sub": "a1", "max_amount_micros": 5_000_000}, key)
    pub = public_key(key)
    assert verify(token, pub)["sub"] == "a1"
    assert verify(token, key)["sub"] == "a1"
    head, body, sig = token.split(".")
    assert verify(f"{head}.{body[:-2]}xx.{sig}", pub) is None
    assert verify(sign_claims({"sub": "a1"}, "secret"), pub) is None
    assert verify(token, "secret") is None
