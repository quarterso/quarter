"""Month-end journal for accounting software.

One expense line per customer and vendor for what agents spent, one per cost people paid
for their agents that month (seats, subscriptions, compute), and one balancing line.
Spend that served a customer is cost of revenue, so it lands in gross margin; spend with
no customer is an operating expense.

`csv` is the plain journal, with the customer in its own column. `quickbooks`, `xero`
and `netsuite` are laid out for each one's own journal import, so the file goes in as it
is: the accounts must already exist there under the names (Xero: the codes) given.

With cost centres, each agent's spend also carries the department, class or location it is
budgeted to, and the expense lines split by them:

    {"department": {"claude-code": "Engineering", "support-bot": "Support", "*": "Other"},
     "class": {"claude-code": "R&D"}}

"*" takes every agent not named. An agent with no entry and no "*" is left unassigned.

Where vendor bills go into the books through accounts payable, the accrual is only the spend
a vendor has not billed yet, reversed on the first day of the next month when the bill posts.

`focus` is not a journal: it is one row per charge in FOCUS 1.4, the FinOps Foundation's cost and
usage format (focus_rows), so FinOps tools and data warehouses read it as it is.
"""

import csv
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from io import StringIO

from .connect import is_observed
from .money import MICROS, cents_str, to_cents
from .store import Ledger, month_range

FORMATS = ("csv", "quickbooks", "xero", "netsuite")
# Most lines each importer takes in one file, where it states one.
LINE_LIMITS = {"quickbooks": 999, "xero": 300}
# The column each importer reads a cost centre from. Xero has tracking categories instead.
DIMENSIONS = ("department", "class", "location")
CENTRE_COLUMNS = {
    "csv": {"department": "Department", "class": "Class", "location": "Location"},
    "quickbooks": {"class": "Class", "location": "Location"},
    "netsuite": {"department": "Department", "class": "Class", "location": "Location"},
}
XERO_TRACKING_LIMIT = 2
DATE_FORMATS = {
    "csv": "%Y-%m-%d",
    "quickbooks": "%m/%d/%Y",
    "xero": "%m/%d/%Y",
    "netsuite": "%m/%d/%Y",
}


@dataclass
class Line:
    account: str
    debit: int  # cents
    credit: int
    description: str
    customer: str
    centres: tuple[str, ...] = ()  # one per dimension in the cost centres, "" if unassigned


def read_centres(text: str) -> dict[str, dict[str, str]]:
    raw = json.loads(text)
    if not isinstance(raw, dict) or not raw:
        raise ValueError('must be an object such as {"department": {"claude-code": "Engineering"}}')
    for dimension, centres in raw.items():
        if dimension not in DIMENSIONS:
            raise ValueError(f"{dimension}: not a cost centre; use {', '.join(DIMENSIONS)}")
        if (
            not isinstance(centres, dict)
            or not centres
            or not all(k and isinstance(v, str) and v.strip() for k, v in centres.items())
        ):
            raise ValueError(f'{dimension}: must map agents to centre names, "*" for the rest')
    return {d: {agent: c.strip() for agent, c in raw[d].items()} for d in DIMENSIONS if d in raw}


def centres_of(centres: dict[str, dict[str, str]], agent: str | None) -> tuple[str, ...]:
    return tuple(c.get(agent or "*", c.get("*", "")) for c in centres.values())


def _charges(ledger: Ledger, start, end, centres) -> list[tuple]:
    """Charges per customer and vendor, and per cost centre when there are any."""
    if centres is None:
        return [(c, v, (), amount) for c, v, amount in ledger.totals("customer", start, end)]
    split: dict[tuple, int] = {}
    for customer, vendor, agent, amount in ledger.db.execute(
        "select customer, vendor, agent, sum(amount) from entries where kind = 'charge'"
        " and at >= ? and at < ? group by customer, vendor, agent",
        (start.isoformat(), end.isoformat()),
    ):
        key = (customer or "", vendor, centres_of(centres, agent))
        split[key] = split.get(key, 0) + amount
    return [(*key, amount) for key, amount in sorted(split.items())]


