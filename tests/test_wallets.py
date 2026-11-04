import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from typing import ClassVar

import pytest
from test_proxy import start

from quarter_ledger import store, wallets
from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.payments import stablecoin_micros
from quarter_ledger.policy import Cap
from quarter_ledger.store import Ledger

GENESIS = int(datetime(2026, 9, 30, tzinfo=UTC).timestamp())
STEP = 600  # one block every 10 minutes: October is blocks 144 to 4607
LATEST = 6_000
OURS, OTHER = "0x" + "a1" * 20, "0x" + "b2" * 20  # the researcher's wallet, a wallet not listed
EXA, PERPLEX, STRANGER = "0x" + "e1" * 20, "0x" + "f2" * 20, "0x" + "99" * 20


def at_block(n: int) -> datetime:
    return datetime.fromtimestamp(GENESIS + n * STEP, UTC)


def log(block: int, sender: str, to: str, amount: int, tx: str) -> dict:
    return {
        "address": wallets.USDC.lower(),
        "blockNumber": hex(block),
        "transactionHash": tx,
        "topics": [wallets.TRANSFER, wallets.topic(sender), wallets.topic(to)],
        "data": "0x" + f"{amount:064x}",
        "removed": False,
    }


class FakeBase(BaseHTTPRequestHandler):
    """Base's JSON-RPC as far as reconcile uses it, with a provider's block range cap."""

    logs: ClassVar[list] = []
    max_range = 500
    getlogs: ClassVar[list] = []
    busy = 0  # answer this many requests with HTTP 429 first

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if FakeBase.busy:
            FakeBase.busy -= 1
            return self.answer(429, {"jsonrpc": "2.0", "id": 1,
                                     "error": {"code": -32011, "message": "request limit reached"}})
        method, params = req["method"], req["params"]
        if method == "eth_blockNumber":
            return self.reply(hex(LATEST))
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return self.reply({"number": params[0], "timestamp": hex(GENESIS + n * STEP)}
                              if n <= LATEST else None)
        q = params[0]
        lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
        FakeBase.getlogs.append((lo, hi))
        if hi - lo + 1 > FakeBase.max_range:
            return self.answer(200, {"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32600, "message": f"block range is too large, max {FakeBase.max_range}"}})
        senders = set(q["topics"][1])
        assert q["address"] == wallets.USDC and q["topics"][0] == wallets.TRANSFER
        return self.reply([g for g in FakeBase.logs if lo <= int(g["blockNumber"], 16) <= hi
                           and g["topics"][1] in senders])

    def reply(self, result):
        self.answer(200, {"jsonrpc": "2.0", "id": 1, "result": result})

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def base():
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeBase))
    FakeBase.logs, FakeBase.getlogs, FakeBase.busy, FakeBase.max_range = [], [], 0, 500
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def pay(ledger, monkeypatch, block, payee, micros, tx="", network="eip155:8453", agent="researcher"):
    """An x402 payment authorised and settled in Quarter at that block's time."""
    monkeypatch.setattr(store, "now", lambda: at_block(block).isoformat())
    ids = {"agent": agent, "task": None, "customer": None}
    _, call_id = ledger.hold([], ids, f"x402:{payee}", "x402", micros)
    ledger.settle(call_id, micros)
    ledger.audit("payment_settled", "127.0.0.1",
                 f"{call_id} x402 {micros} tx={tx} network={network}")
    return call_id


def test_usdc_units_are_micro_dollars():
    assert stablecoin_micros(1_000_000) == to_micros("1.00") == 1_000_000
    assert stablecoin_micros(250_000, decimals=6) == to_micros("0.25")


