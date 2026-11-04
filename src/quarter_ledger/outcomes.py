"""Cost per outcome: what agents' work produced, set against what it cost.

An outcome (a ticket resolved, an invoice closed, a lead qualified) names the task that
produced it. A task's AI cost is every charge booked to that task, split evenly across
the outcomes it produced, so a task that resolved two tickets costs half as much per
ticket. Spend on tasks that produced no outcome is shown on its own: that is the work
that cost money and, as far as Quarter has been told, delivered nothing.
"""

import csv
from datetime import datetime
from io import StringIO

from .money import to_micros
from .store import Ledger, month_range

KIND_MAX = 80


def read_outcomes(text: str) -> list[dict]:
    """Outcomes from a CSV with kind and task columns, and optionally at, id, customer,
    agent and value_usd. Header names are matched without regard to case."""
    rows = csv.DictReader(StringIO(text.lstrip("﻿")))
    fields = {f.strip().lower(): f for f in rows.fieldnames or []}
    if "kind" not in fields or "task" not in fields:
        raise ValueError("need a kind column and a task column")
    out = []
    for line, row in enumerate(rows, start=2):
        get = {k: (row[f] or "").strip() for k, f in fields.items()}
        if not get["kind"]:
            raise ValueError(f"line {line}: no kind")
        try:
            value = to_micros(get["value_usd"]) if get.get("value_usd") else None
            at = datetime.fromisoformat(get["at"]).isoformat() if get.get("at") else None
        except (ArithmeticError, ValueError):
            raise ValueError(f"line {line}: a bad value_usd or at") from None
        out.append({"kind": get["kind"][:KIND_MAX], "task": get["task"] or None, "at": at,
                    "ref": get.get("id") or None, "customer": get.get("customer") or None,
                    "agent": get.get("agent") or None, "value": value})
    return out


def cost_per_outcome(ledger: Ledger, month: str) -> dict:
    """One row per kind of outcome recorded in the month (count, AI cost, cost per outcome,
    value reported and value per dollar of AI cost), most costly first, and the spend that
    month on tasks that produced no outcome at all."""
    start, end = month_range(month)
    db = ledger.db
    found = db.execute(
        "select kind, task, value from outcomes where at >= ? and at < ?", (start.isoformat(), end.isoformat())
    ).fetchall()
    tasks = {t for _, t, _ in found if t}
    cost, shares = {}, {}
    if tasks:
        marks = ",".join("?" * len(tasks))
        cost = dict(db.execute(f"select task, sum(amount) from entries where kind = 'charge' and task in ({marks})"
                               " group by task", tuple(tasks)).fetchall())
        shares = dict(db.execute(f"select task, count(*) from outcomes where task in ({marks}) group by task",
                                 tuple(tasks)).fetchall())
    kinds: dict[str, dict] = {}
    for kind, task, value in found:
        k = kinds.setdefault(kind, {"kind": kind, "count": 0, "cost": 0, "value": 0, "valued": 0, "unpriced": 0})
        k["count"] += 1
        if task and task in cost:
            k["cost"] += cost[task] // shares[task]
        else:
            k["unpriced"] += 1
        if value is not None:
            k["value"] += value
            k["valued"] += 1
    rows = sorted(kinds.values(), key=lambda k: -k["cost"])
    for k in rows:
        priced = k["count"] - k["unpriced"]
        k["per_outcome"] = k["cost"] // priced if priced else None
        k["value_per_dollar"] = k["value"] / k["cost"] if k["valued"] and k["cost"] else None
    idle = db.execute(
        "select count(distinct task), coalesce(sum(amount), 0) from entries where kind = 'charge'"
        " and task is not null and at >= ? and at < ? and task not in (select task from outcomes where task is not null)",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return {"month": month, "rows": rows, "idle_tasks": idle[0], "idle_cost": idle[1],
            "outcomes": len(found), "cost": sum(k["cost"] for k in rows)}
