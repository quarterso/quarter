"""The agent register and the quarterly owner review.

Every agent the ledger knows, with its owner, the rules that apply, and when it last called
and paid. Each quarter the owner, or an approver, confirms the agent is still needed and
rightly owned (keep), or retires it. An agent's review for a quarter is due grace_days (14)
into the next one.

With QUARTER_REGISTER set, an agent with no owner, unused for dormant_days (30), or past its
review is sent to a person on its next call or payment, never refused (see Ledger.hold).
Approving that call is not a review: the next call asks again until the register is put right.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .money import usd_display
from .payments import RAILS
from .policy import cap_label

QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
RAIL_LIST = ", ".join(f"'{r}'" for r in sorted(RAILS))


@dataclass(frozen=True)
class RegisterRule:
    dormant_days: int = 30
    grace_days: int = 14
    shadow: bool = False


def load_register(raw: str | None) -> RegisterRule | None:
    """QUARTER_REGISTER: unset or "off" for no routing, "on", "shadow", or JSON
    {"dormant_days", "grace_days", "mode"}."""
    text = (raw or "").strip().lower()
    if text in ("", "off"):
        return None
    if text in ("on", "enforce", "shadow"):
        return RegisterRule(shadow=text == "shadow")
    cfg = json.loads(raw)
    if cfg.get("mode", "enforce") not in ("enforce", "shadow"):
        raise ValueError(f'QUARTER_REGISTER "mode" is "enforce" or "shadow", not {cfg["mode"]!r}')
    base = RegisterRule()
    rule = RegisterRule(int(cfg.get("dormant_days", base.dormant_days)),
                        int(cfg.get("grace_days", base.grace_days)), cfg.get("mode") == "shadow")
    if rule.dormant_days < 1 or not 0 <= rule.grace_days <= 90:
        raise ValueError("QUARTER_REGISTER needs dormant_days of 1 or more and grace_days of 0 to 90")
    return rule


def quarter_of(at: datetime) -> str:
    return f"{at.year}-Q{(at.month - 1) // 3 + 1}"


def quarter_span(quarter: str) -> tuple[datetime, datetime]:
    year, n = (int(x) for x in QUARTER.match(quarter).groups())
    start = datetime(year, 3 * n - 2, 1, tzinfo=UTC)
    return start, datetime(year + n // 4, 3 * n % 12 + 1, 1, tzinfo=UTC)


def previous(quarter: str) -> str:
    return quarter_of(quarter_span(quarter)[0] - timedelta(days=1))


def due(first: str | None, reviewed: str | None, now: datetime) -> str | None:
    """The last quarter, when this agent was already there in it and was not kept in a
    review for it or later. reviewed: the latest quarter it was kept in."""
    start, last = quarter_span(quarter_of(now))[0], previous(quarter_of(now))
    if first is None or first >= start.isoformat():
        return None
    return last if reviewed is None or reviewed < last else None


def reasons(owner, first, last, reviewed, now: datetime, rule: RegisterRule) -> list[tuple[str, str]]:
    """(status, reason) for each thing that sends this agent's next call to a person: no
    owner, unused for dormant_days, or its review still missing grace_days into the quarter."""
    out = [] if owner else [("no owner", "agent has no owner")]
    if last and last < (now - timedelta(days=rule.dormant_days)).isoformat():
        out.append(("dormant", f"agent unused for {rule.dormant_days} days"))
    late = due(first, reviewed, now)
    if late and now >= quarter_span(quarter_of(now))[0] + timedelta(days=rule.grace_days):
        out.append((f"review overdue ({late})", f"quarterly review overdue ({late})"))
    return out


# One indexed lookup per table: the owner, the agent's first and last gateway entry, its latest
# keep. Observed spend (connect.py) does not make an agent look used to the gateway.
_SEEN = "from entries where agent = :a and substr(call_id, 1, 4) != 'obs:'"
OBS = "(substr(call_id, 1, 4) = 'obs:')"
STATE = (
    "select (select owner from owners where agent = :a),"
    f" (select min(at) {_SEEN}), (select max(at) {_SEEN}),"
    " (select max(quarter) from reviews where agent = :a and result = 'keep')"
)


def state(ledger, agent: str) -> tuple:
    return ledger.db.execute(STATE, {"a": agent}).fetchone()


def review(ledger, quarter: str, agent: str, by: str, keep: bool, approvers, note: str = "") -> dict:
    """Record one agent's review. The owner may confirm their own agent: that is the point of
    an owner review. An approver may review any agent. The agent itself never may. Keeping an
    agent needs an owner. Retiring pauses it, so its key is refused until someone resumes it."""
    by, now = by.strip(), datetime.now(UTC)
    if not QUARTER.match(quarter) or quarter > quarter_of(now):
        raise ValueError(f"the quarter is YYYY-Qn and not after {quarter_of(now)}, not {quarter!r}")
    owner = state(ledger, agent)[0]
    allowed = {a.lower() for a in approvers} | ({owner.lower()} if owner else set())
    if not by or by.lower() == agent.lower() or by.lower() not in allowed:
        ledger.audit("review_refused", None, f"{agent} {quarter} {by}")
        raise PermissionError(f"{by or 'nobody'} may not review {agent}: the owner or an approver must")
    if keep and not owner:
        raise ValueError(f"{agent} has no owner: name one with `quarter-ledger owner` first")
    result = "keep" if keep else "retire"
    with ledger._write():
        ledger.db.execute(
            "insert into reviews (at, quarter, agent, owner, reviewer, result, note)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(), quarter, agent, owner, by, result, note),
        )
    detail = {"quarter": quarter, "agent": agent, "owner": owner, "result": result, "note": note}
    ledger.audit("agent_review", by, json.dumps(detail, sort_keys=True))
    if not keep and ledger.pause(agent, f"retired in the {quarter} review by {by}"):
        ledger.audit("paused", by, f"{agent}: retired in the {quarter} review")
    return detail | {"reviewer": by, "at": now.isoformat()}


def listing(ledger, keys=(), caps=(), payees=None, rule: RegisterRule | None = None,
            now: datetime | None = None) -> list[dict]:
    """Every agent the ledger knows: named keys, and any agent seen in entries, owners or
    reviews. keys: the agent names QUARTER_AGENT_KEYS gives. Spend booked by connect counts
    as use (last_observed), but not as a key's use: it came from a vendor's bill."""
    rule, now = rule or RegisterRule(), now or datetime.now(UTC)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    seen = {
        r[0]: r[1:] for r in ledger.db.execute(
            "select agent, min(at),"
            f" max(case when {OBS} = 0 and model not in ({RAIL_LIST}) then at end),"
            f" max(case when {OBS} = 0 and model in ({RAIL_LIST}) then at end),"
            f" max(case when {OBS} then at end),"
            " coalesce(sum(case when kind = 'charge' and at >= ? then amount end), 0)"
            " from entries where agent is not null group by agent", (month,),
        )
    }
    owners = dict(ledger.db.execute("select agent, owner from owners"))
    last_review = {r[0]: r for r in ledger.db.execute(
        "select agent, quarter, at, reviewer, result from reviews r where id ="
        " (select max(id) from reviews where agent = r.agent)"
    )}
    kept = dict(ledger.db.execute(
        "select agent, max(quarter) from reviews where result = 'keep' group by agent"))
    paused = {p["agent"] for p in ledger.paused_agents() if not p["task"]}
    standing = {}
    for s in ledger.standing():
        standing.setdefault(s["agent"], []).append(f"{s['vendor']} up to ${usd_display(s['up_to'])}")
    payees = payees or {}
    lists = [f"{rail}: {len(v)} listed, others {payees.get('unlisted', 'approve')}"
             for rail, v in sorted(payees.items()) if isinstance(v, list)]
    keys = set(keys)
    out = []
    for agent in sorted(keys | seen.keys() | owners.keys() | last_review.keys()):
        first, last_call, last_pay, last_seen, mtd = seen.get(agent, (None, None, None, None, 0))
        last = max(filter(None, (last_call, last_pay)), default=None)
        _, quarter, at, reviewer, result = last_review.get(agent, (None,) * 5)
        status = [s for s, _ in reasons(owners.get(agent), first, last, kept.get(agent), now, rule)]
        waiting = due(first, kept.get(agent), now)
        if waiting and not any(s.startswith("review overdue") for s in status):
            status.append(f"review due ({waiting})")
        if result == "retire" and agent in paused:
            status.insert(0, "retired")
        if not (last or last_seen):
            status.append("never used")
        if agent not in keys and (last or not last_seen):
            status.append("seen without a key")
        applies = [c for c in caps if c.scope == "all" or (c.scope == "agent" and c.key in ("*", agent))]
        out.append({
            "agent": agent, "owner": owners.get(agent),
            "caps": [f"{cap_label(c, agent if c.key == '*' else c.key)} {c.window} ${usd_display(c.limit)}"
                     for c in applies],
            "payees": lists + standing.get(agent, []),
            "first_seen": first, "last_call": last_call, "last_payment": last_pay,
            "last_observed": last_seen, "month_to_date": mtd,
            "review_quarter": quarter, "reviewed_at": at, "reviewer": reviewer, "review_result": result,
            "status": status or ["ok"],
        })
    return out


def evidence(ledger, month_start: datetime, rule: RegisterRule | None = None,
             now: datetime | None = None) -> dict:
    """The owner reviews for an audit pack: the month's quarter and the one before (whose
    reviews fall due early in this one), who reviewed what, the agents seen in each quarter
    and not reviewed, and the agents overdue as the pack is made."""
    rule, now = rule or RegisterRule(), now or datetime.now(UTC)
    quarters = {}
    for quarter in (previous(quarter_of(month_start)), quarter_of(month_start)):
        start, end = quarter_span(quarter)
        rows = [dict(zip(("agent", "owner", "reviewer", "result", "at", "note"), r, strict=True))
                for r in ledger.db.execute(
                    "select agent, owner, reviewer, result, at, note from reviews where quarter = ?"
                    " order by id", (quarter,))]
        active = {a for (a,) in ledger.db.execute(
            "select distinct agent from entries where agent is not null and at >= ? and at < ?",
            (start.isoformat(), end.isoformat()))}
        quarters[quarter] = {"reviews": rows,
                             "not_reviewed": sorted(active - {r["agent"] for r in rows})}
    agents = listing(ledger, rule=rule, now=now)
    return {
        "grace_days": rule.grace_days,
        "quarters": quarters,
        "overdue_now": [a["agent"] for a in agents if any(s.startswith("review overdue") for s in a["status"])],
        "no_owner_now": [a["agent"] for a in agents if a["owner"] is None],
    }
