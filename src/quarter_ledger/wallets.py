"""The company's own agent wallets: check them against the ledger, and make them refuse
what Quarter refuses. Neither needs an account, a key or any money in Quarter's hands.

Reconcile reads a month of USDC Transfer events out of the company's wallets on Base through
a public JSON-RPC endpoint and matches them to the ledger's x402 settlements. USDC has
6 decimals, so one on-chain unit is one micro-dollar and amounts compare as they are.

The policy export writes a Coinbase Developer Platform (CDP) Policy Engine document from the
caps and the x402 payee list. The company creates it in CDP itself; Quarter never calls CDP.

Sources, read 2026-11-03:
- USDC on Base is 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913:
  https://developers.circle.com/stablecoins/usdc-contract-addresses
  Its decimals() returns 6 (read from the contract through https://mainnet.base.org).
- Base's public RPC is https://mainnet.base.org, chain id 8453:
  https://docs.base.org/get-started/connect-to-base
- eth_getLogs: "Keep fromBlock-to-toBlock ranges under 2,000 blocks", and providers may
  enforce their own limits:
  https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_getLogs
- The policy document is the body of CDP's Create policy request:
  https://docs.cdp.coinbase.com/api-reference/v2/rest-api/policy-engine/create-policy
  https://docs.cdp.coinbase.com/wallets/security-and-policies/policy-engine/evm-policies
- An x402 "exact" payment on EVM is a signed EIP-3009 TransferWithAuthorization:
  https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md
"""

import json
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

