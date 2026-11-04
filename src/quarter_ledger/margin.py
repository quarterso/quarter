"""Margin after AI cost, by customer: revenue from your billing export against the AI
cost Quarter booked to each customer that month.

Vendor invoices name no customer, so this is where cost to serve meets revenue. It is
margin after AI cost only; hosting, support and other cost of revenue are not here.
"""

import csv
from decimal import Decimal
from io import StringIO
from math import ceil
from statistics import median

from .money import to_micros
from .store import Ledger, month_range

CUSTOMER_COLUMNS = ("customer", "customer_id", "customer id", "customer name")
AMOUNT_COLUMNS = ("amount_usd", "amount", "revenue", "amount paid", "total")
HEAVY_SHARE = 0.05  # the heaviest 5% of customers by AI cost


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
    }
