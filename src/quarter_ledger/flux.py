"""Why AI spend moved since last month, what vendors change next, and spend that never sleeps.

The same method as the free close at quarterfi.com/close, run on the ledger instead of a file.

Per vendor, the change between two months splits into use, unit price, models that are new and
models no longer used, and the parts add up exactly to the change. Use is measured as the
month's tokens at today's list prices (prices.json and the customer's own), so a change in what
a token actually cost, from a vendor price change, caching, a tier or a discount, lands in unit
price. A model is split only when every charge for it came through the gateway with its tokens;
spend booked from a vendor's cost API has no tokens, so it is shown as not split.

A unit-price change is matched to dated vendor changes in changes.json, each with its source.
The same list prices the changes due in the next three months on this month's use. Steady
background use is a model that cost about the same every day, weekends as much as weekdays:
the shape of polling, schedules, retries and heartbeats rather than people.
"""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .money import usd_display
from .pricing import PriceBook, UnknownPrice
from .store import Ledger, month_range

CHANGES_FILE = Path(__file__).with_name("changes.json")
NAMES = {"openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google", "google": "Google Cloud", "aws": "AWS",
         "azure": "Azure"}
# Gemini models bill the same through the Gemini API and Google Cloud (Vertex AI)
SAME = {"google": "gemini"}


def _applies(change: dict, vendor: str) -> bool:
    return change["vendor"] == SAME.get(vendor, vendor)
TOKENIZER = re.compile(r"claude-(opus|sonnet|haiku)-(4-[7-9]|[5-9])|claude-(fable|mythos)")
ANTHROPIC_PRICING = "https://platform.claude.com/docs/en/about-claude/pricing"

# Steady background use: at least this many days, a daily spread of at most this share of the
# mean, weekends at least this share of weekdays, and a floor worth a controller's time.
MIN_DAYS, MAX_SPREAD, MIN_WEEKEND, MIN_FLOOR = 14, 0.25, 0.8, 10_000_000
# Tokens priced on the gateway must cover the model's charges this closely for a split.
COVERED = 0.01


@dataclass
class Item:
    vendor: str
    model: str
    micros: int = 0
    qty: int | None = None  # micros at today's list prices; None when it cannot be split
    days: dict[str, int] = field(default_factory=dict)


@dataclass
class Driver:
    kind: str  # volume, rate, new, stopped, unsplit
    micros: int
    text: str
    sources: list[str] = field(default_factory=list)


@dataclass
class Bridge:
    vendor: str
    before: int
    now: int
    drivers: list[Driver]
    needs_note: bool
    summary: str


def changes(path: Path = CHANGES_FILE) -> tuple[str, list[dict]]:
    """The dated vendor changes and the day they were last checked."""
    raw = json.loads(path.read_text())
    out = []
    for c in raw["changes"]:
        out.append({**c, "pattern": re.compile(c["match"])})
    return raw["checked"], out


def normalise(item: str) -> str:
    return re.sub(r"[\s_.]+", "-", item.lower())


def factor_for(factor, item: str) -> float | None:
    if factor is None or isinstance(factor, (int, float)):
        return factor
    n = normalise(item)
    if re.search(r"output|completion", n):
        return factor["output"]
    if re.search(r"input|prompt", n):
        return factor["input"]
    return None


def _usd(micros: int) -> str:
    return "$" + usd_display(abs(micros))


def _signed(micros: int) -> str:
    return ("-" if micros < 0 else "+") + _usd(micros)


def _show(item: str) -> str:
    return re.sub(r",\s*", " ", item)


def _list(names: list[str]) -> str:
    return f"{', '.join(names[:3])} and {len(names) - 3} more" if len(names) > 3 else ", ".join(names)


