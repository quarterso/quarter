"""The vendor's bill against what Quarter recorded: the AI side of a three-way match.

The vendor's figure comes from its own admin cost API, with the customer's own admin key,
or from a CSV exported from the vendor's console. Quarter's figure is its charges to that
vendor that month. Both are compared per model and in total.

The vendor APIs, as their documentation states them (read 2026-11-02):

OpenAI, https://developers.openai.com/api/reference/resources/organization/subresources/audit_logs/methods/get_costs
and https://developers.openai.com/cookbook/examples/completions_usage_api:
GET https://api.openai.com/v1/organization/costs with "Authorization: Bearer <admin key>";
start_time (Unix seconds, inclusive), end_time (exclusive), bucket_width 1d (the only one),
group_by line_item, limit 1 to 180 buckets (default 7), page from the last reply's
next_page while has_more. Each bucket has start_time and results; each result has
amount.value and amount.currency ("usd") and line_item, such as "gpt-4o-2024-08-06, input".
The reference does not say in words that amount.value is dollars; its example (0.06 "usd")
and the cookbook's "Total Cost (USD)" read it so, and so does this module.

Anthropic, https://platform.claude.com/docs/en/manage-claude/usage-cost-api and
https://platform.claude.com/docs/en/api/beta/organization/cost_report/retrieve:
GET https://api.anthropic.com/v1/organizations/cost_report with "x-api-key: <admin key>"
(sk-ant-admin01-...) and "anthropic-version: 2023-06-01"; starting_at and ending_at
(RFC 3339), bucket_width 1d, group_by[]=description, limit 1 to 31 buckets (default 7),
page from next_page while has_more. Each bucket has starting_at and results; each result
has amount (a decimal string in cents: "123.45" is $1.23), currency ("USD"), description
and model (null for costs that are not tokens). Anthropic calls this endpoint legacy but
supported, and says Priority Tier costs are not in it.

Anthropic says a bucket is returned when it ends "before" ending_at, without saying whether
a bucket ending exactly then counts. So both vendors are asked for one day past the month,
and only buckets starting inside the month are kept.
"""

import csv
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO

from .bills import _NoRedirect
from .money import to_micros
from .store import Ledger, month_range

VENDORS = ("openai", "anthropic")
KEY_ENV = {"openai": "OPENAI_ADMIN_KEY", "anthropic": "ANTHROPIC_ADMIN_KEY"}
URLS = {"openai": "https://api.openai.com", "anthropic": "https://api.anthropic.com"}
ANTHROPIC_VERSION = "2023-06-01"
PAGE_LIMIT = 31  # buckets per page: a month and the day past it fit in two pages at most
MAX_PAGES = 50

DATE_COLUMNS = ("date", "day", "period", "month", "start_time", "starting_at", "usage date")
ITEM_COLUMNS = ("model", "line_item", "line item", "description", "item")
AMOUNT_COLUMNS = ("amount_usd", "amount", "cost_usd", "cost", "total")
DATED = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")  # gpt-4o-2024-08-06, claude-...-20250929


def model_of(item: str) -> str:
    """The model a line item is for: "gpt-4o-2024-08-06, input" is gpt-4o-2024-08-06.
    A line item of another product ("assistants api | file search") is kept whole."""
    return item.rsplit(", ", 1)[0].strip() if ", " in item else item.strip()


def same_model(name: str) -> str:
    """The name both sides are matched on: without a dated version suffix, so an agent asking
    for gpt-4o matches the vendor billing gpt-4o-2024-08-06."""
    return DATED.sub("", name.strip().lower())


def _get(url: str, headers: dict, key: str) -> dict:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode(errors="replace").replace(key, "***")
        raise ValueError(f"answered {e.code}: {body}") from None
    except (OSError, ValueError) as e:  # URLError, timeouts, a reply that is not JSON
        raise ValueError(f"could not be read: {str(e).replace(key, '***')}") from None


def fetch(vendor: str, key: str, month: str, base: str | None = None) -> dict[str, int]:
    """The vendor's cost for the month by model (or line item), in micro-dollars."""
    start, end = month_range(month)
    totals: dict[str, Decimal] = {}
    for (_day, item, _group), usd in fetch_days(vendor, key, start, end, base).items():
        totals[item] = totals.get(item, Decimal(0)) + usd
    return {item: to_micros(usd) for item, usd in totals.items()}


