import threading

import pytest

from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.store import Ledger

AGENT = {"agent": "researcher", "task": "t1", "customer": "acme"}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


def test_allows_under_cap_and_blocks_over(ledger):
    caps = [Cap("agent", "researcher", "day", to_micros(1))]
    decision, call = ledger.hold(caps, AGENT, "openai", "m", to_micros("0.6"))
    assert decision.outcome == "allow"
    ledger.settle(call, to_micros("0.6"))
    decision, call = ledger.hold(caps, AGENT, "openai", "m", to_micros("0.5"))
    assert decision.outcome == "block" and call is None


def test_open_holds_count_against_the_cap(ledger):
    caps = [Cap("customer", "*", "month", to_micros(1))]
    assert ledger.hold(caps, AGENT, "v", "m", to_micros("0.7"))[0].outcome == "allow"
    assert ledger.hold(caps, AGENT, "v", "m", to_micros("0.7"))[0].outcome == "block"


def test_settle_replaces_hold_with_actual(ledger):
    _, call = ledger.hold([], AGENT, "v", "m", to_micros(5))
    ledger.settle(call, to_micros("0.25"))
    assert ledger.spent("agent", "researcher", None) == to_micros("0.25")
    assert ledger.totals("customer") == [("acme", "v", to_micros("0.25"))]


def test_failed_call_costs_nothing(ledger):
    _, call = ledger.hold([], AGENT, "v", "m", to_micros(5))
    ledger.settle(call, None)
    assert ledger.spent("agent", "researcher", None) == 0


def test_settle_twice_is_refused(ledger):
    _, call = ledger.hold([], AGENT, "v", "m", 10)
    ledger.settle(call, 5)
    with pytest.raises(KeyError):
        ledger.settle(call, 5)


def test_approval_limit(ledger):
    caps = [Cap("agent", "*", "total", to_micros(100), approve_above=to_micros(2))]
    assert ledger.hold(caps, AGENT, "v", "m", to_micros(3))[0].outcome == "approve"


def test_cap_for_another_agent_does_not_apply(ledger):
    caps = [Cap("agent", "other", "day", 0)]
    assert ledger.hold(caps, AGENT, "v", "m", to_micros(1))[0].outcome == "allow"


def test_parallel_calls_never_overspend(tmp_path):
    path = str(tmp_path / "ledger.db")
    caps = [Cap("agent", "researcher", "total", to_micros(10))]
    allowed = []

    def worker():
        ledger = Ledger(path)  # one connection per thread, like separate requests
        for _ in range(10):
            decision, _ = ledger.hold(caps, AGENT, "v", "m", to_micros(1))
            allowed.append(decision.outcome == "allow")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == 10


def test_one_ledger_shared_by_many_threads_never_overspends(tmp_path):
    shared = Ledger(str(tmp_path / "ledger.db"))  # as the gateway uses it
    caps = [Cap("agent", "researcher", "total", to_micros(10))]
    allowed, errors = [], []

    def worker():
        try:
            for _ in range(10):
                decision, ref = shared.hold(caps, AGENT, "v", "m", to_micros(1))
                allowed.append(decision.outcome == "allow")
                if ref:
                    shared.settle(ref, to_micros(1))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sum(allowed) == 10


def test_window_sums_match_a_fresh_sum_across_processes_and_rollbacks(tmp_path):
    """spent keeps a running sum per cap window; it must always equal summing afresh, with
    another process writing too and after a transaction that rolled back."""
    from datetime import UTC, datetime, timedelta

    path = str(tmp_path / "l.db")
    mine, other = Ledger(path), Ledger(path)
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    windows = [None, midnight, midnight - timedelta(days=40), datetime.now(UTC) - timedelta(minutes=5)]

    def fresh(scope, value, since):
        sql = "select coalesce(sum(case kind when 'release' then -amount else amount end), 0) from entries"
        where, args = ([] if scope == "all" else [f"{scope} = ?"]), ([] if scope == "all" else [value])
        if since:
            where, args = [*where, "at >= ?"], [*args, since.isoformat()]
        sql += " where " + " and ".join(where) if where else ""
        return mine.db.execute(sql, args).fetchone()[0]

    def check():
        for scope, value in (("all", "all"), ("agent", "researcher"), ("customer", "acme"), ("agent", "b")):
            for since in windows:
                assert mine.spent(scope, value, since) == fresh(scope, value, since)

    for i in range(6):
        writer = mine if i % 2 else other
        _, call = writer.hold([], {**AGENT, "agent": "researcher" if i % 3 else "b"}, "v", "m", 1000 + i)
        check()
        writer.settle(call, 300 + i)
        check()
    other.db.execute(  # an entry dated before the windows still counts toward the total only
        "insert into entries (call_id, kind, at, agent, customer, vendor, amount)"
        " values ('old', 'charge', '2020-01-01T00:00:00+00:00', 'researcher', 'acme', 'v', 7)"
    )
    check()
    with pytest.raises(RuntimeError), mine._write():
        mine._append("gone", "charge", AGENT, "v", "m", 999)
        assert mine.spent("agent", "researcher", midnight) == fresh("agent", "researcher", midnight)
        raise RuntimeError
    check()