def _month_name(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def add_months(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    i = y * 12 + m - 1 + n
    return f"{i // 12}-{i % 12 + 1:02d}"


def _last_day(month: str) -> str:
    return f"{month_range(month)[1] - timedelta(days=1):%Y-%m-%d}"


def items(ledger: Ledger, month: str, book: PriceBook) -> list[Item]:
    """Each vendor and model's charges in the month, by UTC day, with its tokens priced at
    today's list prices where every charge has them."""
    start, end = month_range(month)
    span = (start.isoformat(), end.isoformat())
    found: dict[tuple[str, str], Item] = {}
    for vendor, model, day, amount in ledger.db.execute(
        "select vendor, coalesce(model, '(no model)'), substr(at, 9, 2), sum(amount) from entries"
        " where kind = 'charge' and at >= ? and at < ? group by 1, 2, 3", span,
    ):
        it = found.setdefault((vendor, model), Item(vendor, model))
        it.micros += amount
        it.days[day] = it.days.get(day, 0) + amount
    priced: dict[tuple[str, str], list[int]] = {}
    for vendor, model, inp, out, read, write, cost in ledger.db.execute(
        "select vendor, coalesce(model, '(no model)'), input, output, cache_read, cache_write, cost"
        " from calls where route = 'api' and at >= ? and at < ?", span,
    ):
        sums = priced.setdefault((vendor, model), [0, 0])
        try:
            sums[0] += book.price(vendor, model).cost(inp, out, read, write)
        except UnknownPrice:
            sums[0] = -1
        if sums[0] >= 0:
            sums[1] += cost
    for key, (list_cost, cost) in priced.items():
        it = found.get(key)
        if it and list_cost > 0 and abs(cost - it.micros) <= COVERED * max(it.micros, 1):
            it.qty = list_cost
    return [it for it in found.values() if it.micros]


def _split(before: dict[str, Item], now: dict[str, Item]):
    volume = unsplit = 0.0
    rates, grew, fresh, gone, no_qty = [], [], [], [], []
    for model in sorted(before.keys() | now.keys()):
        a, b = before.get(model), now.get(model)
        if not a:
            fresh.append((model, b.micros))
            continue
        if not b:
            gone.append((model, -a.micros))
            continue
        if a.qty and b.qty:
            r0, r1 = a.micros / a.qty, b.micros / b.qty
            v = (b.qty - a.qty) * r0
            volume += v
            grew.append((model, v))
            r = b.micros - a.micros - v
            if abs(r) >= 0.5:
                rates.append((model, r, r1 / r0 - 1))
        else:
            unsplit += b.micros - a.micros
            no_qty.append(model)
    return volume, unsplit, rates, grew, fresh, gone, no_qty


def _vendor_bridge(vendor: str, before: list[Item], now: list[Item], month: str, listed: list[dict],
                   threshold: tuple[int, float]) -> Bridge:
    was, then = _month_name(add_months(month, -1)), _month_name(month)
    p, c = {i.model: i for i in before}, {i.model: i for i in now}
    prev, cur = sum(i.micros for i in before), sum(i.micros for i in now)
    volume, unsplit, rates, grew, fresh, gone, no_qty = _split(p, c)
    drivers: list[Driver] = []
    if round(volume):
        top = [m for m, _ in sorted(grew, key=lambda g: -abs(g[1]))][:3]
        drivers.append(Driver("volume", round(volume),
                              f"{'More' if volume > 0 else 'Less'} use of the same models, mostly {_list([_show(m) for m in top])}."))
    rate_total = sum(r for _, r, _ in rates)
    if round(rate_total):
        window = (_last_day(add_months(month, -2)), _last_day(month))
        hits = {}
        for model, _, _ in rates:
            for ch in listed:
                if _applies(ch, vendor) and window[0] < ch["date"] <= window[1] and ch["pattern"].search(normalise(model)):
                    hits[ch["note"]] = ch
        model, _, change = max(rates, key=lambda r: abs(r[1]))
        lead = (f"Unit cost {'rose' if rate_total > 0 else 'fell'}, most on {_show(model)}"
                f" ({'+' if change > 0 else ''}{round(change * 100)}% a unit).")
        text = (f"{lead} Published changes in the period: {' '.join(hits)}" if hits else
                f"{lead} No published price change covers it: check the cache hit rate, the service tier"
                " (Fast, Priority or Flex), the region, and any discount or commitment.")
        drivers.append(Driver("rate", round(rate_total), text, sorted({h["source"] for h in hits.values()})))
    if fresh:
        names = [f"{_show(m)} ({_usd(v)})" for m, v in sorted(fresh, key=lambda f: -f[1])]
        claude = any(TOKENIZER.search(normalise(m)) for m, _ in fresh)
        drivers.append(Driver("new", sum(v for _, v in fresh), f"New in {then}: {_list(names)}."
                              + (" Newer Claude models write about 30% more tokens for the same text, so part of"
                                 " this is price, not use." if claude else ""),
                              [ANTHROPIC_PRICING] if claude else []))
    if gone:
        names = [f"{_show(m)} ({_usd(v)})" for m, v in sorted(gone, key=lambda g: g[1])]
        drivers.append(Driver("stopped", sum(v for _, v in gone), f"Used in {was}, not in {then}: {_list(names)}."))
    if round(unsplit):
        drivers.append(Driver("unsplit", round(unsplit),
                              f"Changed on {_list([_show(m) for m in no_qty])}. Booked from the vendor's cost report"
                              " without tokens, so use and price cannot be split."))
    gap = cur - prev - sum(d.micros for d in drivers)
    if gap and drivers:
        drivers[-1].micros += gap  # rounding each driver to whole micros; keep the bridge exact
    delta = cur - prev
    dollars, percent = threshold
    needs = abs(delta) >= dollars and (prev == 0 or abs(delta / prev) * 100 >= percent)
    name = NAMES.get(vendor, vendor)
    summary = (f"{name} {'rose' if delta >= 0 else 'fell'} {_usd(delta)} ({round(abs(delta) / prev * 100)}%),"
               f" from {_usd(prev)} in {was} to {_usd(cur)} in {then}." if prev else
               f"{name} is new in {then}, at {_usd(cur)}.")
    return Bridge(vendor, prev, cur, drivers, needs, summary)


def bridges(ledger: Ledger, month: str, book: PriceBook, listed: list[dict],
            threshold: tuple[int, float] = (1_000_000_000, 10.0)) -> list[Bridge]:
    now, before = items(ledger, month, book), items(ledger, add_months(month, -1), book)
    vendors = sorted({i.vendor for i in now + before})
    out = [_vendor_bridge(v, [i for i in before if i.vendor == v], [i for i in now if i.vendor == v],
                          month, listed, threshold) for v in vendors]
    return sorted(out, key=lambda b: -abs(b.now - b.before))


def coming(ledger: Ledger, month: str, book: PriceBook, listed: list[dict]) -> list[dict]:
    """Published changes in the three months after `month`, priced on its use."""
    start, stop = _last_day(month), _last_day(add_months(month, 3))
    now = items(ledger, month, book)
    out = []
    for ch in listed:
        if not start < ch["date"] <= stop:
            continue
        hit = [i for i in now if _applies(ch, i.vendor) and ch["pattern"].search(normalise(i.model))]
        if not hit:
            continue
        affected = sum(i.micros for i in hit)
        factors = [factor_for(ch.get("factor"), i.model) for i in hit]
        impact = (round(sum(i.micros * (f - 1) for i, f in zip(hit, factors, strict=True)))
                  if all(f is not None for f in factors) else None)
        priced = (f"{_usd(affected)} of {_month_name(month)} spend is on the models it affects." if impact is None else
                  f"Moved to the replacement at list prices, {_month_name(month)} use would cost about {_signed(impact)} a month."
                  if ch["kind"] == "retire" else f"At {_month_name(month)} use, about {_signed(impact)} a month.")
        out.append({"date": ch["date"], "vendor": ch["vendor"], "affected": affected, "impact": impact,
                    "text": f"{ch['note']} {priced}", "source": ch["source"]})
    return out


def _weekend(month: str, day: str) -> bool:
    return datetime.strptime(f"{month}-{day}", "%Y-%m-%d").replace(tzinfo=UTC).weekday() >= 5


def steady(ledger: Ledger, month: str, book: PriceBook) -> tuple[bool, list[dict]]:
    """(whether the month has enough days to tell, the models that ran at a steady rate every day)."""
    rows = items(ledger, month, book)
    span = sorted({d for i in rows for d in i.days})
    if len(span) < MIN_DAYS:
        return False, []
    out = []
    for i in rows:
        daily = [i.days.get(d, 0) for d in span]
        if min(daily) <= 0:
            continue
        mean = sum(daily) / len(daily)
        if math.sqrt(sum((v - mean) ** 2 for v in daily) / len(daily)) / mean > MAX_SPREAD:
            continue
        ends = [i.days[d] for d in span if _weekend(month, d)]
        weekdays = [i.days[d] for d in span if not _weekend(month, d)]
        if len(ends) >= 2 and len(weekdays) >= 2 and (
                sum(ends) / len(ends) < MIN_WEEKEND * sum(weekdays) / len(weekdays)):
            continue
        floor = min(daily) * len(span)
        if floor < MIN_FLOOR:
            continue
        out.append({"vendor": i.vendor, "model": i.model, "days": len(span), "floor": floor, "total": i.micros,
                    "text": f"{NAMES.get(i.vendor, i.vendor)} {_show(i.model)} cost at least {_usd(min(daily))} on every"
                            f" one of the {len(span)} days, weekends as much as weekdays: {_usd(floor)} of its {_usd(i.micros)}."})
    return True, sorted(out, key=lambda s: -s["floor"])


STEADY_HINT = ("Steady use with no weekly rhythm usually comes from a schedule, a polling loop, retries or a"
               " heartbeat, not from people. It can be right, for example a nightly job. Check that each still"
               " needs to run, and that agents waiting for work are stopped until they get some.")


def report(ledger: Ledger, month: str, book: PriceBook, threshold: tuple[int, float] = (1_000_000_000, 10.0),
           path: Path = CHANGES_FILE) -> dict:
    """Everything above for one month, as one JSON-ready dict."""
    checked, listed = changes(path)
    daily, still = steady(ledger, month, book)
    return {
        "month": month,
        "changes_checked": checked,
        "bridges": [{"vendor": b.vendor, "before": b.before, "now": b.now, "needs_note": b.needs_note,
                     "summary": b.summary, "drivers": [d.__dict__ for d in b.drivers]}
                    for b in bridges(ledger, month, book, listed, threshold)],
        "coming": coming(ledger, month, book, listed),
        "steady": {"daily": daily, "items": still},
    }


def markdown(r: dict) -> str:
    """The report as a page for the close binder."""
    labels = {"volume": "Use", "rate": "Unit price", "new": "New models", "stopped": "No longer used",
              "unsplit": "Not split"}
    out = [f"# Why AI spend moved, {_month_name(r['month'])}", "",
           f"Vendor changes checked {r['changes_checked']}.", ""]
    for b in r["bridges"]:
        out += [f"## {NAMES.get(b['vendor'], b['vendor'])}", "",
                b["summary"] + (" Over the thresholds, so it needs a note." if b["needs_note"] else ""), ""]
        if b["drivers"]:
            out += ["| Driver | Amount | Explanation |", "|---|---:|---|"]
            for d in b["drivers"]:
                links = " ".join(f"[source]({s})" for s in d["sources"])
                out.append(f"| {labels[d['kind']]} | {_signed(d['micros'])} | {d['text']} {links} |".replace("  |", " |"))
            out.append("")
    out += ["## Steady background use", ""]
    if not r["steady"]["daily"]:
        out.append("Fewer than 14 days of spend, so it cannot be seen.")
    elif r["steady"]["items"]:
        out += [f"- {s['text']}" for s in r["steady"]["items"]] + ["", STEADY_HINT]
    else:
        out.append("None: no model ran at a steady rate every day, weekends included.")
    out += ["", "## Published changes in the next three months", ""]
    out += [f"- {c['date']}: {c['text']} [source]({c['source']})" for c in r["coming"]] or [
        "None we know of for the models in use."]
    return "\n".join(out) + "\n"