def test_reconcile_pages_halves_and_finds_spend_outside_quarter(tmp_path, base, monkeypatch):
    ledger = Ledger(str(tmp_path / "l.db"))
    by_tx = pay(ledger, monkeypatch, 300, EXA, 250_000, tx="0x" + "01" * 32)
    pay(ledger, monkeypatch, 2_500, PERPLEX, 1_000_000)  # no tx reported: matched on payee
    short = pay(ledger, monkeypatch, 3_000, EXA, 500_000)
    missing = pay(ledger, monkeypatch, 4_000, PERPLEX, 70_000)
    pay(ledger, monkeypatch, 4_100, EXA, 10_000, network="eip155:1")  # another chain
    pay(ledger, monkeypatch, 3_500, EXA, 10_000, agent="writer")  # no wallet listed
    pay(ledger, monkeypatch, 5_000, EXA, 10_000)  # November
    FakeBase.logs = [
        log(301, OURS, EXA, 250_000, "0x" + "01" * 32),
        log(2_503, OURS, PERPLEX, 1_000_000, "0x" + "02" * 32),
        log(3_001, OURS, EXA, 600_000, "0x" + "03" * 32),  # paid more than was settled
        log(3_900, OURS, STRANGER, 5_000_000, "0x" + "04" * 32),  # never asked Quarter
        log(3_901, OTHER, STRANGER, 9_000_000, "0x" + "05" * 32),  # not a listed wallet
        log(100, OURS, STRANGER, 1, "0x" + "06" * 32),  # September
    ]
    listed = wallets.read_wallets({"0x" + OURS[2:].upper(): "researcher"})
    found = wallets.reconcile(ledger, listed, "2026-10", wallets.Rpc(base))

    assert found["blocks"] == (144, 4607)
    assert [t["amount"] for t, _ in found["matched"]] == [250_000, 1_000_000]
    assert found["matched"][0][1]["call_id"] == by_tx
    assert [(t["to"], t["amount"]) for t in found["outside"]] == [(STRANGER, 5_000_000)]
    assert [s["call_id"] for s in found["missing"]] == [missing]
    (t, s), = found["mismatched"]
    assert (t["amount"], s["call_id"], s["amount"]) == (600_000, short, 500_000)
    assert t["at"] == at_block(3_001)

    pages = [(lo, hi) for lo, hi in FakeBase.getlogs if hi - lo + 1 <= 500]
    assert FakeBase.getlogs[:2] == [(144, 2143), (144, 1143)]  # too large twice, then halved
    assert pages[0] == (144, 643) and pages[-1][1] == 4607
    assert all(b[0] == a[1] + 1 for a, b in pairwise(pages))
    assert len(FakeBase.getlogs) == 2 + len(pages) == 2 + 9


def test_reconcile_waits_out_a_rate_limit(base):
    FakeBase.busy = 2
    waits = []
    rpc = wallets.Rpc(base, sleep=waits.append)
    assert rpc.call("eth_blockNumber", []) == hex(LATEST) and waits == [1, 2]
    FakeBase.busy = wallets.RETRIES + 1
    with pytest.raises(wallets.RpcError, match="rate limiting"):
        rpc.call("eth_blockNumber", [])


