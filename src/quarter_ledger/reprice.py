"""Margin by customer under a scenario: a change in what customers pay, a vendor's new
prices, or moving a model's calls to another model.

The "before" is exactly `margin`. The "after" changes only the calls the scenario names,
and works each one out again from its own recorded tokens. Spend with no tokens to work
from (booked by connect from a vendor's cost API, or costs recorded by hand) and calls a
subscription pays for stay as they were; spend the scenario names that cannot be repriced
is counted, so the result says how much of it the answer leaves alone.
"""

from .margin import ai_cost
from .pricing import PriceBook, UnknownPrice
from .store import Ledger, month_range


def _pct(margin: int, revenue: int) -> float | None:
    return margin / revenue * 100 if revenue > 0 else None


def scenario_book(base: dict, prices: dict | None) -> PriceBook:
    """The prices a repriced call is charged at: the scenario's, laid over the base, model by model."""
    prices = prices or {}
    return PriceBook.load({v: {**base.get(v, {}), **prices.get(v, {})} for v in base.keys() | prices.keys()})


def reprice(
    ledger: Ledger,
    month: str,
    revenue: dict[str, int],
    base: dict,
    prices: dict | None = None,
    as_model: dict[tuple[str, str], str] | None = None,
    revenue_change_pct: float = 0.0,
) -> dict:
    """`base` is the full price book (built-in prices with the gateway's own over them);
    `prices` holds the scenario's vendor prices, and only the models it lists are repriced;
    `as_model` maps (vendor, model) to the model those calls move to, at its scenario price."""
    prices, as_model = prices or {}, as_model or {}
    book = scenario_book(base, prices)

    def named(vendor: str, model: str) -> bool:
        listed = prices.get(vendor, {})
        return (vendor, model) in as_model or model in listed or "*" in listed

    start, end = month_range(month)
    calls = ledger.db.execute(
        "select e.customer, e.vendor, coalesce(e.model, ''), e.call_id, sum(e.amount),"
        " c.route, c.input, c.output, c.cache_read, c.cache_write"
        " from entries e left join calls c on c.call_id = e.call_id"
        " where e.kind = 'charge' and e.at >= ? and e.at < ? group by e.call_id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    delta: dict[str, int] = {}
    repriced = unchanged = repriced_before = repriced_after = 0
    for customer, vendor, model, call_id, amount, route, *tokens in calls:
        if not named(vendor, model):
            continue
        if route != "api" or call_id.startswith(f"obs:{vendor}:"):
            unchanged += amount  # no tokens of its own, or a subscription pays for it
            continue
        target = as_model.get((vendor, model), model)
        try:
            cost = book.price(vendor, target).cost(*tokens)
        except UnknownPrice:
            raise ValueError(f"no price for {vendor}/{target}: add it to the prices file") from None
        repriced += 1
        repriced_before += amount
        repriced_after += cost
        if customer:
            delta[customer] = delta.get(customer, 0) + cost - amount

    cost_before, unassigned = ai_cost(ledger, month)
    scale = 1 + revenue_change_pct / 100
    rows = []
    for customer in sorted(set(cost_before) | set(revenue)):
        rev, spent = revenue.get(customer, 0), cost_before.get(customer, 0)
        rev_after, spent_after = round(rev * scale), spent + delta.get(customer, 0)
        rows.append({
            "customer": customer,
            "revenue": rev, "ai_cost": spent, "margin": rev - spent, "margin_pct": _pct(rev - spent, rev),
            "revenue_after": rev_after, "ai_cost_after": spent_after, "margin_after": rev_after - spent_after,
            "margin_pct_after": _pct(rev_after - spent_after, rev_after),
            "turns_negative": rev - spent >= 0 > rev_after - spent_after,
        })
    rows.sort(key=lambda r: (r["margin_pct_after"] is not None, r["margin_pct_after"] or 0, -r["ai_cost_after"]))

    total = {k: sum(r[k] for r in rows) for k in ("revenue", "ai_cost", "revenue_after", "ai_cost_after")}
    return {
        "month": month,
        "rows": rows,
        **total,
        "margin": total["revenue"] - total["ai_cost"],
        "margin_after": total["revenue_after"] - total["ai_cost_after"],
        "margin_pct": _pct(total["revenue"] - total["ai_cost"], total["revenue"]),
        "margin_pct_after": _pct(total["revenue_after"] - total["ai_cost_after"], total["revenue_after"]),
        "calls_repriced": repriced,
        "repriced_before": repriced_before,
        "repriced_after": repriced_after,
        "not_repriced": unchanged,
        "unassigned_cost": unassigned,
    }


def read_as_model(values: list[str]) -> dict[tuple[str, str], str]:
    """VENDOR/FROM=TO, once per model moved; a model moves within its vendor."""
    out = {}
    for value in values:
        source, _, target = value.partition("=")
        vendor, _, model = source.partition("/")
        if not (vendor and model and target.strip()):
            raise ValueError(f"{value!r}: use VENDOR/FROM=TO, such as openai/gpt-4o=gpt-4o-mini")
        out[(vendor.strip(), model.strip())] = target.strip()
    return out
