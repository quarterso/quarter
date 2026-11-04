"""Trying rules without breaking anything: what shadow mode recorded, and a proposed rule set
replayed against calls already made.

shadow_report reads the would_hold audit rows: what would have been held, by rule, with the
money and the people who would have been asked, and how many of those asks a standing
approval (in force, or suggested by learn.py) would have answered.

replay runs caps and payee lists over the recorded calls of the last N days, in order, through
the gateway's own decision (Ledger.judge). Each call is decided against a scratch ledger in
memory holding only the entries made before it, so its windows are what they were then. The
real ledger is only read. Windows hold what was recorded: a call the proposed rules would
refuse still counts toward later windows.
"""

import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .learn import suggestions
from .payments import RAILS, payee_rule
from .policy import Cap
from .store import Ledger

LINE = "call over approval limit"
COPIED = ("call_id", "kind", "at", "agent", "task", "customer", "vendor", "model", "amount")


def shadow_report(ledger: Ledger, since: datetime) -> dict:
    rows = [
        json.loads(detail) | {"at": at}
        for at, detail in ledger.db.execute(
            "select at, detail from audit where action = 'would_hold' and at >= ? order by id",
            (since.isoformat(),),
        )
    ]
    rules: dict[str, dict] = {}
    for r in rows:
        for part in r["reason"].split("; "):
            rule = rules.setdefault(part, {"rule": part, "outcome": r["outcome"], "count": 0, "amount": 0, "asked": set()})
            rule["count"] += 1
            rule["amount"] += r.get("amount") or 0
            rule["asked"] |= set(r.get("asked") or [])
            if r["outcome"] == "block":
                rule["outcome"] = "block"
    by_rule = sorted(rules.values(), key=lambda g: (-g["amount"], -g["count"]))
    for g in by_rule:
        g["asked"] = sorted(g["asked"])

    # A standing approval skips only the approval line, so only asks made by it alone count.
    standing = [
        {"agent": s["agent"], "vendor": s["vendor"], "up_to": s["up_to"], "from": "in force", "id": s["id"]}
        for s in ledger.standing()
    ] + [
        {"agent": s["agent"], "vendor": s["vendor"], "up_to": s["up_to"], "from": "suggested", "id": None}
        for s in suggestions(ledger)["suggestions"] if s["kind"] == "standing"
    ]
    covered = defaultdict(lambda: {"count": 0, "amount": 0})
    for r in rows:
        if r["outcome"] != "approve" or any(not p.endswith(LINE) for p in r["reason"].split("; ")):
            continue
        fit = next((s for s in standing if s["agent"] == r.get("agent")
                    and s["vendor"].lower() == str(r.get("vendor")).lower() and s["up_to"] >= r["amount"]), None)
        if fit:
            key = (fit["agent"], fit["vendor"], fit["up_to"], fit["from"], fit["id"])
            covered[key]["count"] += 1
            covered[key]["amount"] += r["amount"]
    return {
        "since": since.isoformat(),
        "calls": len(rows),
        "amount": sum(r.get("amount") or 0 for r in rows),
        "by_rule": by_rule,
        "standing": [
            {"agent": a, "vendor": v, "up_to": u, "from": f, "id": i, **c}
            for (a, v, u, f, i), c in sorted(covered.items(), key=lambda kv: -kv[1]["amount"])
        ],
    }


def payee_reasons(payees: dict, vendor: str, model: str | None) -> tuple[str | None, str | None]:
    """The ask and deny reasons a payee list gives a payment, as the gateway words them."""
    if model not in RAILS or model == "invoice" or not vendor.startswith(f"{model}:"):
        return None, None
    payee = vendor[len(model) + 1:]
    rule = payee_rule(payees, model, payee)
    ask = f"{payee} is not on the {model} payee list" if rule == "ask" else None
    deny = f"{payee} is not allowed on the {model} payee list" if rule == "deny" else None
    return ask, deny


