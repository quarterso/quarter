"""The month's AI spend and margin on one page, in Markdown, for a board deck or an investor update.

Every figure comes from a report Quarter already makes, so the page agrees with the journal,
the margin, cost per outcome and the close: nothing here is worked out a second way.
"""

from datetime import UTC, datetime, timedelta

from .capitalise import Project, capitalised
from .commits import Commit, standing
from .export import lines
from .margin import ai_cost, margins
from .money import usd_display
from .outcomes import cost_per_outcome
from .reconcile import recorded, unreconciled
from .store import Ledger, month_range

TOP = 5


def _usd(cents: int) -> str:
    return ("-$" if cents < 0 else "$") + usd_display(abs(cents) * 10_000)


def _usd_micros(micros: int) -> str:
    return _usd(round(micros / 10_000))


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _accounts(ledger: Ledger, month: str, centres: dict | None) -> dict[str, int]:
    """Debits by account, in cents, with opex split by cost centre class when there is one."""
    split = list(centres or {}).index("class") if centres and "class" in centres else None
    out: dict[str, int] = {}
    for line in lines(ledger, month, centres=centres):
        if not line.debit:
            continue
        name = line.account
        if split is not None and not line.customer and line.centres[split]:
            name = f"{line.account} ({line.centres[split]})"
        out[name] = out.get(name, 0) + line.debit
    return out


def board(
    ledger: Ledger,
    month: str,
    revenue: dict[str, int] | None = None,
    centres: dict | None = None,
    projects: list[Project] | None = None,
    commits: list[Commit] | None = None,
) -> str:
    start, end = month_range(month)
    before = f"{start - timedelta(days=1):%Y-%m}"
    now, last = _accounts(ledger, month, centres), _accounts(ledger, before, centres)
    total, last_total = sum(now.values()), sum(last.values())
    change = (total - last_total) / last_total * 100 if last_total else None

    out = [f"# AI spend and margin, {month}", "", "## Spend", "",
           f"Total AI spend {_usd(total)}, against {_usd(last_total)} in {before} ({_pct(change)}).", "",
           "| Account | This month | Last month |", "|---|---:|---:|"]
    out += [f"| {name} | {_usd(cents)} | {_usd(last.get(name, 0))} |" for name, cents in sorted(now.items())]
    if projects:
        moved = capitalised(ledger, month, projects)["total"]
        out += ["", (f"Of this, {_usd_micros(moved)} is capitalised as internal-use software (ASU 2025-06),"
                " moving from expense to an asset.")]

    out += ["", "## Margin", ""]
    if revenue is not None:
        m = margins(ledger, month, revenue)
        out.append(f"Revenue {_usd_micros(m['revenue'])}, AI cost to serve {_usd_micros(m['ai_cost'])},"
                   f" margin after AI cost {_usd_micros(m['margin'])} ({_pct(m['margin_pct'])}).")
        if m["actual"]:
            a = m["actual"]
            out.append(f"At the vendors' own figures: AI cost {_usd_micros(a['ai_cost'])}, margin"
                       f" {_usd_micros(m['revenue'] - a['ai_cost'])}.")
        if m["unbilled_customers"]:
            out.append(f"{m['unbilled_customers']} customer(s) cost {_usd_micros(m['unbilled_cost'])} in AI"
                       " and paid nothing this month.")
        heaviest = sorted(m["rows"], key=lambda r: -r["ai_cost"])[:TOP]
        out += ["", "| Customer | AI cost | Revenue | Margin |", "|---|---:|---:|---:|"]
        out += [f"| {r['customer']} | {_usd_micros(r['ai_cost'])} | {_usd_micros(r['revenue'])} |"
                f" {_pct(r['margin_pct'])} |" for r in heaviest]
    else:
        cost, _ = ai_cost(ledger, month)
        out += ["No revenue was given, so no margin (`--revenue` or `--stripe`).", "",
                "| Customer | AI cost |", "|---|---:|"]
        out += [f"| {c} | {_usd_micros(v)} |" for c, v in sorted(cost.items(), key=lambda kv: -kv[1])[:TOP]]

    outcomes = cost_per_outcome(ledger, month)
    if outcomes["rows"]:
        out += ["", "## Cost per outcome", "", "| Outcome | Count | Cost each |", "|---|---:|---:|"]
        out += [f"| {k['kind']} | {k['count']} |"
                f" {'n/a' if k['per_outcome'] is None else _usd_micros(k['per_outcome'])} |"
                for k in outcomes["rows"][:TOP]]

    if commits:
        as_of = min(datetime.now(UTC), end - timedelta(days=1)).date()  # a past month is read as at its last day
        found = [standing(ledger, c, as_of) for c in commits]
        out += ["", "## Vendor commitments", "", f"At the last 30 days' pace, as of {as_of}.", "",
                "| Vendor | Term | Committed | Spent | Projected by the end |", "|---|---|---:|---:|---:|"]
        out += [f"| {c['vendor']} | {c['from']} to {c['to']} | {_usd_micros(c['amount'])} | {_usd_micros(c['spent'])}"
                f" | {_usd_micros(c['projected'])} |" for c in found]
        out += [f"\n**Projected shortfall:** {c['vendor']} {_usd_micros(c['shortfall'])} by {c['to']},"
                " committed and not used at this pace." for c in found if c["shortfall"]]

    matched, missing = sorted(recorded(ledger, month)), unreconciled(ledger, month)
    out += ["", "## Close", "",
            f"Reconciled with the vendors' own figures: {', '.join(matched) or 'none'}."
            + (f" Not reconciled: {', '.join(missing)}." if missing else "")]
    return "\n".join(out) + "\n"
