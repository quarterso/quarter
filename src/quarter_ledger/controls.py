"""The controls map in the audit pack: each SOC 2 criterion, EU AI Act article and AIUC-1
control an auditor may test, next to the evidence Quarter holds for the month, with counts and the
pack files that hold it. Quarter provides evidence; it does not certify compliance.

EU AI Act article text was checked against Regulation (EU) 2024/1689, OJ L, 12.7.2024.
SOC 2 criterion names are short paraphrases of the 2017 Trust Services Criteria, not
checked against the AICPA text. AIUC-1 control ids and titles were checked against
standard.aiuc-1.com (Q4 2026 update) on 2026-11-02.
"""

import json
import statistics
from collections import Counter
from datetime import datetime

from .store import Ledger

# Articles 19(1) and 26(6) say logs are kept for "at least six months". Six calendar
# months can run to 184 days (March to August), so anything shorter can fall short.
SIX_MONTHS_DAYS = 184

ACCESS_REFUSALS = ("role_refused", "login_failed", "slack_refused", "approver_mismatch")
# How proxy.py words its loop pauses: the same request, or the same tool call, over and over
LOOP_REASONS = ("sent the same request", "made the same tool call")


def gather(
    ledger: Ledger,
    span: tuple[str, str],
    chain: dict,
    people: dict[str, str] | None,
    retention_days: int | None,
    rules: list[str] | None = None,
    reviews: dict | None = None,
) -> dict:
    """Counts for the period [start, end) from the ledger's own tables. rules: each egress
    rule in force, as a line. reviews: the owner reviews (register.evidence)."""
    db = ledger.db
    actions = Counter(
        dict(db.execute(
            "select action, count(*) from audit where at >= ? and at < ? group by action", span
        ).fetchall())
    )
    changed = [
        detail for (detail,) in db.execute(
            "select detail from audit where action = 'config.changed' and at >= ? and at < ?"
            " order by id", span,
        )
    ]
    pause_reasons = [r for (r,) in db.execute(
        "select reason from pauses where at >= ? and at < ?", span
    )]
    approvals = db.execute(
        "select status, decided_by, created_at, decided_at from approvals"
        " where created_at >= ? and created_at < ?", span,
    ).fetchall()
    waits = [
        (datetime.fromisoformat(decided) - datetime.fromisoformat(created)).total_seconds()
        for _, _, created, decided in approvals if decided
    ]
    loops = sum(r.startswith(LOOP_REASONS) for r in pause_reasons)
    decisions = {
        action: dict(db.execute(
            "select json_extract(detail, '$.decision'), count(*) from audit"
            " where action = ? and at >= ? and at < ? group by 1", (action, *span),
        ).fetchall())
        for action in ("upstream_call", "tool_held")
    }
    kinds: Counter = Counter()
    for (redacted,) in db.execute(
        "select redacted from calls where redacted is not null and at >= ? and at < ?", span
    ):
        for part in redacted.split(","):
            kind, _, n = part.partition(":")
            kinds[kind] += int(n or 0)
    places = dict(db.execute(
        "select coalesce(upstream, vendor), count(*) from calls where at >= ? and at < ? group by 1", span
    ).fetchall())
    feeds = [
        json.loads(detail).get("version") for (detail,) in db.execute(
            "select detail from audit where action = 'feed_applied' and at < ? order by id", span[1:]
        )
    ]
    return {
        "data": {"masked_values": dict(kinds), "calls_by_destination": places,
                 "feed_versions": feeds},
        "records": {
            "entries": db.execute(
                "select count(*) from entries where at >= ? and at < ?", span
            ).fetchone()[0],
            "audit_rows": sum(actions.values()),
        },
        "access": {
            "people": people,
            "roles": dict(Counter(people.values())) if people is not None else None,
            "refusals": {a: actions[a] for a in ACCESS_REFUSALS},
            "self_approval_refused": actions["self_approval_refused"],
        },
        "monitoring": {
            "pauses": len(pause_reasons),
            "loop_pauses": loops,
            "runaway_pauses": len(pause_reasons) - loops,
            "budget_alerts": actions["budget_alert"],
        },
        "change": {
            "config_changes": len(changed),
            "sections_changed": sorted({s for d in changed for s in d.split(", ") if s}),
            "owner_changes": actions["owner"],
        },
        "approvals": {
            "count": len(approvals),
            "by_status": dict(Counter(status for status, *_ in approvals)),
            "decided_by": dict(Counter(by for _, by, _, _ in approvals if by)),
            "median_seconds_to_decision": statistics.median(waits) if waits else None,
        },
        "integrity": chain,
        "retention_days": retention_days,
        "egress": {
            "rules": rules or [],
            "upstream_calls": decisions["upstream_call"],
            "tool_calls_held": decisions["tool_held"],
        },
        "reviews": reviews and reviews | {"refused": actions["review_refused"]},
    }


