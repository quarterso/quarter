"""Margin after AI cost, by customer: revenue from your billing export against the AI
cost Quarter booked to each customer that month.

Vendor invoices name no customer, so this is where cost to serve meets revenue. It is
margin after AI cost only; hosting, support and other cost of revenue are not here.
"""

import csv
import re
import urllib.parse
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from math import ceil
from statistics import median

from .money import to_micros
from .reconcile import _get, recorded, same_model
from .store import Ledger, month_range

CUSTOMER_COLUMNS = ("customer", "customer_id", "customer id", "customer name")
AMOUNT_COLUMNS = ("amount_usd", "amount", "revenue", "amount paid", "total")
HEAVY_SHARE = 0.05  # the heaviest 5% of customers by AI cost
STRIPE_URL = "https://api.stripe.com"
STRIPE_KEY_ENV = "STRIPE_REVENUE_KEY"
STRIPE_LOOKBACK = timedelta(days=120)  # an invoice paid this month may have been created months before
STRIPE_MAX_PAGES = 500
CUSTOMER_ID = re.compile(r"cus_[A-Za-z0-9]{1,64}")


def read_revenue(text: str) -> dict[str, int]:
    """Revenue by customer, in micro-dollars, from a CSV with a customer and an amount
    column. Header names are matched without regard to case; repeated customers add up;
    "$1,234.50" is read as 1234.50."""
    rows = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    fields = {f.strip().lower(): f for f in rows.fieldnames or []}
    customer = next((fields[c] for c in CUSTOMER_COLUMNS if c in fields), None)
    amount = next((fields[c] for c in AMOUNT_COLUMNS if c in fields), None)
    if customer is None or amount is None:
        raise ValueError(
            f"need a customer column ({', '.join(CUSTOMER_COLUMNS)}) and an amount column"
            f" ({', '.join(AMOUNT_COLUMNS)})"
        )
    out: dict[str, int] = {}
    for line, row in enumerate(rows, start=2):
        who = (row[customer] or "").strip()
        raw = (row[amount] or "").replace("$", "").replace(",", "").strip()
        if not who and not raw:
            continue
        try:
            value = to_micros(Decimal(raw))
        except (ArithmeticError, ValueError):  # InvalidOperation, and Infinity's OverflowError
            raise ValueError(f"line {line}: {row[amount]!r} is not an amount") from None
        if not who:
            raise ValueError(f"line {line}: no customer")
        out[who] = out.get(who, 0) + value
    return out


def stripe_customer(invoice: dict) -> str:
    """The name Quarter books a Stripe customer under: the customer's `quarter_customer`
    metadata when set (so it can match the customer your agents send), else the name on
    the invoice, else the Stripe customer id."""
    return _named(invoice.get("customer"), invoice.get("customer_name"))


def stripe_customer_named(key: str, customer_id: str, base: str = STRIPE_URL) -> str:
    """The same name for a customer that paid no invoice this month, read from the customer."""
    if not CUSTOMER_ID.fullmatch(customer_id):
        raise ValueError(f"{customer_id!r} is not a Stripe customer id")
    customer = _get(f"{base}/v1/customers/{customer_id}", {"Authorization": f"Bearer {key}"}, key)
    return _named(customer, customer.get("name"))


def _named(customer: dict | str | None, name: str | None) -> str:
    meta = (customer.get("metadata") or {}) if isinstance(customer, dict) else {}
    cid = customer.get("id") if isinstance(customer, dict) else customer
    return (meta.get("quarter_customer") or name or cid or "").strip()


def stripe_revenue(key: str, month: str, base: str = STRIPE_URL) -> tuple[dict[str, int], dict]:
    """Revenue by customer for the month from Stripe, read-only: invoices paid that month
    (cash basis), excluding tax, less credit notes issued after payment. USD only; other
    currencies are counted and left out. notes["ids"] maps each Stripe customer id to its
    name. Needs a restricted key that can read invoices and customers, nothing else."""
    if not key.startswith("rk_"):
        raise ValueError("use a restricted key (rk_...) with read access to invoices and customers;"
                         " Quarter never needs a full secret key")
    start, end = month_range(month)
    params = [("status", "paid"), ("limit", "100"), ("expand[]", "data.customer"),
              ("created[gte]", str(int((start - STRIPE_LOOKBACK).timestamp()))),
              ("created[lt]", str(int(end.timestamp())))]
    headers = {"Authorization": f"Bearer {key}"}
    out: dict[str, int] = {}
    notes = {"invoices": 0, "skipped_currency": 0, "test_mode": False, "ids": {}}
    after = None
    for _ in range(STRIPE_MAX_PAGES):
        query = urllib.parse.urlencode(params + ([("starting_after", after)] if after else []))
        page = _get(f"{base}/v1/invoices?{query}", headers, key)
        for inv in page.get("data") or []:
            paid = (inv.get("status_transitions") or {}).get("paid_at")
            if not paid or not start.timestamp() <= paid < end.timestamp():
                continue
            if inv.get("currency") != "usd":
                notes["skipped_currency"] += 1
                continue
            cents = (inv.get("total_excluding_tax") or 0) - (inv.get("post_payment_credit_notes_amount") or 0)
            who = stripe_customer(inv)
            out[who] = out.get(who, 0) + cents * 10_000
            customer = inv.get("customer")
            notes["ids"][customer.get("id") if isinstance(customer, dict) else customer] = who
            notes["invoices"] += 1
            notes["test_mode"] |= inv.get("livemode") is False
        if not page.get("has_more") or not page.get("data"):
            return out, notes
        after = page["data"][-1]["id"]
    raise ValueError(f"more than {STRIPE_MAX_PAGES} pages of invoices; stopped")


