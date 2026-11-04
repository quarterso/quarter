from datetime import UTC, datetime, timedelta

from quarter_ledger.money import to_micros
from quarter_ledger.runaway import RunawayRule, check, load_runaway
from quarter_ledger.store import Ledger


def charge(ledger, usd, at):
    ledger.db.execute(
        "insert into entries (call_id, kind, at, agent, vendor, amount)"
        " values ('c', 'charge', ?, 'bot', 'openai', ?)",
        (at.isoformat(), to_micros(usd)),
    )


def test_trips_on_a_spike_not_on_usual_spend(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime.now(UTC)
    rule = RunawayRule(timedelta(minutes=5), 10, to_micros(5))
    for hour in range(1, 24):
        charge(ledger, 12, now - timedelta(hours=hour))  # $12 an hour, $1 per 5 minutes
    charge(ledger, 6, now - timedelta(minutes=1))
    assert check(ledger, "bot", rule, now) is None  # over the floor, under 10x usual
    charge(ledger, 9, now - timedelta(minutes=1))
    reason = check(ledger, "bot", rule, now)
    assert reason and "in 5 minutes" in reason


def test_floor_protects_new_agents(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime.now(UTC)
    charge(ledger, 4, now - timedelta(minutes=1))
    assert check(ledger, "bot", RunawayRule(), now) is None


def test_pause_resume_and_config(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    assert ledger.pause("bot", "loop") and not ledger.pause("bot", "again")
    assert ledger.paused("bot") == "loop"
    decision, ref = ledger.hold([], {"agent": "bot"}, "openai", "m", 1)
    assert decision.outcome == "block" and ref is None
    assert ledger.resume("bot", "admin") and ledger.paused("bot") is None
    assert load_runaway("off") is None
    assert load_runaway('{"floor_usd": 20}').floor == to_micros(20)