from .policy import Cap
from .store import Ledger

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE = "eip155:8453"
PUBLIC_RPC = "https://mainnet.base.org"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
LOG_RANGE = 2_000  # blocks per eth_getLogs; halved when the endpoint says it is too many
RETRIES = 4  # tries again after HTTP 429, waiting 1, 2, 4 and 8 seconds
MATCH_WINDOW = timedelta(hours=1)  # a transfer and its settlement are this close in time
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class Rpc:
    """JSON-RPC over HTTP. The URL may hold a provider's key, so it is never in a message."""

    def __init__(self, url: str, sleep=time.sleep):
        self.url, self.sleep = url, sleep

    def call(self, method: str, params: list):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        request = urllib.request.Request(
            self.url, data=body.encode(),
            # Base's public endpoint refuses urllib's default user agent (Cloudflare 1010)
            headers={"content-type": "application/json", "user-agent": "quarter-ledger"},
        )
        for attempt in range(RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    reply = json.loads(response.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < RETRIES:
                    self.sleep(2**attempt)
                    continue
                if e.code == 429:
                    raise RpcError("the RPC endpoint is rate limiting (HTTP 429): try later,"
                                   " or pass --rpc with a provider's endpoint") from None
                try:
                    reply = json.loads(e.read())  # some answer a JSON-RPC error with a 4xx
                except ValueError:
                    raise RpcError(f"the RPC endpoint answered HTTP {e.code}") from None
                break
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                raise RpcError(f"the RPC endpoint could not be read: {getattr(e, 'reason', e)}") from None
        if not isinstance(reply, dict):
            raise RpcError("the RPC endpoint did not answer JSON-RPC")
        if reply.get("error"):
            error = reply["error"] if isinstance(reply["error"], dict) else {"message": reply["error"]}
            raise RpcError(str(error.get("message", "")), error.get("code"))
        return reply.get("result")


def too_many(e: RpcError) -> bool:
    """Whether an eth_getLogs error asks for a smaller block range. Providers word it
    differently: "block range is too large", "query exceeds max block range",
    "query returned more than 10000 results", or code -32005 (limit exceeded)."""
    text = str(e).lower()
    return e.code == -32005 or any(w in text for w in ("range", "too large", "too many", "more than"))


def block_time(rpc: Rpc, number: int) -> int:
    block = rpc.call("eth_getBlockByNumber", [hex(number), False])
    if not block:
        raise RpcError(f"the RPC endpoint has no block {number}")
    return int(block["timestamp"], 16)


def first_block_at(rpc: Rpc, when: int, latest: int) -> int:
    """The first block with a timestamp at or after `when`; latest + 1 if none yet."""
    lo, hi = 0, latest + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if block_time(rpc, mid) < when:
            lo = mid + 1
        else:
            hi = mid
    return lo


def month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError(f"month is YYYY-MM, not {month!r}") from None
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def topic(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def transfers(rpc: Rpc, wallets: list[str], first: int, last: int) -> list[dict]:
    """USDC Transfer events out of these wallets in blocks first to last, page by page."""
    topics = [TRANSFER, [topic(w) for w in wallets]]
    logs, span, lo = [], LOG_RANGE, first
    while lo <= last:
        hi = min(last, lo + span - 1)
        query = {"address": USDC, "fromBlock": hex(lo), "toBlock": hex(hi), "topics": topics}
        try:
            logs += rpc.call("eth_getLogs", [query]) or []
        except RpcError as e:
            if span > 1 and too_many(e):
                span //= 2
                continue
            raise
        lo = hi + 1
    times: dict[int, int] = {}
    out = []
    for log in logs:
        if log.get("removed"):
            continue
        number = int(log["blockNumber"], 16)
        if number not in times:
            stamp = log.get("blockTimestamp")
            times[number] = int(stamp, 16) if stamp else block_time(rpc, number)
        out.append({
            "tx": log["transactionHash"].lower(),
            "from": "0x" + log["topics"][1][-40:].lower(),
            "to": "0x" + log["topics"][2][-40:].lower(),
            "amount": int(log["data"], 16),  # USDC units, which are micro-dollars
            "at": datetime.fromtimestamp(times[number], UTC),
            "block": number,
        })
    return out


def settlements(ledger: Ledger, start: datetime, end: datetime) -> list[dict]:
    """The ledger's x402 charges from `start` to `end`, with the tx and network the agent
    reported when it settled."""
    reported = {}
    for (detail,) in ledger.db.execute(
        "select detail from audit where action = 'payment_settled' and detail like '% x402 %'"
    ):
        call_id, _, *rest = detail.split(" ")
        fields = dict(f.split("=", 1) for f in rest[1:] if "=" in f)
        reported[call_id] = (fields.get("tx", "").lower(), fields.get("network", ""))
    rows = ledger.db.execute(
        "select call_id, at, agent, vendor, amount from entries"
        " where kind = 'charge' and model = 'x402' and at >= ? and at < ? order by at",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out = []
    for call_id, at, agent, vendor, amount in rows:
        tx, network = reported.get(call_id, ("", ""))
        if network and network != BASE:
            continue  # paid on another chain
        out.append({"call_id": call_id, "at": datetime.fromisoformat(at), "agent": agent,
                    "payee": vendor.split(":", 1)[1].lower(), "amount": amount, "tx": tx})
    return out


def read_wallets(raw) -> dict[str, str]:
    """wallets.json: {"0x...": "agent name"}, each agent wallet and the agent that pays from it."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError('wallets file is {"0x... wallet address": "agent name"}')
    bad = [a for a in raw if not ADDRESS.match(a)]
    if bad:
        raise ValueError(f"not a wallet address: {bad[0]}")
    return {a.lower(): str(agent) for a, agent in raw.items()}


def reconcile(ledger: Ledger, wallets: dict[str, str], month: str, rpc: Rpc) -> dict:
    start, end = month_bounds(month)
    latest = int(rpc.call("eth_blockNumber", []), 16)
    first = first_block_at(rpc, int(start.timestamp()), latest)
    last = min(latest, first_block_at(rpc, int(end.timestamp()), latest) - 1)
    moved = transfers(rpc, list(wallets), first, last) if first <= last else []
    agents = set(wallets.values())
    paid = [s for s in settlements(ledger, start - MATCH_WINDOW, end + MATCH_WINDOW)
            if s["agent"] in agents]
    matched, mismatched, unclaimed = [], [], list(paid)

    def take(t, test):
        near = [s for s in unclaimed if test(t, s)]
        if not near:
            return None
        s = min(near, key=lambda s: abs(s["at"] - t["at"]))
        unclaimed.remove(s)
        return s

    def same_tx(t, s):
        return s["tx"] == t["tx"]

    def same_payee(t, s):  # only settlements with no tx reported are matched this way
        return (not s["tx"] and wallets[t["from"]] == s["agent"] and s["payee"] == t["to"]
                and abs(s["at"] - t["at"]) <= MATCH_WINDOW)

    def same_payment(t, s):
        return same_payee(t, s) and s["amount"] == t["amount"]

    outside = []
    for t in moved:
        s = take(t, same_tx) or take(t, same_payment) or take(t, same_payee)
        if s is None:
            outside.append(t)
        elif s["payee"] == t["to"] and s["amount"] == t["amount"]:
            matched.append((t, s))
        else:
            mismatched.append((t, s))
    missing = [s for s in unclaimed if start <= s["at"] < end]
    return {"month": month, "blocks": (first, last), "transfers": moved, "matched": matched,
            "outside": outside, "missing": missing, "mismatched": mismatched}


# The EIP-712 type a wallet signs for an x402 payment (EIP-3009).
TRANSFER_WITH_AUTHORIZATION = {
    "types": {"TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]},
    "primaryType": "TransferWithAuthorization",
}


def largest_payment(caps: list[Cap], agent: str | None) -> int | None:
    """The most one payment can be before Quarter refuses it outright: the smallest
    blocking cap that counts every payment the agent makes (or every agent's, with none).
    Caps that ask a person, caps in shadow, and task or customer caps, refuse nothing for certain."""
    keys = {"*"} | ({agent} if agent else set())
    limits = [c.limit for c in caps if c.over == "block" and c.mode == "enforce"
              and (c.scope == "all" or (c.scope == "agent" and c.key in keys))]
    return min(limits) if limits else None


def cdp_policy(caps: list[Cap], payees: dict, agent: str | None = None) -> dict:
    """A CDP Policy Engine document (the Create policy body) that accepts only USDC
    payments on Base to the x402 payee list, each at most the largest payment Quarter
    allows, and rejects signing raw hashes, which could authorise anything."""
    allowed = payees.get("x402") if payees.get("mode") != "shadow" else None
    if allowed is not None:
        bad = [p for p in allowed if not ADDRESS.match(str(p))]
        if bad:
            raise ValueError(f"x402 payee is not an address: {bad[0]}")
        if not 0 < len(allowed) <= 300:  # CDP allows 1 to 300 addresses in a criterion
            raise ValueError(f"CDP takes 1 to 300 addresses in a list, not {len(allowed)}")
    limit = largest_payment(caps, agent)
    if allowed is None and limit is None:
        raise ValueError("no x402 payee list and no blocking cap: nothing for the wallet to refuse")
    typed, params = [], []
    if allowed is not None:
        typed.append({"addresses": allowed, "operator": "in", "path": "to"})
        params.append({"name": "to", "operator": "in", "values": allowed})
    if limit is not None:
        typed.append({"value": str(limit), "operator": "<=", "path": "value"})
        params.append({"name": "value", "operator": "<=", "value": str(limit)})
    usdc_transfer = [
        {"type": "evmAddress", "addresses": [USDC], "operator": "in"},
        {"type": "ethValue", "ethValue": "0", "operator": "<="},
        {"type": "evmData", "abi": "erc20", "conditions": [{"function": "transfer", "params": params}]},
    ]
    return {
        "scope": "account" if agent else "project",
        "description": "Quarter caps and x402 payee list",
        "rules": [
            {"action": "accept", "operation": "signEvmTypedData", "criteria": [
                {"type": "evmTypedDataVerifyingContract", "addresses": [USDC], "operator": "in"},
                {"type": "evmTypedDataField", "types": TRANSFER_WITH_AUTHORIZATION,
                 "conditions": typed},
            ]},
            {"action": "accept", "operation": "sendEvmTransaction", "criteria": [
                *usdc_transfer, {"type": "evmNetwork", "networks": ["base"], "operator": "in"},
            ]},
            {"action": "accept", "operation": "signEvmTransaction", "criteria": usdc_transfer},
            {"action": "reject", "operation": "signEvmHash"},
        ],
    }
