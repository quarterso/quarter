from concurrent.futures import ThreadPoolExecutor

from quarter_ledger.policy import load_caps
from quarter_ledger.store import Ledger


def test_parallel_calls_never_pass_a_cap(tmp_path):
    """50 calls of $1 at once against a $10 cap: exactly 10 get through.

    Each worker opens its own Ledger, as separate gateway processes would."""
    path = str(tmp_path / "l.db")
    Ledger(path)
    caps = load_caps([{"scope": "all", "window": "month", "limit_usd": "10"}])

    def call(i):
        _, call_id = Ledger(path).hold(caps, {"agent": f"a{i % 5}"}, "openai", "m", 1_000_000)
        return call_id

    with ThreadPoolExecutor(max_workers=50) as pool:
        allowed = [c for c in pool.map(call, range(50)) if c]
    assert len(allowed) == 10
    assert Ledger(path).spent("all", "all", None) == 10_000_000