def test_reconcile_command_reports_and_exits_1(tmp_path, base, monkeypatch, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    pay(ledger, monkeypatch, 300, EXA, 250_000, tx="0x" + "01" * 32)
    FakeBase.logs = [log(301, OURS, EXA, 250_000, "0x" + "01" * 32),
                     log(900, OURS, STRANGER, 2_000_000, "0x" + "04" * 32)]
    (tmp_path / "wallets.json").write_text(json.dumps({OURS: "researcher"}))
    with pytest.raises(SystemExit) as stop:
        main(["--db", db, "wallets", "reconcile", "--month", "2026-10",
              "--wallets", str(tmp_path / "wallets.json"), "--rpc", base])
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "2 USDC transfers" in out and "1 match" in out
    outside = out.split("Paid outside Quarter")[1]
    assert f"-> {STRANGER}" in outside and "$      2.00" in outside and "0x" + "04" * 32 in outside

    FakeBase.logs = FakeBase.logs[:1]
    with pytest.raises(SystemExit) as stop:
        main(["--db", db, "wallets", "reconcile", "--month", "2026-10",
              "--wallets", str(tmp_path / "wallets.json"), "--rpc", base])
    assert stop.value.code == 0


def test_reconcile_errors_are_messages(tmp_path, base):
    db = str(tmp_path / "l.db")
    good = json.dumps({OURS: "researcher"})
    for args, says in [
        (["--month", "2026-13", "--wallets", good, "--rpc", base], "month is YYYY-MM"),
        (["--month", "2026-10", "--wallets", '{"0x12": "a"}', "--rpc", base], "not a wallet"),
        (["--month", "2026-10", "--wallets", good, "--rpc", "http://127.0.0.1:9/key-abc"],
         "could not be read"),
    ]:
        with pytest.raises(SystemExit) as stop:
            main(["--db", db, "wallets", "reconcile", *args])
        assert says in str(stop.value.code) and "key-abc" not in str(stop.value.code)


CAPS = [
    Cap("all", "*", "month", to_micros(500)),
    Cap("agent", "researcher", "day", to_micros(20)),
    Cap("agent", "*", "month", to_micros(5), over="approve"),  # asks a person: refuses nothing
    Cap("task", "*", "day", to_micros(1)),  # only calls naming a task
]


def test_cdp_policy_document_shape():
    doc = wallets.cdp_policy(CAPS, {"x402": [EXA, PERPLEX], "unlisted": "deny"})
    assert set(doc) == {"scope", "description", "rules"} and doc["scope"] == "project"
    assert len(doc["description"]) <= 50 and all(c.isalnum() or c in " ,." for c in doc["description"])
    typed, send, sign, hashes = doc["rules"]
    assert [r["operation"] for r in doc["rules"]] == [
        "signEvmTypedData", "sendEvmTransaction", "signEvmTransaction", "signEvmHash"]
    assert [r["action"] for r in doc["rules"]] == ["accept", "accept", "accept", "reject"]
    assert hashes == {"action": "reject", "operation": "signEvmHash"}

    contract, field = typed["criteria"]
    assert contract == {"type": "evmTypedDataVerifyingContract", "addresses": [wallets.USDC],
                        "operator": "in"}
    assert field["types"]["primaryType"] == "TransferWithAuthorization"
    assert [f["name"] for f in field["types"]["types"]["TransferWithAuthorization"]] == [
        "from", "to", "value", "validAfter", "validBefore", "nonce"]
    assert field["conditions"] == [
        {"addresses": [EXA, PERPLEX], "operator": "in", "path": "to"},
        {"value": "500000000", "operator": "<=", "path": "value"},  # $500 in USDC units
    ]

    assert [c["type"] for c in send["criteria"]] == ["evmAddress", "ethValue", "evmData", "evmNetwork"]
    assert send["criteria"][3] == {"type": "evmNetwork", "networks": ["base"], "operator": "in"}
    assert sign["criteria"] == send["criteria"][:3]
    data = send["criteria"][2]
    assert data["abi"] == "erc20" and data["conditions"] == [{"function": "transfer", "params": [
        {"name": "to", "operator": "in", "values": [EXA, PERPLEX]},
        {"name": "value", "operator": "<=", "value": "500000000"},
    ]}]

    own = wallets.cdp_policy(CAPS, {}, agent="researcher")
    assert own["scope"] == "account"
    assert own["rules"][0]["criteria"][1]["conditions"] == [
        {"value": "20000000", "operator": "<=", "path": "value"}]
    assert own["rules"][1]["criteria"][2]["conditions"][0]["params"] == [
        {"name": "value", "operator": "<=", "value": "20000000"}]


def test_cdp_policy_refuses_what_it_cannot_express(tmp_path, monkeypatch, capsys):
    with pytest.raises(ValueError, match="nothing for the wallet to refuse"):
        wallets.cdp_policy([], {})
    with pytest.raises(ValueError, match="not an address"):
        wallets.cdp_policy([], {"x402": ["acme.com"]})
    with pytest.raises(ValueError, match="1 to 300"):
        wallets.cdp_policy([], {"x402": [EXA] * 301})

    monkeypatch.setenv("QUARTER_PAYEES", json.dumps({"x402": [EXA]}))
    main(["--db", str(tmp_path / "l.db"), "wallets", "policy", "--format", "cdp",
          "--caps", '[{"scope": "all", "limit_usd": "40"}]'])
    printed = capsys.readouterr()
    assert json.loads(printed.out)["rules"][0]["criteria"][1]["conditions"][1]["value"] == "40000000"
    assert "approve a payee off the list" in printed.err


def test_privy_policy_document_shape():
    doc = wallets.privy_policy(CAPS, {"x402": [EXA, PERPLEX], "unlisted": "deny"})
    assert set(doc) == {"version", "name", "chain_type", "rules"}
    assert (doc["version"], doc["chain_type"]) == ("1.0", "ethereum") and len(doc["name"]) <= 100
    assert all(len(r["name"]) <= 50 for r in doc["rules"])
    assert {r["action"] for r in doc["rules"]} == {"ALLOW"}  # Privy denies any method with no rule
    typed, viem, send, sign = doc["rules"]
    assert [r["method"] for r in doc["rules"]] == [
        "eth_signTypedData_v4", "eth_signTypedData_v4", "eth_sendTransaction", "eth_signTransaction"]

    domain = [{"field_source": "ethereum_typed_data_domain", "field": "chainId", "operator": "eq",
               "value": "8453"},
              {"field_source": "ethereum_typed_data_domain", "field": "verifyingContract",
               "operator": "eq", "value": wallets.USDC}]
    assert typed["conditions"][:2] == viem["conditions"][:2] == domain
    message = typed["conditions"][2:]
    assert [(c["field_source"], c["field"], c["operator"], c["value"]) for c in message] == [
        ("ethereum_typed_data_message", "to", "in", [EXA, PERPLEX]),
        ("ethereum_typed_data_message", "value", "lte", "500000000"),  # $500 in USDC units
    ]
    shape = message[0]["typed_data"]
    assert shape["primary_type"] == "TransferWithAuthorization"
    assert list(shape["types"]) == ["TransferWithAuthorization"]
    assert [f["name"] for f in shape["types"]["TransferWithAuthorization"]] == [
        "from", "to", "value", "validAfter", "validBefore", "nonce"]
    with_domain = viem["conditions"][2]["typed_data"]["types"]
    assert list(with_domain) == ["EIP712Domain", "TransferWithAuthorization"]
    assert [f["name"] for f in with_domain["EIP712Domain"]] == [
        "name", "version", "chainId", "verifyingContract"]

    assert send["conditions"] == sign["conditions"]
    tx, calldata = send["conditions"][:3], send["conditions"][3:]
    assert tx == [
        {"field_source": "ethereum_transaction", "field": "to", "operator": "eq", "value": wallets.USDC},
        {"field_source": "ethereum_transaction", "field": "value", "operator": "lte", "value": "0"},
        {"field_source": "ethereum_transaction", "field": "chain_id", "operator": "eq", "value": "8453"},
    ]
    assert [(c["field_source"], c["field"], c["operator"], c["value"]) for c in calldata] == [
        ("ethereum_calldata", "transfer.to", "in", [EXA, PERPLEX]),
        ("ethereum_calldata", "transfer.value", "lte", "500000000"),
    ]
    abi, = calldata[0]["abi"]
    assert abi["name"] == "transfer" and [i["name"] for i in abi["inputs"]] == ["to", "value"]

    own = wallets.privy_policy(CAPS, {}, agent="researcher")
    assert own["name"].endswith("for researcher")
    assert [(c["field"], c["value"]) for c in own["rules"][0]["conditions"][2:]] == [
        ("value", "20000000")]
    assert [(c["field"], c["value"]) for c in own["rules"][2]["conditions"][3:]] == [
        ("transfer.value", "20000000")]


def test_privy_policy_refuses_what_it_cannot_express(tmp_path, monkeypatch, capsys):
    with pytest.raises(ValueError, match="nothing for the wallet to refuse"):
        wallets.privy_policy([], {})
    with pytest.raises(ValueError, match="not an address"):
        wallets.privy_policy([], {"x402": ["acme.com"]})
    with pytest.raises(ValueError, match="Privy takes 1 to 100"):
        wallets.privy_policy([], {"x402": [EXA] * 101})

    monkeypatch.setenv("QUARTER_PAYEES", json.dumps({"x402": [EXA]}))
    main(["--db", str(tmp_path / "l.db"), "wallets", "policy", "--format", "privy",
          "--caps", '[{"scope": "all", "limit_usd": "40"}]'])
    printed = capsys.readouterr()
    assert json.loads(printed.out)["rules"][0]["conditions"][3]["value"] == "40000000"
    assert "approve a payee off the list" in printed.err
