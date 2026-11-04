"""AI vendor commitments: spend against each contract's committed amount, and where the
last 30 days' pace leaves it by the contract's end.

A month `close` reconciled and that falls wholly inside the contract counts at the vendor's
own figure, since that is what the vendor holds against the commitment. Every other day
counts at what Quarter recorded.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .money import to_micros
from .reconcile import recorded
from .store import Ledger

PACE_DAYS = 30


@dataclass(frozen=True)
class Commit:
    vendor: str
    amount: int  # micros
    start: date
    end: date  # the last day the commitment covers


def read_commits(raw: list) -> list[Commit]:
    """[{"vendor", "amount_usd", "from", "to"}], dates as YYYY-MM-DD, `to` inclusive."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("commits: a non-empty JSON list of {vendor, amount_usd, from, to}")
    out = []
    for i, c in enumerate(raw, start=1):
        try:
            commit = Commit(str(c["vendor"]), to_micros(c["amount_usd"]),
                            date.fromisoformat(c["from"]), date.fromisoformat(c["to"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            raise ValueError(f"commit {i}: needs vendor, amount_usd, from and to (YYYY-MM-DD): {e}") from None
        if commit.end < commit.start or commit.amount <= 0:
            raise ValueError(f"commit {i}: `to` is before `from`, or the amount is not above zero")
        out.append(commit)
    return out


def _at(day: date) -> str:
    return datetime(day.year, day.month, day.day, tzinfo=UTC).isoformat()


def _charged(ledger: Ledger, vendor: str, start: date, end: date) -> int:
    """What Quarter recorded with the vendor from `start` up to, not including, `end`."""
    (total,) = ledger.db.execute(
        "select coalesce(sum(amount), 0) from entries where kind = 'charge' and vendor = ? and at >= ? and at < ?",
        (vendor, _at(start), _at(end))).fetchone()
    return total


def _months(start: date, end: date):
    """(month, first day, day after its last) for each month from start's to end's."""
    day = start.replace(day=1)
    while day <= end:
        after = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield f"{day:%Y-%m}", day, after
        day = after


def standing(ledger: Ledger, commit: Commit, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    through = min(today, commit.end) + timedelta(days=1)  # spend counts up to and including this day
    spent, at_vendor = 0, []
    for month, first, after in _months(commit.start, min(today, commit.end)):
        lo, hi = max(first, commit.start), min(after, through)
        match = recorded(ledger, month).get(commit.vendor)
        if match and first >= commit.start and after <= commit.end + timedelta(days=1):
            spent += match["vendor_total"]
            at_vendor.append(month)
        else:
            spent += _charged(ledger, commit.vendor, lo, hi)
    pace = _charged(ledger, commit.vendor, max(through - timedelta(days=PACE_DAYS), commit.start), through)
    days = min(PACE_DAYS, (through - commit.start).days)
    per_day = pace / days if days > 0 else 0
    left = max((commit.end - today).days, 0)
    projected = spent + round(per_day * left)
    return {
        "vendor": commit.vendor, "amount": commit.amount, "from": commit.start.isoformat(),
        "to": commit.end.isoformat(), "spent": spent, "at_vendor_figures": at_vendor,
        "per_day": round(per_day), "days_left": left, "projected": projected,
        "shortfall": max(commit.amount - projected, 0), "overage": max(projected - commit.amount, 0),
    }