def fetch_days(
    vendor: str, key: str, start: datetime, end: datetime, base: str | None = None,
    by_group: bool = False,
) -> dict[tuple[str, str, str | None], Decimal]:
    """The vendor's cost in dollars by (UTC day, model or line item, group) for the buckets
    starting in [start, end). The group is the OpenAI project or Anthropic workspace when
    by_group is set, else None. connect.py reads it per day; fetch sums it for a month."""
    past = end + timedelta(days=1)
    base = (base or URLS[vendor]).rstrip("/")
    if vendor == "openai":
        path = "/v1/organization/costs"
        query = {"start_time": int(start.timestamp()), "end_time": int(past.timestamp()),
                 "bucket_width": "1d", "group_by": ["line_item", "project_id"] if by_group else "line_item",
                 "limit": PAGE_LIMIT}
        headers = {"authorization": f"Bearer {key}"}
    else:
        path = "/v1/organizations/cost_report"
        query = {"starting_at": start.isoformat().replace("+00:00", "Z"),
                 "ending_at": past.isoformat().replace("+00:00", "Z"),
                 "bucket_width": "1d",
                 "group_by[]": ["description", "workspace_id"] if by_group else "description",
                 "limit": PAGE_LIMIT}
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    totals: dict[tuple, Decimal] = {}
    page, seen = None, set()
    for _ in range(MAX_PAGES):
        url = base + path + "?" + urllib.parse.urlencode(query | ({"page": page} if page else {}), doseq=True)
        reply = _get(url, headers, key)
        if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
            raise ValueError("answered without a data list")  # noqa: TRY004, a bad reply, not a bad call
        for bucket in reply["data"]:
            _add_bucket(vendor, bucket, start, end, totals, by_group)
        page = reply.get("next_page")
        if not reply.get("has_more") or not page:
            return totals
        if page in seen:
            raise ValueError("returned the same page twice")
        seen.add(page)
    raise ValueError(f"had more than {MAX_PAGES} pages")