def centre_headers(fmt: str, dimensions: list[str]) -> list[str]:
    """Each importer's columns for the cost centres; a ValueError if it cannot take them."""
    if fmt == "xero":
        if len(dimensions) > XERO_TRACKING_LIMIT:
            raise ValueError(f"Xero takes {XERO_TRACKING_LIMIT} tracking categories at most")
        numbers = range(1, len(dimensions) + 1)
        return [h for i in numbers for h in (f"TrackingName{i}", f"TrackingOption{i}")]
    if fmt == "quickbooks" and "department" in dimensions:
        raise ValueError("QuickBooks Online tracks departments as locations; use location")
    return [CENTRE_COLUMNS[fmt][d] for d in dimensions] if fmt in CENTRE_COLUMNS else []


def lines(
    ledger: Ledger,
    month: str,
    expense_account: str = "AI vendor costs",
    offset_account: str = "Accrued AI vendor costs",
    revenue_cost_account: str = "Cost of revenue: AI",
    centres: dict[str, dict[str, str]] | None = None,
) -> list[Line]:
    start, end = month_range(month)
    out, total = [], 0  # total of the rounded lines, so the entry always balances
    for customer, vendor, centre, amount in _charges(ledger, start, end, centres):
        cents = to_cents(amount)
        if cents:
            total += cents
            account = revenue_cost_account if customer else expense_account
            out.append(Line(account, cents, 0, f"{vendor} usage", customer or "", centre))
    for cost in ledger.costs_in(month):
        cents = to_cents(cost["amount"])
        if cents:
            total += cents
            what = f"{cost['category']}: {cost['vendor']}" + (
                f" ({cost['agent']})" if cost["agent"] else ""
            )
            account = revenue_cost_account if cost["customer"] else expense_account
            centre = centres_of(centres, cost["agent"]) if centres else ()
            out.append(Line(account, cents, 0, what, cost["customer"] or "", centre))
    if total:
        out.append(Line(offset_account, 0, total, "Agent spend accrual", ""))
    return out


