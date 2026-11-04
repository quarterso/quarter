"""What past approvals teach: decisions people keep making the same way.

Calls an agent sends to one vendor or payee that people approved every time become a
suggested standing approval, sized to what was approved. Ones people denied every time
are pointed out, so a rule can stop them before anyone is asked. Nothing changes until a
person accepts a suggestion; each customer's own decisions are the only data used.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median

from .store import Ledger, dicts

MIN_APPROVED = 5
MIN_DENIED = 3
DOLLAR = 1_000_000


def _up(micros: int) -> int:
    """Up to the next whole dollar."""
    return -(-micros // DOLLAR) * DOLLAR


def suggestions(ledger: Ledger, days: int = 90, min_approved: int = MIN_APPROVED) -> dict:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = dicts(
        ledger.db.execute(
            "select agent, vendor, estimate, status, created_at, decided_at, decided_by from approvals"
            " where status in ('approved', 'used', 'denied') and decided_at >= ? and estimate > 0",
            (since,),
        )
    )
    groups = defaultdict(list)
    for r in rows:
        groups[(r["agent"], r["vendor"].lower())].append(r)
    standing = ledger.standing()

    out = []
    for (agent, _), decided in sorted(groups.items()):
        vendor = decided[-1]["vendor"]
        yes = [r for r in decided if r["status"] != "denied"]
        no = len(decided) - len(yes)
        people = sorted({r["decided_by"] for r in decided if r["decided_by"]})
        if len(yes) >= min_approved and no == 0:
            top = max(r["estimate"] for r in yes)
            covered = any(
                s["agent"] == agent and s["vendor"].lower() == vendor.lower() and s["up_to"] >= top
                for s in standing
            )
            if covered:
                continue
            months = defaultdict(int)
            for r in yes:
                months[r["created_at"][:7]] += r["estimate"]
            # A standing approval's monthly limit counts all this agent's spend to the vendor,
            # not only the calls people approved, so size it to that too.
            for month, spent in ledger.db.execute(
                "select substr(at, 1, 7), sum(amount) from entries where kind = 'charge'"
                " and agent = ? and lower(vendor) = lower(?) and at >= ? group by 1",
                (agent, vendor, since),
            ):
                months[month] = max(months[month], spent)
            up_to, monthly = _up(top), _up(max(months.values()))
            out.append(
                {
                    "kind": "standing",
                    "agent": agent,
                    "vendor": vendor,
                    "approved": len(yes),
                    "largest": top,
                    "up_to": up_to,
                    "monthly": max(monthly, up_to),
                    "approvers": people,
                    "text": f"{', '.join(people) or 'people'} approved all {len(yes)} requests from {agent}"
                    f" to {vendor} in the last {days} days",
                }
            )
        elif no >= MIN_DENIED and not yes:
            out.append(
                {
                    "kind": "always_denied",
                    "agent": agent,
                    "vendor": vendor,
                    "denied": no,
                    "approvers": people,
                    "text": f"{', '.join(people) or 'people'} denied all {no} requests from {agent} to {vendor}"
                    f" in the last {days} days; a rule or payee list could refuse them before anyone is asked",
                }
            )
    out.sort(key=lambda s: -(s.get("approved") or s.get("denied")))

    waits = [
        (
            datetime.fromisoformat(r["decided_at"]) - datetime.fromisoformat(r["created_at"])
        ).total_seconds()
        / 60
        for r in rows
    ]
    asked_less = sum(s["approved"] for s in out if s["kind"] == "standing")
    return {
        "days": days,
        "decisions": len(rows),
        "median_wait_minutes": round(median(waits), 1) if waits else None,
        "would_not_ask": asked_less,
        "suggestions": out,
    }
