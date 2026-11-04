"""Runaway-loop stop: pause an agent that suddenly spends far faster than it usually does.

An agent trips when its spend in the last window passes both a floor and a multiple of
its average spend per window over the previous day. An agent also trips when it sends
the same request body "repeats" times inside the window, the shape of a retry loop that
spends little per call. A tripped agent is paused until a person resumes it, so a loop costs minutes of spend, not a day's budget.

A task trips when its model makes the same tool call, same name and arguments, and gets the
same result back "tool_repeats" times inside the window. Agent loops grow the body every turn,
so the body check misses them; the repeat is in the tool calls. A call repeated while its
result changes is usually work (tests rerun after each edit), so it trips only at three times
that count. Only that task is paused, not the agent.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .money import to_micros, usd_display
from .store import GATEWAY_ONLY, Ledger

BASELINE = timedelta(hours=24)


@dataclass(frozen=True)
class RunawayRule:
    window: timedelta = timedelta(minutes=5)
    multiple: float = 10
    floor: int = to_micros(5)
    repeats: int = 20
    tool_repeats: int = 8
    shadow: bool = False  # "mode": "shadow": record the pause it would make, pause nothing


def load_runaway(raw: str | None) -> RunawayRule | None:
    """QUARTER_RUNAWAY: unset for the defaults, "off" to disable, or JSON
    {"window_minutes", "multiple", "floor_usd", "repeats", "tool_repeats", "mode"}."""
    if raw is None or not raw.strip():
        return RunawayRule()
    if raw.strip().lower() == "off":
        return None
    cfg = json.loads(raw)
    if cfg.get("mode", "enforce") not in ("enforce", "shadow"):
        raise ValueError(f'QUARTER_RUNAWAY "mode" is "enforce" or "shadow", not {cfg["mode"]!r}')
    base = RunawayRule()
    return RunawayRule(
        timedelta(minutes=float(cfg.get("window_minutes", base.window.total_seconds() / 60))),
        float(cfg.get("multiple", base.multiple)),
        to_micros(cfg["floor_usd"]) if "floor_usd" in cfg else base.floor,
        int(cfg.get("repeats", base.repeats)),
        int(cfg.get("tool_repeats", base.tool_repeats)),
        cfg.get("mode") == "shadow",
    )


def check(ledger: Ledger, agent: str, rule: RunawayRule, now: datetime | None = None) -> str | None:
    """The reason to pause this agent, or None."""
    now = now or datetime.now(UTC)
    recent = ledger.spent("agent", agent, now - rule.window)
    if recent <= rule.floor:
        return None
    start, end = now - rule.window - BASELINE, now - rule.window
    before = ledger.db.execute(
        "select coalesce(sum(amount), 0) from entries"
        f" where kind = 'charge' and agent = ? and at >= ? and at < ? and {GATEWAY_ONLY}",
        (agent, start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    usual = before * (rule.window / BASELINE)
    if recent <= rule.multiple * usual:
        return None
    minutes = rule.window.total_seconds() / 60
    return (
        f"spent ${usd_display(recent)} in {minutes:g} minutes,"
        f" against a usual ${usd_display(round(usual))}"
    )