def _add_bucket(
    vendor: str, bucket: dict, start: datetime, end: datetime, totals: dict[tuple, Decimal],
    by_group: bool = False,
) -> None:
    try:
        if vendor == "openai":
            from_ = datetime.fromtimestamp(int(bucket["start_time"]), UTC)
        else:
            from_ = datetime.fromisoformat(str(bucket["starting_at"]))
        results = bucket["results"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("answered with a bucket that has no start or results") from None
    from_ = from_ if from_.tzinfo else from_.replace(tzinfo=UTC)
    if not start <= from_ < end:
        return
    for r in results:
        try:
            if vendor == "openai":
                currency, usd = r["amount"]["currency"], Decimal(str(r["amount"]["value"]))
                item = model_of(r.get("line_item") or "") or "(no line item)"
                group = r.get("project_id")
            else:  # cents, as a decimal string
                currency, usd = r["currency"], Decimal(str(r["amount"])) / 100
                item = r.get("model") or r.get("description") or "(no description)"
                group = r.get("workspace_id")
        except (KeyError, TypeError, ArithmeticError):
            raise ValueError("answered with a cost that has no amount") from None
        if str(currency).lower() != "usd":
            raise ValueError(f"answered in {currency}; only USD is compared")
        if not usd.is_finite():
            raise ValueError(f"answered with an amount of {usd}")
        slot = (from_.date().isoformat(), item, group if by_group else None)
        totals[slot] = totals.get(slot, Decimal(0)) + usd


def read_costs(text: str, month: str) -> dict[str, int]:
    """The vendor's cost for the month by model or line item, in micro-dollars, from a CSV
    with a date or period column, an optional model or line item column and an amount
    column in dollars. Rows dated outside the month are left out."""
    start, _ = month_range(month)
    rows = csv.DictReader(StringIO(text.lstrip("﻿")))
    fields = {f.strip().lower(): f for f in rows.fieldnames or []}
    when = next((fields[c] for c in DATE_COLUMNS if c in fields), None)
    item = next((fields[c] for c in ITEM_COLUMNS if c in fields), None)
    amount = next((fields[c] for c in AMOUNT_COLUMNS if c in fields), None)
    if when is None or amount is None:
        raise ValueError(
            f"need a date column ({', '.join(DATE_COLUMNS)}) and an amount column"
            f" ({', '.join(AMOUNT_COLUMNS)})"
        )
    totals: dict[str, Decimal] = {}
    for line, row in enumerate(rows, start=2):
        day = (row[when] or "").strip()
        raw = (row[amount] or "").replace("$", "").replace(",", "").strip()
        if not day and not raw:
            continue
        if not re.match(r"^\d{4}-\d{2}(\b|$)", day):
            raise ValueError(f"line {line}: {row[when]!r} is not a date, YYYY-MM-DD or YYYY-MM")
        try:
            usd = Decimal(raw)
            to_micros(usd)
        except (ArithmeticError, ValueError):  # InvalidOperation, and Infinity's OverflowError
            raise ValueError(f"line {line}: {row[amount]!r} is not an amount") from None
        if day[:7] != start.strftime("%Y-%m"):
            continue
        name = model_of((row[item] or "") if item else "") or "(no model)"
        totals[name] = totals.get(name, Decimal(0)) + usd
    return {name: to_micros(usd) for name, usd in totals.items()}


def quarter_side(ledger: Ledger, vendor: str, month: str) -> tuple[dict[str, int], dict]:
    """Quarter's charges to the vendor that month by model, and the calls a subscription
    paid for: recorded at no cost, with what they were worth at list price."""
    start, end = month_range(month)
    span = (vendor, start.isoformat(), end.isoformat())
    rows = ledger.db.execute(  # less what connect booked from this same cost API: not a second opinion
        "select coalesce(model, ''), sum(amount) from entries where kind = 'charge'"
        " and vendor = ? and at >= ? and at < ? and substr(call_id, 1, ?) != ? group by model",
        (*span, len(f"obs:{vendor}:"), f"obs:{vendor}:"),
    ).fetchall()
    calls, worth = ledger.db.execute(
        "select count(*), coalesce(sum(list_cost), 0) from calls where route = 'subscription'"
        " and vendor = ? and at >= ? and at < ?",
        span,
    ).fetchone()
    return {model or "(no model)": amount for model, amount in rows}, {
        "calls": calls,
        "list_cost": worth,
    }


def compare(vendor_costs: dict[str, int], quarter: dict[str, int], tolerance_pct: float) -> dict:
    """One row per model, the largest difference first, and the totals. A difference is
    flagged when it is over tolerance_pct of the vendor's figure and at least a cent."""
    billed: dict[str, list] = {}
    for side, costs in ((0, vendor_costs), (1, quarter)):
        for name, amount in costs.items():
            row = billed.setdefault(same_model(name), [set(), 0, 0])
            row[0].add(name)
            row[side + 1] += amount
    rows = [_row(" / ".join(sorted(names)), v, q, tolerance_pct)
            for names, v, q in billed.values() if v or q]
    rows.sort(key=lambda r: (-abs(r["difference"]), r["model"]))
    total = _row("total", sum(vendor_costs.values()), sum(quarter.values()), tolerance_pct)
    return {"rows": rows, "total": total}


def _row(model: str, vendor: int, quarter: int, tolerance_pct: float) -> dict:
    difference = vendor - quarter
    pct = difference / vendor * 100 if vendor else None
    if abs(difference) < 10_000:
        flag = ""
    elif not quarter:
        flag = "outside"  # billed, and Quarter never saw it
    elif not vendor:
        flag = "unbilled"  # Quarter saw it, and the vendor did not bill it
    else:
        flag = "over" if abs(pct) > tolerance_pct else ""
    return {"model": model, "vendor": vendor, "quarter": quarter, "difference": difference,
            "difference_pct": pct, "flag": flag}


def record(ledger: Ledger, vendor: str, month: str, source: str, out: dict, by: str) -> None:
    """The month's match for this vendor, into the audit chain: what `close` leaves behind for
    the audit pack, and for margin at the vendor's own figures."""
    rows = [{"model": r["model"], "vendor": r["vendor"], "quarter": r["quarter"]} for r in out["rows"]]
    ledger.audit("reconciled", by, json.dumps({
        "month": month, "vendor": vendor, "source": source,
        "vendor_total": out["total"]["vendor"], "quarter_total": out["total"]["quarter"], "rows": rows,
    }))


def recorded(ledger: Ledger, month: str) -> dict[str, dict]:
    """The latest recorded match for the month, by vendor, with when and by whom."""
    out = {}
    for at, by, detail in ledger.db.execute(
        "select at, client, detail from audit where action = 'reconciled'"
        " and json_extract(detail, '$.month') = ? order by id", (month,),
    ):
        found = json.loads(detail)
        out[found["vendor"]] = found | {"at": at, "by": by}
    return out


def unreconciled(ledger: Ledger, month: str) -> list[str]:
    """Vendors Quarter can match that had spend this month and no recorded match."""
    start, end = month_range(month)
    spent = {v for (v,) in ledger.db.execute(
        "select distinct vendor from entries where kind = 'charge' and at >= ? and at < ?",
        (start.isoformat(), end.isoformat()))}
    return sorted((spent & set(VENDORS)) - set(recorded(ledger, month)))