def ai_cost(ledger: Ledger, month: str) -> tuple[dict[str, int], int]:
    """AI cost by customer for the month (agent spend plus costs recorded for a customer),
    and what was spent for no customer, which is an operating expense instead."""
    start, end = month_range(month)
    by_customer: dict[str, int] = {}
    unassigned = 0
    for customer, _vendor, amount in ledger.totals("customer", start, end):
        if customer:
            by_customer[customer] = by_customer.get(customer, 0) + amount
        else:
            unassigned += amount
    for cost in ledger.costs_in(month):
        if cost["customer"]:
            by_customer[cost["customer"]] = by_customer.get(cost["customer"], 0) + cost["amount"]
        else:
            unassigned += cost["amount"]
    return by_customer, unassigned


def actual_costs(ledger: Ledger, month: str) -> dict | None:
    """Each customer's AI cost at the vendor's own figures, for a month `close` reconciled;
    None when no vendor was. Per model, where the vendor charged less than Quarter recorded
    (discounts, caching, credits), each customer's cost is scaled down by the same share.
    Where it charged more, the excess may be calls Quarter never saw, so customers keep their
    recorded cost and the excess is shown apart as not attributed. Models the vendor did not
    report, and spend connect booked from the vendor's own cost API, stay as recorded."""
    matched = recorded(ledger, month)
    if not matched:
        return None
    ratio, excess = {}, 0
    for vendor, found in matched.items():
        for r in found["rows"]:
            key = (vendor, same_model(r["model"].split(" / ")[0]))
            if r["quarter"] and r["vendor"] < r["quarter"]:
                ratio[key] = (r["vendor"], r["quarter"])
            excess += max(r["vendor"] - r["quarter"], 0)
    start, end = month_range(month)
    out: dict[str, int] = {}
    for customer, vendor, model, call, amount in ledger.db.execute(
        "select customer, vendor, coalesce(model, '(no model)'), call_id, amount from entries"
        " where kind = 'charge' and at >= ? and at < ?", (start.isoformat(), end.isoformat()),
    ):
        own = call.startswith(f"obs:{vendor}:")  # booked from the vendor's own figures already
        excess -= amount if own and vendor in matched else 0
        if customer:
            vendor_total, quarter_total = (None, None) if own else ratio.get((vendor, same_model(model)), (None, None))
            out[customer] = out.get(customer, 0) + (amount * vendor_total // quarter_total if quarter_total else amount)
    for cost in ledger.costs_in(month):
        if cost["customer"]:
            out[cost["customer"]] = out.get(cost["customer"], 0) + cost["amount"]
    return {"by_customer": out, "unattributed": max(excess, 0), "vendors": sorted(matched)}


def margins(ledger: Ledger, month: str, revenue: dict[str, int]) -> dict:
    """One row per customer with revenue or AI cost, worst margin first, and the totals
    finance asks about: margin overall, AI cost with no revenue behind it, how much of
    the cost the heaviest customers account for, and the median cost per customer."""
    cost, unassigned = ai_cost(ledger, month)
    rows = []
    for customer in sorted(set(cost) | set(revenue)):
        rev, spent = revenue.get(customer, 0), cost.get(customer, 0)
        pct = (rev - spent) / rev * 100 if rev > 0 else None
        rows.append(
            {
                "customer": customer,
                "revenue": rev,
                "ai_cost": spent,
                "margin": rev - spent,
                "margin_pct": pct,
            }
        )
    rows.sort(key=lambda r: (r["margin_pct"] is not None, r["margin_pct"] or 0, -r["ai_cost"]))
    actual = actual_costs(ledger, month)
    for r in rows if actual else []:
        r["actual_cost"] = actual["by_customer"].get(r["customer"], 0)
        r["actual_margin"] = r["revenue"] - r["actual_cost"]

    total_rev, total_cost = sum(revenue.values()), sum(cost.values())
    costs = sorted(cost.values(), reverse=True)
    heavy = ceil(len(costs) * HEAVY_SHARE) if costs else 0
    unbilled = [r for r in rows if r["revenue"] <= 0 and r["ai_cost"] > 0]
    return {
        "month": month,
        "rows": rows,
        "revenue": total_rev,
        "ai_cost": total_cost,
        "margin": total_rev - total_cost,
        "margin_pct": (total_rev - total_cost) / total_rev * 100 if total_rev > 0 else None,
        "unbilled_cost": sum(r["ai_cost"] for r in unbilled),
        "unbilled_customers": len(unbilled),
        "heavy_customers": heavy,
        "heavy_share_pct": sum(costs[:heavy]) / total_cost * 100 if total_cost else None,
        "median_cost": int(median(costs)) if costs else 0,
        "unassigned_cost": unassigned,
        "actual": None if actual is None else {
            "vendors": actual["vendors"],
            "ai_cost": sum(r["actual_cost"] for r in rows),
            "unattributed": actual["unattributed"],
        },
    }