def warnings(facts: dict) -> list[str]:
    out = []
    for table, result in facts["integrity"].items():
        if not result["intact"]:
            out.append(
                f"The {table} hash chain is BROKEN at row {result['first_bad_id']}. Rows from"
                " there on cannot be shown to be unedited."
            )
    days = facts["retention_days"]
    if days is None:
        out.append(
            "No retention period is set (QUARTER_RETENTION_DAYS). The EU AI Act asks for logs"
            " to be kept at least six months (Articles 19(1) and 26(6))."
        )
    elif days < SIX_MONTHS_DAYS:
        out.append(
            f"Retention is {days} days, under six months ({SIX_MONTHS_DAYS} days covers any"
            " six calendar months). Articles 19(1) and 26(6) ask for at least six months."
        )
    if facts["access"]["people"] is None:
        out.append(
            "QUARTER_PEOPLE was not given to this pack, so the people and roles in force are"
            " not listed. The audit log records each change to them as a hash."
        )
    return out


def controls(facts: dict) -> list[dict]:
    """Each control with what Quarter holds for it. `evidence` lines are plain sentences."""
    acc, mon, chg, apr = facts["access"], facts["monitoring"], facts["change"], facts["approvals"]
    entries, audit_rows = facts["records"]["entries"], facts["records"]["audit_rows"]
    intact = all(r["intact"] for r in facts["integrity"].values())
    days = facts["retention_days"]
    egress = facts["egress"]

    people = (
        "not supplied to this pack" if acc["people"] is None
        else f"People in force: {len(acc['people'])}, by role: {_counts(acc['roles'])}"
    )
    refusals = _counts(acc["refusals"])
    median = apr["median_seconds_to_decision"]
    wait = "no decisions" if median is None else f"median time to decision {median / 60:.1f} minutes"
    approvals = (
        f"Approvals requested: {apr['count']} ({_counts(apr['by_status'])}); decided by:"
        f" {_counts(apr['decided_by'])}; {wait}"
    )
    self_refused = f"Owner self-approvals refused: {acc['self_approval_refused']}"
    monitoring = (
        f"Agent pauses: {mon['pauses']} (runaway spend {mon['runaway_pauses']},"
        f" loops {mon['loop_pauses']}); budget alerts {mon['budget_alerts']}"
    )
    sections = f" (sections: {', '.join(chg['sections_changed'])})" if chg["sections_changed"] else ""
    changes = (
        f"Config changes: {chg['config_changes']}{sections};"
        f" agent owner changes: {chg['owner_changes']}"
    )
    chain = "both hash chains recompute" if intact else "a hash chain is BROKEN, see warnings"
    records = f"Ledger entries: {entries}; audit log rows: {audit_rows}; each hash-chained; {chain}"
    retention = "not set" if days is None else f"{days} days"
    rules = [f"Egress rules in force: {len(egress['rules'])}", *egress["rules"]]
    reviews = _reviews(facts.get("reviews"))
    calls = [
        f"Upstream calls by decision: {_counts(egress['upstream_calls'])}",
        f"Tool calls held in model replies: {_counts(egress['tool_calls_held'])}",
    ]

    data = facts["data"]
    masked = f"Values masked before leaving for a model, by kind: {_counts(data['masked_values'])}"
    places = f"Model calls by where they were sent: {_counts(data['calls_by_destination'])}"
    feed = data["feed_versions"]
    feed = f"Quarter feed in force: version {feed[-1]}" if feed else "No Quarter feed in force"

    return [
        _c("AIUC-1", "A006", "Prevent PII leakage", [masked], ["controls.json"]),
        _c("AIUC-1", "B006", "Prevent unauthorized AI agent actions", rules + calls,
           ["rules.json", "audit_log.csv", "approvals.csv"]),
        _c("AIUC-1", "B007", "Enforce user access privileges to AI systems",
           [people, f"Access refusals: {refusals}", self_refused], ["audit_log.csv"]),
        _c("AIUC-1", "C007", "Flag high risk outputs for human review",
           [calls[1], approvals], ["approvals.csv", "audit_log.csv"]),
        _c("AIUC-1", "C009", "Enable real-time feedback and intervention",
           [monitoring, approvals], ["pauses.csv", "approvals.csv"]),
        _c("AIUC-1", "D003", "Restrict unsafe tool calls", [calls[1], feed, *rules],
           ["rules.json", "audit_log.csv", "approvals.csv"]),
        _c("AIUC-1", "E004", "Assign accountability", [changes, approvals, *reviews[:2]],
           ["audit_log.csv", "approvals.csv", "controls.json"]),
        _c("AIUC-1", "E009", "Monitor third-party access", [calls[0], places], ["audit_log.csv"]),
        _c("AIUC-1", "E011", "Record processing locations", [places], ["controls.json"]),
        _c("AIUC-1", "E015", "Log AI system activity", [records],
           ["entries.csv", "audit_log.csv", "chain.json"]),
        _c("SOC 2", "CC6.1", "Logical access is restricted to authorised people",
           [people, f"Access refusals: {refusals}"], ["audit_log.csv"]),
        _c("SOC 2", "CC6.2", "Access is granted and changed through a recorded process",
           [people, changes, *reviews[:2]], ["audit_log.csv", "controls.json"]),
        _c("SOC 2", "CC6.3", "Access follows role and segregation of duties",
           [f"Role refusals: {acc['refusals']['role_refused']}", self_refused, *reviews[2:]],
           ["audit_log.csv", "approvals.csv", "controls.json"]),
        _c("SOC 2", "CC7.2", "Operations are monitored for anomalies",
           [monitoring], ["pauses.csv", "audit_log.csv"]),
        _c("SOC 2", "CC8.1", "Changes are authorised and recorded",
           [changes], ["audit_log.csv", "caps.json", "approval_record/approvals.csv"]),
        _c("SOC 2", "Approvals", "Spend over the approval line has a human decision",
           [approvals, self_refused], ["approvals.csv", "receipts.jsonl"]),
        _c("SOC 2", "Egress", "Agents act on outside systems only as written rules allow",
           rules + calls, ["rules.json", "audit_log.csv", "approvals.csv"]),
        _c("SOC 2", "Integrity", "Records are complete and unaltered",
           [records], ["chain.json", "entries.csv", "audit_log.csv"]),
        _c("EU AI Act", "Art. 12(1)", "Automatic recording of events (logs) over the lifetime",
           [records], ["entries.csv", "audit_log.csv", "chain.json"]),
        _c("EU AI Act", "Art. 19(1)", "Provider keeps logs at least six months",
           [f"Retention: {retention}. Art. 19 binds providers; Art. 26(6) binds deployers."],
           ["chain.json"]),
        _c("EU AI Act", "Art. 26(2)", "Deployer assigns human oversight to competent people",
           [people, approvals], ["approvals.csv", "audit_log.csv"]),
        _c("EU AI Act", "Art. 26(5)", "Deployer monitors operation",
           [monitoring], ["pauses.csv", "audit_log.csv"]),
        _c("EU AI Act", "Art. 26(6)", "Deployer keeps logs at least six months",
           [f"Retention: {retention}"], ["chain.json"]),
    ]


