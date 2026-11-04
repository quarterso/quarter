"""Evidence for capitalising agent spend on software the company builds for itself.

Under ASC 350-40, as amended by ASU 2025-06, some costs of building internal-use software
are recorded as an asset instead of an expense, once the project qualifies. Coding-agent
spend can count when it is directly attributable to that work. Whether a project qualifies,
and from when, is the company's judgement: it names its projects in projects.json. Quarter
supplies what it alone can see, which calls went into each project, and the entry that
moves that spend from expense to the asset.

projects.json:

    [{"project": "Checkout rebuild", "from": "2026-10-01", "to": "2026-12-31",
      "agents": ["claude-code"], "tasks": ["checkout-*"]}]

A call belongs to a project when its date is within from and to (to is optional), its agent
is listed (or agents is left out), and its task matches a pattern (or tasks is left out).
Each project needs agents or tasks. A call that matches two projects is counted in neither
and reported, so nothing is capitalised twice.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from fnmatch import fnmatchcase

from .export import Line
from .money import to_cents
from .store import Ledger, month_range


@dataclass
class Project:
    name: str
    start: datetime
    end: datetime | None  # exclusive
    agents: list[str] | None
    tasks: list[str] | None

    def covers(self, at: datetime, agent: str | None, task: str | None) -> bool:
        return (
            self.start <= at
            and (self.end is None or at < self.end)
            and (self.agents is None or agent in self.agents)
            and (self.tasks is None or any(fnmatchcase(task or "", p) for p in self.tasks))
        )


def _day(text: str, field: str, name: str) -> datetime:
    try:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: {field} must be a date, YYYY-MM-DD") from None


def read_projects(text: str) -> list[Project]:
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("projects.json must be a list of projects")  # noqa: TRY004, a bad file, not a bad call
    out, seen = [], set()
    for p in raw:
        if not isinstance(p, dict) or not str(p.get("project", "")).strip():
            raise ValueError("each project needs a name: {\"project\": ...}")
        name = str(p["project"]).strip()
        if name.lower() in seen:
            raise ValueError(f"{name}: named twice")
        seen.add(name.lower())
        agents, tasks = p.get("agents"), p.get("tasks")
        for field, value in (("agents", agents), ("tasks", tasks)):
            if value is not None and (
                not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value)
            ):
                raise ValueError(f"{name}: {field} must be a list of names")
        if agents is None and tasks is None:
            raise ValueError(f"{name}: needs agents or tasks, or it would take every call")
        start = _day(p.get("from"), "from", name)
        end = _day(p["to"], "to", name) + timedelta(days=1) if p.get("to") else None
        if end is not None and end <= start:
            raise ValueError(f"{name}: to is before from")
        out.append(Project(name, start, end, agents, tasks))
    return out


def capitalised(ledger: Ledger, month: str, projects: list[Project]) -> dict:
    """The month's agent spend attributable to each project, the tasks behind it, and the
    calls that matched more than one project."""
    start, end = month_range(month)
    rows = ledger.db.execute(
        "select at, agent, task, customer, vendor, model, amount, call_id from entries"
        " where kind = 'charge' and amount > 0 and at >= ? and at < ? order by at",
        (start.isoformat(), end.isoformat()),
    )  # read row by row: a busy month is millions of charges
    totals = {p.name: 0 for p in projects}
    tasks: dict[tuple, dict] = {}
    by_account: dict[tuple[str, bool], int] = {}
    conflicts = []
    for at_text, agent, task, customer, vendor, model, amount, call_id in rows:
        at = datetime.fromisoformat(at_text)
        hits = [p.name for p in projects if p.covers(at, agent, task)]
        if len(hits) > 1:
            conflicts.append({"call_id": call_id, "projects": hits, "amount": amount})
            continue
        if not hits:
            continue
        name = hits[0]
        totals[name] += amount
        key = (name, task or "", agent or "", vendor, model or "")
        t = tasks.setdefault(key, {"calls": 0, "amount": 0, "first": at_text, "last": at_text})
        t["calls"] += 1
        t["amount"] += amount
        t["last"] = at_text
        by_account[(name, bool(customer))] = by_account.get((name, bool(customer)), 0) + amount
    return {
        "month": month,
        "projects": [{"project": n, "amount": a} for n, a in totals.items()],
        "total": sum(totals.values()),
        "tasks": [
            {"project": k[0], "task": k[1], "agent": k[2], "vendor": k[3], "model": k[4], **v}
            for k, v in sorted(tasks.items())
        ],
        "by_account": by_account,
        "conflicts": conflicts,
    }


def reclass(
    result: dict,
    asset_account: str = "Capitalised software",
    expense_account: str = "AI vendor costs",
    revenue_cost_account: str = "Cost of revenue: AI",
) -> list[Line]:
    """Journal lines that move the attributable spend from the accounts the month-end journal
    booked it to (the same defaults as `export`) into the asset, one pair per project."""
    out = []
    for (name, served_customer), amount in sorted(result["by_account"].items()):
        cents = to_cents(amount)
        if cents:
            credit = revenue_cost_account if served_customer else expense_account
            out.append(Line(asset_account, cents, 0, f"Agent spend capitalised: {name}", ""))
            out.append(Line(credit, 0, cents, f"Agent spend capitalised: {name}", ""))
    return out