def _calls(ledger: Ledger, since: str) -> list[dict]:
    """Every call recorded since then that a rule decided: held entries, approvals that never
    became a call, and refusals. recorded is the outcome then: an entry a person approved
    was asked first; one shadow let through, what shadow said (None after a pause, which
    replay does not decide). Bills are left out: their invoice checks are not rules here."""
    asked = {r[0] for r in ledger.db.execute("select call_id from approvals where call_id is not null")}
    shadowed = {}
    for (detail,) in ledger.db.execute("select detail from audit where action = 'would_hold' and at >= ?", (since,)):
        d = json.loads(detail)
        if d.get("call_id"):
            paused = d["reason"].startswith(("agent paused", "task paused"))
            shadowed[d["call_id"]] = None if paused else d["outcome"]
    calls = []
    for entry_id, call_id, at, agent, task, customer, vendor, model, amount in ledger.db.execute(
        "select id, call_id, at, agent, task, customer, vendor, model, amount from entries"
        " where kind = 'hold' and at >= ? and coalesce(model, '') != 'invoice' order by id", (since,)
    ):
        recorded = shadowed.get(call_id, "allow") if call_id not in asked else "approve"
        calls.append({"entry": entry_id, "at": at, "agent": agent, "task": task, "customer": customer,
                      "vendor": vendor, "model": model, "amount": amount, "recorded": recorded})
    for at, agent, task, customer, vendor, model, amount in ledger.db.execute(
        "select created_at, agent, task, customer, vendor, model, estimate from approvals"
        " where call_id is null and estimate > 0 and coalesce(model, '') != 'invoice' and created_at >= ?", (since,)
    ):
        calls.append({"entry": None, "at": at, "agent": agent, "task": task, "customer": customer,
                      "vendor": vendor, "model": model, "amount": amount, "recorded": "approve"})
    for at, detail in ledger.db.execute("select at, detail from audit where action = 'refused' and at >= ?", (since,)):
        d = json.loads(detail)
        if d.get("model") != "invoice":
            calls.append({"entry": None, "at": at, **{k: d.get(k) for k in COPIED[3:]}, "recorded": "block"})
    return calls


def replay(
    ledger: Ledger,
    live: tuple[list[Cap], dict],
    proposed: tuple[list[Cap], dict],
    days: int = 30,
    now: datetime | None = None,
) -> dict:
    """Decide each recorded call of the last `days` under the live and the proposed (caps,
    payees). Shadow modes are ignored: replay asks what each rule set would hold."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat()
    calls = _calls(ledger, since)
    # Decided before the entry of the same call is copied; others once every entry made
    # before them is in.
    calls.sort(key=lambda c: (c["at"], c["entry"] is None))
    scratch = Ledger(":memory:")
    scratch.db.executemany(
        "insert into standing values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ledger.db.execute("select id, created_at, created_by, agent, vendor, up_to, monthly, ends,"
                          " revoked_at, revoked_by from standing").fetchall(),
    )
    rule_sets = {
        name: ([replace(c, mode="enforce") for c in caps], payees)
        for name, (caps, payees) in (("live", live), ("proposed", proposed))
    }
    entries = ledger.db.execute(f"select id, {', '.join(COPIED)} from entries order by id")
    pending = next(entries, None)
    out = []
    for call in calls:
        while pending is not None and (pending[0] < call["entry"] if call["entry"] else pending[3] <= call["at"]):
            scratch.db.execute(f"insert into entries ({', '.join(COPIED)}) values ({', '.join('?' * len(COPIED))})",
                               pending[1:])
            pending = next(entries, None)
        ids = {"agent": call["agent"], "task": call["task"], "customer": call["customer"]}
        at = datetime.fromisoformat(call["at"])
        for name, (caps, payees) in rule_sets.items():
            ask, deny = payee_reasons(payees, call["vendor"], call["model"])
            decision, _ = scratch.judge(caps, ids, call["vendor"], call["amount"], ask=ask, deny=deny, at=at)
            call[name], call[f"{name}_reason"] = decision.outcome, decision.reason
        out.append(call)

    def total(rows):
        return {"calls": len(rows), "amount": sum(r["amount"] for r in rows)}

    newly = [c for c in out if c["live"] == "allow" and c["proposed"] != "allow"]
    freed = [c for c in out if c["live"] != "allow" and c["proposed"] == "allow"]
    changed = [c for c in out if "allow" not in (c["live"], c["proposed"]) and c["live"] != c["proposed"]]
    by_rule = defaultdict(lambda: {"calls": 0, "amount": 0})
    for c in newly:
        for part in c["proposed_reason"].split("; "):
            by_rule[part]["calls"] += 1
            by_rule[part]["amount"] += c["amount"]
    comparable = [c for c in out if c["recorded"] is not None]
    return {
        "since": since,
        "days": days,
        "calls": out,
        "self_check": {"compared": len(comparable), "matched": sum(c["live"] == c["recorded"] for c in comparable)},
        "newly_held": total(newly) | {
            "block": sum(c["proposed"] == "block" for c in newly),
            "approve": sum(c["proposed"] == "approve" for c in newly),
        },
        "no_longer_held": total(freed),
        "changed": total(changed),
        "by_rule": sorted(({"rule": k, **v} for k, v in by_rule.items()), key=lambda r: -r["amount"]),
    }
