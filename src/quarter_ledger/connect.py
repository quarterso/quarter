"""Connect mode: book spend Quarter never saw, read from where the company already has it.

A source (sources.py) gives Observed records. Each becomes a charge in the same hash-chained
entries table, with call_id "obs:<source>:<id>", dated when the spend happened. So status,
report, export, margin, summary, agents list and the audit pack count it like any charge,
while no gateway decision does (store.GATEWAY_ONLY): observed spend never blocks, never
pauses and never counts as approved. It has no hold and no approval behind it.

Pulling is read-only toward the source, idempotent and resumable:
- Each record is kept in `observed` by (source, id), so a record already booked is skipped.
- A bucket (a day's total from a vendor) can still change at the source. Re-reading one
  books only the difference, as a further charge on the same call_id, never an edit.
- Each page is written in its own transaction, so a pull cut short keeps what it booked,
  and the next run picks up from the source's cursor.

Vendor totals (the OpenAI and Anthropic cost APIs, OpenRouter activity) cover spend the
detailed sources (LiteLLM, Cloudflare, OpenRouter generations, CSV) may have booked already.
They book only the remainder: the vendor's figure for that day and model less what Quarter
already holds for it, never below zero. Re-reading them after a detailed source corrects it.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import period as periods
from .insights import forecast
from .money import to_micros, usd_display
from .policy import Cap
from .reconcile import same_model
from .store import Ledger, month_range, now

SCHEMA = """
create table if not exists observed (
  source text not null,
  source_id text not null,
  call_id text not null,
  at text not null,
  amount integer not null,
  pulled_at text not null,
  primary key (source, source_id)
);
create table if not exists connect_cursor (
  source text primary key,
  through text not null,
  at text not null
);
"""
PREFIX = "obs:"
FINGERPRINT = json.dumps({"tools": {}, "observed": True})  # forensics.why reads "tools"
SPIKE_LOOKBACK = timedelta(days=3)  # older days are history, not news
BASELINE_DAYS = 7


@dataclass
class Observed:
    """One piece of spend a source reports. amount is micro-dollars. A bucket is a total the
    source can still revise (a day of a vendor's costs); anything else is final."""

    id: str
    at: str  # ISO time, UTC
    vendor: str
    amount: int
    model: str | None = None
    agent: str | None = None
    task: str | None = None
    customer: str | None = None
    input: int | None = None
    output: int | None = None
    bucket: bool = False


@dataclass
class Pulled:
    source: str
    booked: int = 0  # new records with spend
    revised: int = 0  # buckets the source changed since the last pull
    unchanged: int = 0  # already booked, skipped
    changed_final: int = 0  # final records the source now reports differently: kept as first booked
    amount: int = 0  # micro-dollars added to the ledger by this pull
    days: set = field(default_factory=set)


def setup(ledger: Ledger) -> None:
    ledger.db.executescript(SCHEMA)
    periods.setup(ledger)


def call_id(source: str, source_id: str) -> str:
    return f"{PREFIX}{source}:{source_id}"


def is_observed(call: str) -> bool:
    return call.startswith(PREFIX)


def book(ledger: Ledger, source: str, records: Iterable[Observed], pulled: Pulled | None = None) -> Pulled:
    """Book one page of records in one transaction. Safe to repeat."""
    setup(ledger)
    pulled = pulled or Pulled(source)
    with ledger._write():
        for r in records:
            if r.amount < 0 and not r.bucket:
                raise ValueError(f"{source} record {r.id} has a negative amount")
            cid = call_id(source, r.id)
            row = ledger.db.execute(
                "select amount from observed where source = ? and source_id = ?", (source, r.id)
            ).fetchone()
            if row is None:
                ledger.db.execute(
                    "insert into observed (source, source_id, call_id, at, amount, pulled_at)"
                    " values (?, ?, ?, ?, ?, ?)", (source, r.id, cid, r.at, r.amount, now()))
                delta = r.amount
                pulled.booked += bool(delta)
                _call_row(ledger, cid, r)
            elif row[0] == r.amount:
                pulled.unchanged += 1
                continue
            elif not r.bucket:
                pulled.changed_final += 1
                continue
            else:
                ledger.db.execute("update observed set amount = ?, pulled_at = ? where source = ? and source_id = ?",
                                  (r.amount, now(), source, r.id))
                delta = r.amount - row[0]
                pulled.revised += 1
            if delta:
                _charge(ledger, cid, r, delta)
                pulled.amount += delta
                pulled.days.add(r.at[:10])
    return pulled


def _charge(ledger: Ledger, cid: str, r: Observed, amount: int) -> None:
    """A charge dated when the spend happened, in the chain like every entry. Spend in a
    locked month is booked in the first open month after it, as a restatement (period.py)."""
    at = periods.booking_time(ledger, r.at)
    row = (cid, "charge", at, r.agent, r.task, r.customer, r.vendor, r.model, amount)
    ledger.db.execute(
        "insert into entries (call_id, kind, at, agent, task, customer, vendor, model, amount, hash)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row, ledger._next_hash("entries", row)))
    if at != r.at:
        periods.note_restated(ledger, cid, r.at, at, amount)


def _call_row(ledger: Ledger, cid: str, r: Observed) -> None:
    """A record with a task and tokens also goes in calls, so `why <task>` can show it. The
    prompt was never seen, so there are no parts and no cache breaks. Its request class keeps
    it out of every lane the gateway compares calls in."""
    if not r.task or r.input is None or r.bucket:
        return
    ledger.db.execute(
        "insert or ignore into calls (call_id, at, agent, task, request_class, vendor, model, route,"
        " input, output, cache_read, cache_write, cost, list_cost, tools_cost, system_cost,"
        " history_cost, output_cost, fingerprint, break_cost)"
        " values (?, ?, ?, ?, 'observed', ?, ?, 'api', ?, ?, 0, 0, ?, ?, 0, 0, 0, 0, ?, 0)",
        (cid, r.at, r.agent, r.task, r.vendor, r.model, r.input, r.output or 0, r.amount, r.amount,
         FINGERPRINT))


def remainder(
    ledger: Ledger, source: str, vendor: str, groups: dict[tuple[str, str], dict[str | None, int]],
    owner: Callable[[str | None], str | None] = lambda part: None,
) -> list[Observed]:
    """Vendor totals as bucket records that book only what nothing else in the ledger holds.

    groups: {(day, model): {part: micro-dollars}}, where a part is a project, workspace or
    key (None for no part). For each day and model, what the ledger already holds for that
    vendor from the gateway and every other source is taken off the total, and the rest is
    split over the parts in their own proportions. owner names the agent a part belongs to."""
    setup(ledger)
    out = []
    for (day, model), parts in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        span = (vendor, start.isoformat(), (start + timedelta(days=1)).isoformat(),
                len(call_id(source, "")), call_id(source, ""))
        held = sum(
            amount for m, amount in ledger.db.execute(
                "select coalesce(model, ''), sum(amount) from entries where kind = 'charge'"
                " and lower(vendor) = lower(?) and at >= ? and at < ? and substr(call_id, 1, ?) != ?"
                " group by model", span,
            ) if same_model(m) == same_model(model)
        ) + sum(  # spend for that day booked in a later month, because the day's month was locked
            amount for m, amount in ledger.db.execute(
                "select coalesce(e.model, ''), sum(r.amount) from restatements r join"
                " (select distinct call_id, vendor, model from entries) e on e.call_id = r.call_id"
                " where lower(e.vendor) = lower(?) and r.spent_at >= ? and r.spent_at < ?"
                " and substr(r.call_id, 1, ?) != ? group by e.model", span,
            ) if same_model(m) == same_model(model)
        )
        total = sum(parts.values())
        left = max(total - held, 0)
        names = sorted(parts, key=lambda p: (p is None, p or ""))
        given = 0
        for n, part in enumerate(names):
            share = left - given if n == len(names) - 1 else left * parts[part] // max(total, 1)
            given += share
            out.append(Observed(f"{day}|{model}|{part or ''}", f"{day}T00:00:00+00:00", vendor, share,
                                model=model, agent=owner(part), bucket=True))
    return out


# Where a pull starts and stops

def cursor(ledger: Ledger, source: str) -> str | None:
    setup(ledger)
    row = ledger.db.execute("select through from connect_cursor where source = ?", (source,)).fetchone()
    return row[0] if row else None


def advance(ledger: Ledger, source: str, through: datetime) -> None:
    """Remember the pull reached `through`; never moves back."""
    with ledger._write():
        old = ledger.db.execute("select through from connect_cursor where source = ?", (source,)).fetchone()
        if old is None or old[0] < through.isoformat():
            ledger.db.execute("insert or replace into connect_cursor (source, through, at) values (?, ?, ?)",
                              (source, through.isoformat(), now()))


def period(ledger: Ledger, source: str, month: str | None, since: str | None, until: str | None,
           at: datetime | None = None, overlap: timedelta = timedelta(days=1)) -> tuple[datetime, datetime]:
    """The span to pull: a month, or --since to --until (default now), or from where the last
    pull stopped less `overlap` (late records arrive), or the start of this month."""
    at = at or datetime.now(UTC)
    if month:
        start, end = month_range(month)
        return start, min(end, at)
    if since:
        start = _when(since)
    elif (last := cursor(ledger, source)) is not None:
        start = datetime.fromisoformat(last) - overlap
    else:
        start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = _when(until) if until else at
    if end <= start:
        raise ValueError(f"nothing to pull: {start:%Y-%m-%d %H:%M} is not before {end:%Y-%m-%d %H:%M}")
    return start, end


def _when(text: str) -> datetime:
    value = datetime.fromisoformat(text.strip())
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def utc_iso(value) -> str:
    """A source's timestamp as ISO UTC: ISO text with or without a zone, or Unix seconds."""
    if isinstance(value, int | float) or (isinstance(value, str) and re.fullmatch(r"\d{9,11}(\.\d+)?", value)):
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    when = datetime.fromisoformat(text)
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def usd(value) -> int:
    """A dollar amount a source reports, as micro-dollars; missing is zero."""
    if value in (None, ""):
        return 0
    micros = to_micros(str(value).replace("$", "").replace(",", "").strip())
    return micros


def stable_id(*values) -> str:
    return hashlib.sha256("\x1f".join("" if v is None else str(v) for v in values).encode()).hexdigest()[:24]


# Who spent it: one small path language for every source's tags

def pick(row: dict, specs: Iterable[str]) -> str | None:
    """The first spec that finds a value. A spec is a dotted path ("metadata.customer_id"),
    and may end in [prefix] to take a list entry starting with prefix, without it
    ("request_tags[customer:]" finds "customer:acme" and gives "acme"). Values that are JSON
    text (Cloudflare's metadata) are read as JSON."""
    for spec in specs:
        value = _path(row, spec)
        if value not in (None, "", [], {}):
            return str(value)
    return None


def _path(row, spec: str):
    prefix = None
    if spec.endswith("]") and "[" in spec:
        spec, prefix = spec[:-1].split("[", 1)
    value = row
    for part in spec.split("."):
        if isinstance(value, str) and value[:1] in "{[":
            try:
                value = json.loads(value)
            except ValueError:
                return None
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if prefix is None:
        return value
    if isinstance(value, str) and value[:1] == "[":
        try:
            value = json.loads(value)
        except ValueError:
            return None
    for item in value if isinstance(value, list) else ():
        if isinstance(item, str) and item.startswith(prefix):
            return item[len(prefix):]
    return None


# Alerts: spend that jumped, and caps observed spend has reached

@dataclass(frozen=True)
class Spike:
    multiple: float = 3
    floor: int = to_micros(20)


def spikes(ledger: Ledger, days: Iterable[str], rule: Spike, at: datetime | None = None) -> list[dict]:
    """Agents and customers whose observed spend on one of these days passed the floor and
    `multiple` times their daily average over the 7 days before. Days older than 3 days are
    left out, so backfilling a month raises no old news. A new agent or customer has no
    average, so only the floor counts for it."""
    at = at or datetime.now(UTC)
    out = []
    for day in sorted(days):
        start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        if start < (at - SPIKE_LOOKBACK).replace(hour=0, minute=0, second=0, microsecond=0) or start > at:
            continue
        span = (start.isoformat(), (start + timedelta(days=1)).isoformat())
        before = ((start - timedelta(days=BASELINE_DAYS)).isoformat(), start.isoformat())
        for scope in ("agent", "customer"):
            sql = (f"select {scope}, sum(amount) from entries where kind = 'charge' and {scope} is not null"
                   " and substr(call_id, 1, 4) = 'obs:' and at >= ? and at < ? group by " + scope)
            usual = {k: v / BASELINE_DAYS for k, v in ledger.db.execute(sql, before)}
            for key, spent in ledger.db.execute(sql, span):
                base = usual.get(key, 0)
                if spent > rule.floor and spent > rule.multiple * base:
                    out.append({"scope": scope, "key": key, "day": day, "spent": spent, "usual": round(base)})
    return out


def alert(ledger: Ledger, caps: list[Cap], days: Iterable[str], rule: Spike | None,
          notify: Callable[[str, str], None], at: datetime | None = None) -> list[str]:
    """Send each spike and each monthly cap reached or on course to be, once: the audit log
    remembers what was sent (spike_alert, budget_alert), so a re-run sends nothing twice.
    Alerts only: observed spend never blocks."""
    at = at or datetime.now(UTC)
    sent_before = {d for (d,) in ledger.db.execute(
        "select detail from audit where action in ('spike_alert', 'budget_alert')")}
    texts = []
    for s in spikes(ledger, days, rule, at) if rule else ():
        key = f"{s['scope']} {s['key']} {s['day']}"
        if key in sent_before:
            continue
        usual = f"a usual ${usd_display(s['usual'])} a day" if s["usual"] else "no spend in the 7 days before"
        text = (f"Spend jump: {s['scope']} {s['key']} spent ${usd_display(s['spent'])}"
                f" on {s['day']} (observed), against {usual}.")
        texts.append((key, "spike_alert", text))
    today = at.strftime("%Y-%m-%d")
    for w in forecast(ledger, caps, at)["cap_warnings"]:
        key = f"{w['cap']} {today}"  # the gateway's budget alert key, so both send once a day
        if key in sent_before:
            continue
        when = "has been reached" if w["reached"] == "already" else f"will be reached on {w['reached']}"
        text = (f"Budget: the ${usd_display(w['limit'])} monthly cap on {w['cap']} {when}, counting"
                f" observed spend (month end ${usd_display(w['projected'])}). Observed spend is not blocked.")
        texts.append((key, "budget_alert", text))
    for key, action, text in texts:
        ledger.audit(action, "connect", key)
        notify(text, key)
    return [text for _, _, text in texts]