def unbilled(
    ledger: Ledger,
    month: str,
    billed: dict[str, int],
    expense_account: str = "AI vendor costs",
    accrued_account: str = "Accrued liabilities",
    revenue_cost_account: str = "Cost of revenue: AI",
    centres: dict[str, dict[str, str]] | None = None,
) -> list[Line]:
    """The accrual for spend recorded that month that the vendor has not billed: per vendor,
    what Quarter charged past the billed total (in micro-dollars), spread over that vendor's
    lines in proportion. A vendor missing from billed has billed nothing. Costs recorded with
    `cost add` were paid already, so they are left out."""
    start, end = month_range(month)
    charges = _charges(ledger, start, end, centres)
    recorded: dict[str, int] = {}
    for _, vendor, _, amount in charges:
        recorded[vendor] = recorded.get(vendor, 0) + amount
    out, total = [], 0
    for customer, vendor, centre, amount in charges:
        gap = max(recorded[vendor] - billed.get(vendor, 0), 0)
        cents = to_cents(amount * gap // recorded[vendor]) if gap else 0
        if cents:
            total += cents
            account = revenue_cost_account if customer else expense_account
            out.append(Line(account, cents, 0, f"{vendor} usage not yet billed", customer or "",
                            centre))
    if total:
        out.append(Line(accrued_account, 0, total, "Unbilled agent spend accrual", ""))
    return out


def journal(
    ledger: Ledger,
    month: str,
    expense_account: str = "AI vendor costs",
    offset_account: str = "Accrued AI vendor costs",
    revenue_cost_account: str = "Cost of revenue: AI",
    fmt: str = "csv",
    date_format: str | None = None,
    customer_names: bool = False,
    tax_rate: str = "Tax Exempt",
    rows: list[Line] | None = None,
    number: str | None = None,
    narration: str | None = None,
    centres: dict[str, dict[str, str]] | None = None,
    reversal: str | None = None,
) -> str:
    """The journal as one file.

    customer_names puts the customer in each importer's Name column, which must then match
    a customer already in that system; otherwise it goes in the line's description.
    tax_rate is the Xero tax rate named on every line (no tax applies to a journal like this).
    rows, number and narration lay out another journal for the same month instead.
    reversal is the number of the same entry reversed on the first day of the next month.
    centres splits the expense lines by cost centre, in each importer's own columns."""
    dimensions = list(centres or {})
    extra = centre_headers(fmt, dimensions)
    _, end = month_range(month)
    date = datetime.fromordinal(end.toordinal() - 1).strftime(date_format or DATE_FORMATS[fmt])
    number = number or f"AGENT-{month}"
    if rows is None:
        rows = lines(
            ledger, month, expense_account, offset_account, revenue_cost_account, centres
        )
    narration = narration or f"Quarter agent spend {month}"
    posted = [(number, date, narration, r) for r in rows]
    if reversal:
        first = end.strftime(date_format or DATE_FORMATS[fmt])
        posted += [(reversal, first, f"{narration} reversed", replace(r, debit=r.credit, credit=r.debit))
                   for r in rows]

    def centred(line: Line) -> list[str]:
        named = line.centres or ("",) * len(dimensions)
        if fmt != "xero":
            return list(named)
        cells = []
        for dimension, centre in zip(dimensions, named, strict=True):
            cells += [dimension.title(), centre] if centre else ["", ""]
        return cells

    def described(line: Line) -> str:
        if customer_names or not line.customer:
            return line.description
        return f"{line.description} for {line.customer}"

    def amount(cents: int) -> str:
        return cents_str(cents) if cents else ""

    out = StringIO()
    writer = csv.writer(out)
    if fmt == "csv":
        writer.writerow(
            ["Journal No", "Date", "Account", "Debit", "Credit", "Description", "Customer"]
            + extra
        )
        for no, on, narrated, r in posted:
            writer.writerow(
                [
                    no,
                    on,
                    r.account,
                    amount(r.debit),
                    amount(r.credit),
                    r.description,
                    r.customer,
                ]
                + centred(r)
            )
    elif fmt == "quickbooks":  # Settings, Import data, Journal entries
        writer.writerow(
            [
                "Journal No.",
                "Journal Date",
                "Account Name",
                "Debits",
                "Credits",
                "Description",
                "Name",
            ]
            + extra
        )
        for no, on, narrated, r in posted:
            name = r.customer if customer_names else ""
            writer.writerow(
                [no, on, r.account, amount(r.debit), amount(r.credit), described(r), name]
                + centred(r)
            )
    elif fmt == "xero":  # Manual journals, Import. One Amount column: debits positive
        writer.writerow(
            ["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount"]
            + extra
        )
        for no, on, narrated, r in posted:
            signed = cents_str(r.debit) if r.debit else "-" + cents_str(r.credit)
            writer.writerow(
                [narrated, on, described(r), r.account, tax_rate, signed] + centred(r)
            )
    elif fmt == "netsuite":  # Import CSV records, Transactions, Journal entry
        writer.writerow(
            ["External ID", "Date", "Account", "Debit", "Credit", "Memo", "Name"] + extra
        )
        for no, on, narrated, r in posted:
            name = r.customer if customer_names else ""
            writer.writerow(
                [no, on, r.account, amount(r.debit), amount(r.credit), described(r), name]
                + centred(r)
            )
    else:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    return out.getvalue()


# FOCUS 1.4 (github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec, tag v1.4): its Mandatory
# Cost and Usage columns in the spec's own order, then the Conditional and Recommended ones Quarter
# can fill, then Quarter's own, prefixed x_ as the spec's Custom Column Handling requires.
FOCUS_MANDATORY = (
    "BilledCost", "BillingAccountId", "BillingAccountName", "BillingCurrency", "BillingPeriodEnd",
    "BillingPeriodStart", "ChargeCategory", "ChargeClass", "ChargeDescription", "ChargePeriodEnd",
    "ChargePeriodStart", "ContractedCost", "EffectiveCost", "HostProviderName", "InvoiceIssuerName",
    "ListCost", "PricingQuantity", "PricingUnit", "ServiceProviderName", "ServiceCategory",
    "ServiceName",
)
FOCUS_OPTIONAL = (
    "ChargeFrequency", "ConsumedQuantity", "ConsumedUnit", "ServiceSubcategory", "SkuId", "SkuPriceId",
    "Tags",
)
FOCUS_QUARTER = (
    "x_CallId", "x_Agent", "x_Task", "x_Customer", "x_Governed", "x_InputTokens", "x_OutputTokens",
    "x_CacheReadTokens", "x_CacheWriteTokens",
)
FOCUS_COLUMNS = FOCUS_MANDATORY + FOCUS_OPTIONAL + FOCUS_QUARTER


def _decimal(micros: int) -> str:
    """Micro-dollars as an exact decimal, as FOCUS's numeric format allows: no symbol, no comma."""
    sign = "-" if micros < 0 else ""
    return f"{sign}{abs(micros) // MICROS}.{abs(micros) % MICROS:06d}"


def _focus_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def focus_rows(ledger: Ledger, start: datetime | None = None, end: datetime | None = None,
               accounts: dict[str, str] | None = None) -> list[dict]:
    """One FOCUS row per charge in the entries table, gateway and observed alike, from start
    (inclusive) to end (exclusive). Holds and releases are not cost, so they are left out.

    The charge period is the UTC day the spend happened: a vendor total covers a day, a call falls
    in one. The billing period is its calendar month. accounts gives each vendor's billing account
    id; without one, the vendor name stands in for it. Tokens come from the calls table, which the
    gateway fills for every call and connect for records that carried a task and tokens."""
    sql = (
        "select e.call_id, e.at, e.agent, e.task, e.customer, e.vendor, e.model, e.amount,"
        " c.input, c.output, c.cache_read, c.cache_write, c.cost, c.list_cost"
        " from entries e left join calls c on c.call_id = e.call_id where e.kind = 'charge'"
    )
    args: list = []
    if start is not None:
        sql += " and e.at >= ?"
        args.append(start.isoformat())
    if end is not None:
        sql += " and e.at < ?"
        args.append(end.isoformat())
    out = []
    for (call, at, agent, task, customer, vendor, model, amount,
         given, made, read, wrote, cost, list_cost) in ledger.db.execute(sql + " order by e.at, e.id", args):
        day = datetime.fromisoformat(at).astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        month, month_end = month_range(day.strftime("%Y-%m"))
        tokens = None if given is None else given + made + read + wrote
        listed = list_cost if cost == amount and list_cost is not None else amount
        account = (accounts or {}).get(vendor, vendor)
        tags = {k: v for k, v in (("agent", agent), ("customer", customer), ("task", task)) if v}
        out.append({
            "BilledCost": _decimal(amount),
            "BillingAccountId": account,
            "BillingAccountName": account,
            "BillingCurrency": "USD",
            "BillingPeriodEnd": _focus_time(month_end),
            "BillingPeriodStart": _focus_time(month),
            "ChargeCategory": "Usage",
            "ChargeClass": None,
            "ChargeDescription": f"{vendor} {model} usage" if model else f"{vendor} usage",
            "ChargePeriodEnd": _focus_time(day + timedelta(days=1)),
            "ChargePeriodStart": _focus_time(day),
            "ContractedCost": _decimal(amount),
            "EffectiveCost": _decimal(amount),
            "HostProviderName": vendor,
            "InvoiceIssuerName": vendor,
            "ListCost": _decimal(listed),
            "PricingQuantity": tokens,
            "PricingUnit": "Tokens" if tokens is not None else None,
            "ServiceProviderName": vendor,
            "ServiceCategory": "AI and Machine Learning" if model else "Other",
            "ServiceName": vendor,
            "ChargeFrequency": "Usage-Based",
            "ConsumedQuantity": tokens,
            "ConsumedUnit": "Tokens" if tokens is not None else None,
            "ServiceSubcategory": "Generative AI" if model else "Other (Other)",
            "SkuId": model,
            "SkuPriceId": model,
            "Tags": json.dumps(tags, sort_keys=True) if tags else None,
            "x_CallId": call,
            "x_Agent": agent,
            "x_Task": task,
            "x_Customer": customer,
            "x_Governed": "false" if is_observed(call) else "true",
            "x_InputTokens": given,
            "x_OutputTokens": made,
            "x_CacheReadTokens": read,
            "x_CacheWriteTokens": wrote,
        })
    return out


def focus(rows: list[dict]) -> str:
    """The rows as FOCUS CSV: a header of column ids, and an empty cell for null."""
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(FOCUS_COLUMNS)
    for row in rows:
        writer.writerow(["" if row[c] is None else row[c] for c in FOCUS_COLUMNS])
    return out.getvalue()