def _reviews(found: dict | None) -> list[str]:
    """The quarterly owner reviews as evidence lines: results per quarter, who is past their
    review now, then who reviewed. Nothing when the pack was made without them."""
    if not found:
        return []
    per = "; ".join(
        f"{q}: {_counts(dict(Counter(r['result'] for r in v['reviews'])))},"
        f" not reviewed {len(v['not_reviewed'])}" for q, v in found["quarters"].items()
    )
    late, ownerless = len(found["overdue_now"]), len(found["no_owner_now"])
    by = Counter(r["reviewer"] for v in found["quarters"].values() for r in v["reviews"])
    return [
        f"Quarterly owner reviews of agents (kept or retired, and agents active but not reviewed): {per}",
        f"Agents past their review now ({found['grace_days']} days into the quarter): {late}; with no owner: {ownerless}",
        f"Reviewed by: {_counts(dict(by))}; reviews refused (not the owner or an approver): {found['refused']}",
    ]


def _c(framework, ref, title, evidence, files) -> dict:
    return {"framework": framework, "ref": ref, "title": title, "evidence": evidence,
            "files": files}


def _counts(counts: dict | None) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "none"


def render(month: str, facts: dict, files: set[str]) -> tuple[str, str]:
    """controls.md and controls.json for the pack. `files` are the pack's other files, so
    a control never points at one this pack lacks (receipts.jsonl needs a signing key)."""
    found = warnings(facts)
    mapped = controls(facts)
    for c in mapped:
        c["files"] = [f for f in c["files"] if f in files]
    lines = [
        f"# Controls evidence for {month}",
        "",
        ("Quarter provides evidence for these controls. It does not certify compliance;"
         " your auditor decides whether a control is met."),
        "",
        ("The EU AI Act articles bind high-risk AI systems. Whether yours is high-risk is"
         " yours to decide. Article text: Regulation (EU) 2024/1689, OJ L, 12.7.2024."
         " SOC 2 criterion names are paraphrased, not quoted. AIUC-1 ids and titles are"
         " from its Q4 2026 update."),
        "",
    ]
    if found:
        lines += ["## Warnings", ""] + [f"- {w}" for w in found] + [""]
    for framework in ("AIUC-1", "SOC 2", "EU AI Act"):
        lines += [f"## {framework}", ""]
        for c in mapped:
            if c["framework"] == framework:
                lines += [f"### {c['ref']}: {c['title']}", ""]
                lines += [f"- {e}" for e in c["evidence"]]
                lines += [f"- Files: {', '.join(c['files'])}", ""]
    reviews = facts.get("reviews")
    if reviews:
        lines += ["## Agent owner reviews", "",
                  f"Each quarter's review is due {reviews['grace_days']} days into the next quarter.", ""]
        for quarter, v in reviews["quarters"].items():
            lines += [f"### {quarter}", ""]
            lines += [f"- {r['agent']}: {r['result']}, owner {r['owner'] or 'none'}, reviewed by"
                      f" {r['reviewer']} on {r['at'][:10]}" + (f" ({r['note']})" if r["note"] else "")
                      for r in v["reviews"]]
            lines += [f"- Active and not reviewed: {', '.join(v['not_reviewed']) or 'none'}", ""]
        lines += [f"- Past their review now: {', '.join(reviews['overdue_now']) or 'none'}",
                  f"- With no owner now: {', '.join(reviews['no_owner_now']) or 'none'}", ""]
    doc = {"month": month, "warnings": found, "facts": facts, "controls": mapped}
    return "\n".join(lines), json.dumps(doc, indent=2)
