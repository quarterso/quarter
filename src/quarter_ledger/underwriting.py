"""The underwriting statement: one period's agent risk, in a form an insurer can rate on.

Counts only. No prompt, command, person or customer name leaves in it. Its fields follow
the telemetry the insurers' own blueprint asks for: exposure, authority, high-impact
actions, approval and containment events, and proof the record was not rewritten.
"""

from datetime import UTC, datetime

from . import controls, receipts
from .store import Ledger, month_range

SCHEMA = "quarter.underwriting/1"
READS = ("GET", "HEAD", "OPTIONS")


def statement(ledger: Ledger, month: str, caps: list, rules: list[str],
              retention_days: int | None = None) -> dict:
    start, end = month_range(month)
    span = (start.isoformat(), end.isoformat())
    db = ledger.db
    chain = ledger.verify_chain()
    facts = controls.gather(ledger, span, chain, None, retention_days, rules)

    models = [
        {"vendor": v, "model": m, "route": r, "calls": n, "input": i, "output": o,
         "cache_read": cr, "cache_write": cw, "cost_micros": c, "list_cost_micros": lc}
        for v, m, r, n, i, o, cr, cw, c, lc in db.execute(
            "select vendor, model, route, count(*), sum(input), sum(output), sum(cache_read),"
            " sum(cache_write), sum(cost), sum(list_cost) from calls where at >= ? and at < ?"
            " group by 1, 2, 3 order by 9 desc", span,
        )
    ]
    agents = {a for (a,) in db.execute(
        "select agent from calls where at >= ? and at < ? and agent is not null"
        " union select agent from entries where at >= ? and at < ? and agent is not null"
        " union select json_extract(detail, '$.agent') from audit where action = 'upstream_call'"
        " and at >= ? and at < ? and json_extract(detail, '$.agent') is not null", span * 3,
    )}
    owned = {a for (a,) in db.execute("select agent from owners")} & agents
    actions = [
        {"upstream": u, "method": m, "decision": d, "count": n}
        for u, m, d, n in db.execute(
            "select json_extract(detail, '$.upstream'), json_extract(detail, '$.method'),"
            " json_extract(detail, '$.decision'), count(*) from audit"
            " where action = 'upstream_call' and at >= ? and at < ? group by 1, 2, 3 order by 1, 2, 3",
            span,
        )
    ]
    tasks = db.execute(
        "select count(*) from (select task from calls where at >= ? and at < ? and task is not null"
        " union select task from entries where at >= ? and at < ? and task is not null)", span * 2,
    ).fetchone()[0]

    return {
        "schema": SCHEMA,
        "period": {"month": month, "start": span[0], "end": span[1]},
        "made_at": datetime.now(UTC).isoformat(),
        "exposure": {
            "calls": sum(m["calls"] for m in models),
            "agents": len(agents),
            "tasks": tasks,
            "cost_micros": sum(m["cost_micros"] for m in models),
            "list_cost_micros": sum(m["list_cost_micros"] for m in models),
            "by_model": models,
        },
        "authority": {
            "caps": [
                {"scope": c.scope, "every": c.key == "*", "window": c.window,
                 "limit_micros": c.limit, "approve_above_micros": c.approve_above}
                for c in caps
            ],
            "rules": rules,
            "agents_with_owner": len(owned),
        },
        "high_impact": {
            "upstream_calls": actions,
            "writes": sum(a["count"] for a in actions if a["method"] not in READS),
            "tool_calls_held": facts["egress"]["tool_calls_held"],
        },
        "oversight": {
            key: facts["approvals"][key]
            for key in ("count", "by_status", "median_seconds_to_decision")
        } | {"self_approval_refused": facts["access"]["self_approval_refused"]},
        "containment": facts["monitoring"],
        "integrity": {
            "chain": chain,
            "heads": {
                t: dict(zip(("id", "hash"), db.execute(
                    f"select id, hash from {t} where hash is not null order by id desc limit 1"
                ).fetchone() or (None, None), strict=True))
                for t in ("entries", "audit")
            },
            "retention_days": retention_days,
        },
    }


def signed(statement: dict, key: str) -> str:
    """The statement as a signed token. With an ed25519 key, anyone holding the public
    key checks it offline: `quarter verify <token>`."""
    return receipts.sign_claims({"iss": "quarter-ledger", "sub": SCHEMA, **statement}, key)
