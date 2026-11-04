"""The incident pack: the record of one agent's actions around a loss, for a claim.

A claim turns on records made before the loss. Each chain is linear, so a row is proven
only by every row after it up to a head hash someone else already holds. The pack
therefore carries each chain from the first row in the window to its current head, in
full, and marks the rows in scope. Anyone holding an earlier underwriting statement checks
that the chain passes through that statement's head: every row up to it existed, as
written, when the statement was made.

The rows outside the scope travel too, since the proof needs them. They are the same
ledger's rows: who sends the pack decides who sees them.
"""

import json
from datetime import UTC, datetime

from .store import CALL_COLUMNS, CHAINED, Ledger, chain_hash, dicts

SCHEMA = "quarter.incident/1"


def _in_scope(table: str, row: dict, agent: str | None, task: str | None) -> bool:
    if table == "entries":
        return (agent is None or row["agent"] == agent) and (task is None or row["task"] == task)
    if row["action"] == "paused":  # "<agent>: <reason>" or "task <task> of <agent>: <reason>"
        who = (row["detail"] or "").partition(": ")[0]
        paused_task, _, paused_agent = who[5:].partition(" of ") if who.startswith("task ") else (None, "", who)
        return (agent is None or paused_agent == agent) and (task is None or paused_task == task)
    try:
        detail = json.loads(row["detail"] or "")
    except ValueError:
        return False
    if not isinstance(detail, dict) or "agent" not in detail:
        return False
    return (agent is None or detail["agent"] == agent) and (task is None or detail.get("task") == task)


def pack(ledger: Ledger, start: str, end: str, agent: str | None = None, task: str | None = None) -> dict:
    db = ledger.db
    chains, scope = {}, {}
    for table, columns in CHAINED.items():
        first = db.execute(
            f"select min(id) from {table} where hash is not null and at >= ?", (start,)
        ).fetchone()[0]
        prev = db.execute(
            f"select hash from {table} where hash is not null and id < ? order by id desc limit 1",
            (first if first is not None else -1,),
        ).fetchone() if first is not None else None
        rows = dicts(db.execute(
            f"select id, {', '.join(columns)}, hash from {table}"
            " where hash is not null and id >= ? order by id", (first,),
        )) if first is not None else []
        chains[table] = {"columns": list(columns), "prev": prev[0] if prev else "",
                         "rows": [[r["id"], *(r[c] for c in columns), r["hash"]] for r in rows]}
        scope[table] = [r["id"] for r in rows if r["at"] < end and _in_scope(table, r, agent, task)]

    where, params = "at >= ? and at < ?", [start, end]
    for column, value in (("agent", agent), ("task", task)):
        if value is not None:
            where, params = f"{where} and {column} = ?", [*params, value]
    calls = dicts(db.execute(f"select {', '.join(CALL_COLUMNS)} from calls where {where} order by at", params))
    return {
        "schema": SCHEMA,
        "window": {"start": start, "end": end},
        "scope": {"agent": agent, "task": task},
        "made_at": datetime.now(UTC).isoformat(),
        "chains": chains,
        "in_scope": scope,
        "calls": calls,
    }


def heads_of(statement: dict) -> dict[str, set[str]]:
    """The head hashes an underwriting statement committed to, by table."""
    heads = statement.get("integrity", {}).get("heads", {})
    return {t: {h["hash"]} for t, h in heads.items() if isinstance(h, dict) and h.get("hash")}


def check(incident: dict, known_heads: dict[str, set[str]]) -> dict:
    """Recompute each chain in the pack. A row is anchored when a head someone already
    held comes at or after it; the rows in scope are counted both ways."""
    result = {}
    for table, chain in incident["chains"].items():
        prev, bad, anchored_through = chain["prev"], None, None
        heads = known_heads.get(table, set())
        for row_id, *values, stored in chain["rows"]:
            if chain_hash(prev, tuple(values)) != stored:
                bad = row_id
                break
            prev = stored
            if stored in heads:
                anchored_through = row_id
        scope = incident["in_scope"].get(table, [])
        anchored = [i for i in scope if anchored_through is not None and i <= anchored_through and bad is None]
        result[table] = {
            "rows": len(chain["rows"]),
            "intact": bad is None,
            "first_bad_id": bad,
            "anchored_through": anchored_through,
            "in_scope": len(scope),
            "in_scope_anchored": len(anchored),
        }
    return result
