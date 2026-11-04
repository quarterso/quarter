"""Where the month will land: month-to-date spend plus the last seven days' daily rate
for the rest of the month, and the date each monthly cap would be reached.

The seven days reach back into last month, so the first days of a month are projected
from a full week. A ledger younger than seven days uses the time since its first
charge, and one younger than a day projects nothing: a few hours of spend stretched
over a month is noise, not a forecast.
"""

from calendar import monthrange
from datetime import UTC, datetime, timedelta

from .policy import Cap, cap_label, window_start
from .store import Ledger

RATE_DAYS = 7
MIN_RATE_DAYS = 1  # history needed before projecting


def _rate_since(ledger: Ledger, now: datetime) -> datetime:
    """Start of the rate window: seven days back, or the first charge if that is later."""
    since = now - timedelta(days=RATE_DAYS)
    first = ledger.db.execute("select min(at) from entries where kind = 'charge'").fetchone()[0]
    return max(since, datetime.fromisoformat(first)) if first else since


def _daily(ledger: Ledger, scope: str, now: datetime) -> tuple[dict, dict | None, float]:
    """Month-to-date and daily rate per id, and the number of days the rate covers.
    The rate is None when the ledger has less than a day of history."""
    since = _rate_since(ledger, now)
    days = (now - since).total_seconds() / 86400
    mtd = ledger.spend_by(scope, window_start("month", now))
    if days < MIN_RATE_DAYS:
        return mtd, None, days
    return mtd, {k: v / days for k, v in ledger.spend_by(scope, since).items()}, days


def forecast(ledger: Ledger, caps: list[Cap], now: datetime | None = None) -> dict:
    """Where the month will land. Without a day of history, month_end and per_day are
    None and enough_data is False; caps already reached are still reported."""
    now = now or datetime.now(UTC)
    days_in_month = monthrange(now.year, now.month)[1]
    month_end = window_start("month", now) + timedelta(days=days_in_month)
    left = (month_end - now).total_seconds() / 86400

    mtd, rate, rate_days = _daily(ledger, "agent", now)
    enough = rate is not None
    agents = sorted(
        (
            {
                "agent": a,
                "so_far": mtd[a],
                "per_day": round(rate.get(a, 0)) if enough else None,
                "month_end": round(mtd[a] + rate.get(a, 0) * left) if enough else None,
            }
            for a in mtd
        ),
        key=lambda r: -(r["month_end"] if enough else r["so_far"]),
    )

    warnings = []
    for cap in caps:
        if cap.window != "month" or (cap.key == "*" and cap.scope != "all"):
            continue
        if cap.scope == "all":
            spent = sum(mtd.values())
            daily = sum(rate.values()) if enough else 0
        else:
            used, per_day, _ = _daily(ledger, cap.scope, now)
            spent = used.get(cap.key, 0)
            daily = per_day.get(cap.key, 0) if enough else 0
        label = cap_label(cap, cap.key)
        projected = spent + daily * left
        if spent >= cap.limit:
            warnings.append(
                {
                    "cap": label,
                    "limit": cap.limit,
                    "projected": round(projected),
                    "reached": "already",
                }
            )
        elif enough and projected > cap.limit and daily > 0:
            when = now + timedelta(days=(cap.limit - spent) / daily)
            warnings.append(
                {
                    "cap": label,
                    "limit": cap.limit,
                    "projected": round(projected),
                    "reached": f"{when:%Y-%m-%d}",
                }
            )

    return {
        "month": f"{now:%Y-%m}",
        "so_far": sum(r["so_far"] for r in agents),
        "month_end": sum(r["month_end"] for r in agents) if enough else None,
        "enough_data": enough,
        "rate_days": round(max(rate_days, 0), 1),
        "agents": agents,
        "cap_warnings": warnings,
    }
