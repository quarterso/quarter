"""What was spent, what is left, and what is waiting, at a glance.

Days and months are UTC, as the caps count them. "Used" counts charges and open holds,
the same sum the gateway checks a call against. Observed spend (connect.py) shows beside it.
"""

from datetime import UTC, datetime

from .insights import forecast
from .money import usd_shown
from .policy import Cap, cap_label, window_start
from .store import Ledger

BAR = 20


def cap_usage(ledger: Ledger, caps: list[Cap], now: datetime) -> list[dict]:
    """One row per cap and id it counts: a wildcard cap gives a row per id with spend."""
    rows = []
    for cap in caps:
        since = window_start(cap.window, now)
        if cap.scope == "all":
            targets = ["all"]
        elif cap.key == "*":
            targets = sorted(ledger.spend_by(cap.scope, since))
        else:
            targets = [cap.key]
        for target in targets:
            used = ledger.spent(cap.scope, target, since)
            rows.append({"cap": cap_label(cap, target), "window": cap.window,
                         "used": used, "limit": cap.limit, "left": max(cap.limit - used, 0),
                         "observed": ledger.spent(cap.scope, target, since, observed=True) - used})
    return rows


def budget_left(ledger: Ledger, caps: list[Cap], ids: dict[str, str | None],
                now: datetime) -> list[dict]:
    """Each cap that applies to a call with these ids, and what is left under it."""
    rows = []
    for cap in caps:
        target = cap.applies_to(ids)
        if target is None:
            continue
        used = ledger.spent(cap.scope, target, window_start(cap.window, now))
        rows.append({"cap": cap_label(cap, target), "window": cap.window, "used": used,
                     "limit": cap.limit, "left": max(cap.limit - used, 0),
                     "approve_above": cap.approve_above})
    return rows


def bar(used: int, limit: int) -> str:
    filled = BAR if limit <= 0 else min(BAR, round(BAR * used / limit))
    return "[" + "#" * filled + "." * (BAR - filled) + "]"


def render(ledger: Ledger, caps: list[Cap], now: datetime | None = None,
           color: bool = False, rules: list[str] | None = None, sanctions=None) -> str:
    now = now or datetime.now(UTC)
    red, yellow, reset = ("\033[31m", "\033[33m", "\033[0m") if color else ("", "", "")
    today = sum(ledger.spend_by("agent", window_start("day", now)).values())
    month = sum(ledger.spend_by("agent", window_start("month", now)).values())
    header = f"Spent today {usd_shown(today)}, this month {usd_shown(month)}"
    lines = [f"{header}  ({now:%Y-%m-%d} UTC)"]

    usage = cap_usage(ledger, caps, now)
    if usage:
        lines += ["", "Caps"]
    for row in usage:
        pct = 100 * row["used"] // row["limit"] if row["limit"] else 100
        tint = red if pct >= 100 else yellow if pct >= 80 else ""
        lines.append(
            f"  {row['cap'][:22]:<22} {row['window']:<5} {tint}{bar(row['used'], row['limit'])}"
            f"{reset} {usd_shown(row['used'])} of {usd_shown(row['limit'])}"
        )
        if row["observed"]:  # booked by connect: counts here, never blocks
            lines.append(f"  {'':<22}       + {usd_shown(row['observed'])} observed, alert only")
    if not caps:
        lines += ["", "No caps set: spend is recorded, not limited."]

    saved, calls = ledger.saved_since(window_start("month", now))
    if calls:
        lines += ["", f"Prompt caching Quarter added saved {usd_shown(saved)} this month, on {calls} calls"]

    out = forecast(ledger, caps, now)
    if out["enough_data"]:
        rate = f"the last {out['rate_days']:g} days' rate"
        lines += ["", f"Month end, at {rate}: {usd_shown(out['month_end'])}"]
    else:
        lines += ["", "Month end: not enough data yet (less than a day of spend)"]
    for w in out["cap_warnings"]:
        when = "already reached" if w["reached"] == "already" else f"reached {w['reached']}"
        lines.append(f"  {red}{w['cap']} cap {usd_shown(w['limit'])}: {when}{reset}")

    waiting = ledger.approvals()
    lines += ["", f"Approvals waiting: {len(waiting)}"]
    for a in waiting:
        lines.append(f"  {a['id']}  {(a['agent'] or '')[:16]:<16} {usd_shown(a['estimate'])}"
                     f"  {(a['reason'] or '')[:26]}")

    paused = ledger.paused_agents()
    lines += ["", f"Paused agents: {len(paused)}"]
    for p in paused:
        who = f"task {p['task']}" if p["task"] else p["agent"]
        lines.append(f"  {who[:20]:<20} since {p['at'][:16]}  {(p['reason'] or '')[:30]}")

    if rules:
        lines += ["", f"Rules: {len(rules)}"] + [f"  {line[:76]}" for line in rules]
    if sanctions:  # sanctions.Screen
        tint = red if sanctions.stale(now) else ""
        lines += ["", f"{tint}{sanctions.summary(now)}{reset}"]
    return "\n".join(lines) + "\n"
