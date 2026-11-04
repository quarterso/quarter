"""The sources connect mode reads, each a small reader, and `quarter-ledger connect`.

Every reader only reads (GET), sends its key only to its own host, never follows a redirect
with it, and never prints it (reconcile._get). Field names follow each vendor's published
documentation, read 2026-11-03; README.md says which ones could not be confirmed.

openai, anthropic: the organisation cost APIs reconcile.py already reads, per UTC day, and
  grouped by project or workspace when the map names them. Vendor totals: they book the
  remainder (connect.remainder). Only finished days are read.
litellm: a LiteLLM proxy's GET /spend/logs/v2 (page, page_size up to 1000) with the master
  key, one day at a time, or a CSV of the same rows. Each row is one call.
openrouter: GET /api/v1/activity?date=YYYY-MM-DD with a management key: daily totals per
  model, for the last 30 finished days, per API key with api_key_hash. Vendor totals.
openrouter-gen: GET /api/v1/generation?id=gen-... for the ids in a CSV your app logged,
  with the agent, customer and task it logged beside them. Each id is one call.
cloudflare: GET /client/v4/accounts/{account}/ai-gateway/gateways/{gateway}/logs (page,
  per_page up to 50, start_date and end_date) with an API token. Each log is one call;
  agent and customer come from its metadata (the cf-aig-metadata header).
csv: any CSV with a date and an amount, columns named in the map.
"""

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from . import connect
from .connect import Observed, Pulled, Spike, pick, stable_id, usd, utc_iso
from .money import to_micros, usd_display
from .reconcile import KEY_ENV, _get, fetch_days
from .slack import Slack, escape
from .store import Ledger

SOURCES = ("openai", "anthropic", "litellm", "openrouter", "openrouter-gen", "cloudflare", "csv")
TOTALS = {"openai", "anthropic", "openrouter"}  # vendor totals: book the remainder
MAX_PAGES = 1000
LITELLM_PAGE = 1000
CLOUDFLARE_PAGE = 50
OPENROUTER_DAYS = 30
CHUNK = timedelta(days=7)  # the cost APIs are read a week at a time, each week its own write

DEFAULT_MAP = {
    "litellm": {
        "agent": ["metadata.user_api_key_alias", "user", "metadata.user_api_key_team_alias", "team_id"],
        "customer": ["end_user", "request_tags[customer:]"],
        "task": ["session_id", "request_tags[task:]"],
    },
    "cloudflare": {
        "agent": ["metadata.agent", "metadata.user"],
        "customer": ["metadata.customer", "metadata.customer_id"],
        "task": ["metadata.task", "metadata.session_id"],
    },
    "openrouter-gen": {"agent": ["agent"], "customer": ["customer", "external_user"], "task": ["task", "session_id"]},
}
CSV_COLUMNS = {
    "id": ["id", "request_id", "call_id"],
    "at": ["date", "timestamp", "time", "at", "created_at", "start_time"],
    "amount": ["amount_usd", "cost_usd", "amount", "cost", "spend"],
    "vendor": ["vendor", "provider"],
    "model": ["model"],
    "agent": ["agent"],
    "customer": ["customer", "customer_id"],
    "task": ["task"],
    "input": ["input_tokens", "prompt_tokens"],
    "output": ["output_tokens", "completion_tokens"],
}


def day_start(when: datetime) -> datetime:
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def days(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    day = day_start(start)
    while day < end:
        yield max(day, start), min(day + timedelta(days=1), end)
        day += timedelta(days=1)


def _inside(record: Observed, start: datetime, end: datetime) -> bool:
    return start.isoformat() <= record.at < end.isoformat()


def _int(value) -> int | None:
    try:
        return None if value in (None, "") else int(float(value))
    except (TypeError, ValueError):
        return None


def _rows(reply, where: str) -> list[dict]:
    rows = reply.get("data", reply.get("result")) if isinstance(reply, dict) else reply
    if not isinstance(rows, list):
        raise ValueError(f"{where} answered without a list of rows")  # noqa: TRY004, a bad reply
    return [r for r in rows if isinstance(r, dict)]


# Vendor cost APIs

def admin_pages(ledger: Ledger, vendor: str, key: str, start: datetime, end: datetime, mapping: dict,
                base: str | None, at: datetime) -> Iterator[tuple[list[Observed], datetime]]:
    """A week at a time, finished days only. A project or workspace the map names is that
    agent's; the map's key is "projects" for OpenAI and "workspaces" for Anthropic."""
    owners = mapping.get("projects" if vendor == "openai" else "workspaces") or {}
    last = day_start(end) + timedelta(days=1) if end > day_start(end) else end  # the day end falls in
    start, end = day_start(start), min(last, day_start(at))  # today is not finished
    while start < end:
        stop = min(start + CHUNK, end)
        groups: dict[tuple[str, str], dict] = {}
        for (day, item, group), dollars in fetch_days(vendor, key, start, stop, base, bool(owners)).items():
            parts = groups.setdefault((day, item), {})
            parts[group] = parts.get(group, Decimal(0)) + dollars
        groups = {k: {g: to_micros(d) for g, d in v.items()} for k, v in groups.items()}
        yield connect.remainder(ledger, vendor, vendor, groups, owners.get), stop
        start = stop


# LiteLLM

def litellm_row(row: dict, mapping: dict) -> Observed:
    model = row.get("model") or row.get("model_group") or None
    vendor = row.get("custom_llm_provider") or (model.split("/", 1)[0] if model and "/" in model else "litellm")
    when = row.get("startTime") or row.get("start_time") or row.get("endTime")
    if not when:
        raise ValueError(f"litellm row {row.get('request_id')!r} has no startTime")
    return Observed(
        str(row.get("request_id") or stable_id(json.dumps(row, sort_keys=True, default=str))),
        utc_iso(when), vendor, usd(row.get("spend")), model=model,
        agent=pick(row, mapping["agent"]), task=pick(row, mapping["task"]),
        customer=pick(row, mapping["customer"]),
        input=_int(row.get("prompt_tokens")), output=_int(row.get("completion_tokens")),
    )


def litellm_pages(base: str, key: str, start: datetime, end: datetime, mapping: dict
                  ) -> Iterator[tuple[list[Observed], datetime]]:
    for day, stop in days(start, end):
        for page in range(1, MAX_PAGES + 1):
            # end_date a day on, in case LiteLLM reads it as exclusive; rows are kept by startTime
            query = {"start_date": f"{day:%Y-%m-%d}", "end_date": f"{day_start(day) + timedelta(days=1):%Y-%m-%d}",
                     "page": page, "page_size": LITELLM_PAGE}
            reply = _get(f"{base.rstrip('/')}/spend/logs/v2?{urllib.parse.urlencode(query)}",
                         {"authorization": f"Bearer {key}"}, key)
            rows = _rows(reply, "litellm")
            records = [r for r in (litellm_row(row, mapping) for row in rows) if _inside(r, day, stop)]
            last = (not rows or len(rows) < LITELLM_PAGE
                    or (isinstance(reply, dict) and page >= int(reply.get("total_pages") or page + 1)))
            yield records, stop if last else day
            if last:
                break
        else:
            raise ValueError(f"litellm had more than {MAX_PAGES} pages on {day:%Y-%m-%d}")


def read_csv(text: str) -> list[dict]:
    return list(csv.DictReader(StringIO(text.lstrip("﻿"))))


# OpenRouter

def openrouter_pages(ledger: Ledger, base: str, key: str, start: datetime, end: datetime, mapping: dict,
                     at: datetime) -> Iterator[tuple[list[Observed], datetime]]:
    """One finished day at a time, in the last 30. With "keys" in the map ({"sha256 of the
    key": "agent"}), each key's day is read too and its share is that agent's."""
    keys = mapping.get("keys") or {}
    oldest = day_start(at) - timedelta(days=OPENROUTER_DAYS)
    for day, stop in days(max(start, oldest), min(end, day_start(at))):
        if stop - day < timedelta(days=1):  # a part day: read it when it has finished
            continue

        def read(extra: dict, day=day) -> dict[str, int]:
            query = urllib.parse.urlencode({"date": f"{day:%Y-%m-%d}"} | extra)
            out: dict[str, Decimal] = {}
            for row in _rows(_get(f"{base.rstrip('/')}/api/v1/activity?{query}",
                                  {"authorization": f"Bearer {key}"}, key), "openrouter"):
                if row.get("date", f"{day:%Y-%m-%d}") != f"{day:%Y-%m-%d}":
                    continue
                model = row.get("model") or "(no model)"
                out[model] = out.get(model, Decimal(0)) + Decimal(str(row.get("usage") or 0))
            return {m: to_micros(d) for m, d in out.items()}

        total = read({})
        groups = {(f"{day:%Y-%m-%d}", m): {None: amount} for m, amount in total.items()}
        for key_hash in sorted(keys):
            for model, amount in read({"api_key_hash": key_hash}).items():
                parts = groups.setdefault((f"{day:%Y-%m-%d}", model), {None: amount})
                parts[key_hash] = amount
                parts[None] = max(parts[None] - amount, 0)
        yield connect.remainder(ledger, "openrouter", "openrouter", groups, keys.get), stop


def generation_pages(ledger: Ledger, base: str, key: str, ids: list[dict], mapping: dict
                     ) -> Iterator[tuple[list[Observed], None]]:
    """Each id not booked yet, 50 to a write. The CSV's own agent, customer and task win
    over what OpenRouter returns (external_user, session_id)."""
    connect.setup(ledger)
    booked = {r[0] for r in ledger.db.execute("select source_id from observed where source = 'openrouter-gen'")}
    todo = [row for row in ids if (row.get("id") or "").strip() and row["id"].strip() not in booked]
    for n in range(0, len(todo), 50):
        records = []
        for row in todo[n:n + 50]:
            gen_id = row["id"].strip()
            data = _get(f"{base.rstrip('/')}/api/v1/generation?{urllib.parse.urlencode({'id': gen_id})}",
                        {"authorization": f"Bearer {key}"}, key)
            data = data.get("data") if isinstance(data, dict) else None
            if not isinstance(data, dict):
                raise ValueError(f"openrouter answered {gen_id} without data")  # noqa: TRY004, a bad reply
            merged = {**data, **{k: v for k, v in row.items() if v}}
            records.append(Observed(
                gen_id, utc_iso(data["created_at"]), "openrouter", usd(data.get("total_cost")),
                model=data.get("model"), agent=pick(merged, mapping["agent"]),
                customer=pick(merged, mapping["customer"]), task=pick(merged, mapping["task"]),
                input=_int(data.get("tokens_prompt")), output=_int(data.get("tokens_completion")),
            ))
        yield records, None


# Cloudflare AI Gateway

def cloudflare_row(row: dict, mapping: dict) -> Observed:
    return Observed(
        str(row["id"]), utc_iso(row["created_at"]), row.get("provider") or "cloudflare",
        usd(row.get("cost")), model=row.get("model"),
        agent=pick(row, mapping["agent"]), task=pick(row, mapping["task"]),
        customer=pick(row, mapping["customer"]),
        input=_int(row.get("tokens_in")), output=_int(row.get("tokens_out")),
    )


def cloudflare_pages(base: str, token: str, account: str, gateway: str, start: datetime, end: datetime,
                     mapping: dict) -> Iterator[tuple[list[Observed], datetime]]:
    path = (f"{base.rstrip('/')}/client/v4/accounts/{urllib.parse.quote(account, safe='')}"
            f"/ai-gateway/gateways/{urllib.parse.quote(gateway, safe='')}/logs")
    for day, stop in days(start, end):
        for page in range(1, MAX_PAGES + 1):
            query = {"start_date": day.isoformat().replace("+00:00", "Z"),
                     "end_date": stop.isoformat().replace("+00:00", "Z"), "page": page,
                     "per_page": CLOUDFLARE_PAGE, "order_by": "created_at", "order_by_direction": "asc"}
            reply = _get(f"{path}?{urllib.parse.urlencode(query)}", {"authorization": f"Bearer {token}"}, token)
            if isinstance(reply, dict) and reply.get("success") is False:
                raise ValueError(f"cloudflare answered success false: {str(reply.get('errors'))[:200]}")
            rows = _rows(reply, "cloudflare")
            records = [r for r in (cloudflare_row(row, mapping) for row in rows) if _inside(r, day, stop)]
            total = (reply.get("result_info") or {}).get("total_count") if isinstance(reply, dict) else None
            last = not rows or len(rows) < CLOUDFLARE_PAGE or (total is not None and page * CLOUDFLARE_PAGE >= total)
            yield records, stop if last else day
            if last:
                break
        else:
            raise ValueError(f"cloudflare had more than {MAX_PAGES} pages on {day:%Y-%m-%d}")


# Any CSV

def csv_records(text: str, mapping: dict, vendor: str | None) -> list[Observed]:
    """Columns are found by the names in CSV_COLUMNS, or the map's "columns" ({"amount":
    "Cost ($)"}). A date and an amount are needed, and a vendor column or --vendor. With no
    id column, a row's id is a hash of its values and how many identical rows came before,
    so importing the same file twice books it once."""
    rows = read_csv(text)
    if not rows:
        return []
    fields = {f.strip().lower(): f for f in rows[0]}
    named = {k.lower(): v for k, v in (mapping.get("columns") or {}).items()}
    columns = {}
    for name, guesses in CSV_COLUMNS.items():
        given = named.get(name)
        if given is not None and given.strip().lower() not in fields:
            raise ValueError(f"the map names column {given!r} for {name}, and the file has none")
        columns[name] = fields.get((given or "").strip().lower()) or next(
            (fields[g] for g in guesses if g in fields), None)
    if columns["at"] is None or columns["amount"] is None:
        raise ValueError(f"need a date column ({', '.join(CSV_COLUMNS['at'])}) and an amount column"
                         f" ({', '.join(CSV_COLUMNS['amount'])}), or name them in the map's \"columns\"")
    if columns["vendor"] is None and not vendor:
        raise ValueError("need a vendor column, or --vendor")
    out, seen = [], {}
    for line, row in enumerate(rows, start=2):
        def get(name, row=row):
            return (row.get(columns[name]) or "").strip() if columns[name] else ""
        if not get("at") and not get("amount"):
            continue
        try:
            when, amount = utc_iso(get("at")), usd(get("amount"))
        except (ValueError, ArithmeticError):
            raise ValueError(f"line {line}: {get('at')!r} or {get('amount')!r} is not a date or amount") from None
        if amount < 0:
            raise ValueError(f"line {line}: a negative amount; record refunds and credits apart")
        rid = get("id")
        if not rid:
            values = tuple(row.get(f) for f in rows[0])
            seen[values] = seen.get(values, 0) + 1
            rid = stable_id(*values, seen[values])
        out.append(Observed(rid, when, get("vendor") or vendor, amount, model=get("model") or None,
                            agent=get("agent") or None, customer=get("customer") or None,
                            task=get("task") or None, input=_int(get("input")), output=_int(get("output"))))
    return out


# Running a pull

def load_map(source: str, raw: str | None) -> dict:
    given = {}
    if raw:
        text = Path(raw).read_text() if not raw.lstrip().startswith("{") else raw
        given = json.loads(text)
        if not isinstance(given, dict):
            raise ValueError("the map is a JSON object")
    out = {k: list(v) for k, v in DEFAULT_MAP.get(source, {"agent": [], "customer": [], "task": []}).items()}
    for k, v in given.items():
        out[k] = [v] if k in ("agent", "customer", "task") and isinstance(v, str) else v
    return out


def need(env, name: str, what: str) -> str:
    value = env.get(name)
    if not value:
        raise ValueError(f"set {name} to {what}")
    return value


def pull(ledger: Ledger, source: str, start: datetime, end: datetime, env, mapping: dict,
         file: str | None = None, vendor: str | None = None, name: str | None = None,
         at: datetime | None = None, explicit: bool = True) -> Pulled:
    """Read the source for [start, end) and book it, a page per write. The cursor moves only
    as far as every page before it was booked, so a pull cut short resumes where it stopped."""
    at = at or datetime.now(UTC)
    label = name or source
    pulled = Pulled(label)
    pages: Iterator[tuple[list[Observed], datetime | None]]
    if source in ("openai", "anthropic"):
        key = need(env, KEY_ENV[source], f"your {source} admin key")
        pages = admin_pages(ledger, source, key, start, end, mapping,
                            env.get(f"QUARTER_{source.upper()}_ADMIN_URL"), at)
    elif source == "litellm" and file:
        records = [litellm_row(r, mapping) for r in _file_rows(file)]
        pages = iter([([r for r in records if not explicit or _inside(r, start, end)], None)])
    elif source == "litellm":
        pages = litellm_pages(need(env, "QUARTER_LITELLM_URL", "your LiteLLM proxy's address"),
                              need(env, "QUARTER_LITELLM_KEY", "a LiteLLM key that may read spend logs"),
                              start, end, mapping)
    elif source == "openrouter":
        pages = openrouter_pages(ledger, env.get("QUARTER_OPENROUTER_URL") or "https://openrouter.ai",
                                 need(env, "QUARTER_OPENROUTER_MANAGEMENT_KEY", "an OpenRouter management key"),
                                 start, end, mapping, at)
    elif source == "openrouter-gen":
        if not file:
            raise ValueError("openrouter-gen needs --file: a CSV with an id column of gen-... ids")
        key = env.get("QUARTER_OPENROUTER_KEY") or need(env, "OPENROUTER_API_KEY", "an OpenRouter API key")
        pages = generation_pages(ledger, env.get("QUARTER_OPENROUTER_URL") or "https://openrouter.ai", key,
                                 _file_rows(file), mapping)
    elif source == "cloudflare":
        pages = cloudflare_pages(env.get("QUARTER_CLOUDFLARE_URL") or "https://api.cloudflare.com",
                                 need(env, "QUARTER_CLOUDFLARE_TOKEN", "a Cloudflare API token with AI Gateway Read"),
                                 need(env, "QUARTER_CLOUDFLARE_ACCOUNT", "your Cloudflare account id"),
                                 need(env, "QUARTER_CLOUDFLARE_GATEWAY", "the AI Gateway's id"),
                                 start, end, mapping)
    elif source == "csv":
        if not file:
            raise ValueError("csv needs --file")
        records = csv_records(Path(file).read_text(), mapping, vendor)
        pages = iter([([r for r in records if not explicit or _inside(r, start, end)], None)])
    else:
        raise ValueError(f"unknown source {source}; one of {', '.join(SOURCES)}")
    for records, through in pages:
        connect.book(ledger, label, records, pulled)
        if through is not None:
            connect.advance(ledger, label, through)
    ledger.audit("observed_pull", "connect", json.dumps({
        "source": label, "from": start.isoformat(), "to": end.isoformat(), "booked": pulled.booked,
        "revised": pulled.revised, "unchanged": pulled.unchanged, "amount": pulled.amount,
    }, sort_keys=True))
    return pulled


def _file_rows(path: str) -> list[dict]:
    text = Path(path).read_text()
    if text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        return _rows(data, path)
    return read_csv(text)


def notifier(ledger: Ledger, env) -> Callable[[str, str], None]:
    """Alerts go where the gateway's go: Slack when set up, else the approval webhook (https
    only), else nowhere but the terminal. A failure is audited and printed, never raised."""
    slack = Slack.from_env(env)
    hook = env.get("QUARTER_APPROVAL_WEBHOOK")
    if hook and not hook.startswith("https://"):
        raise ValueError("the approval webhook must use https")

    def send(text: str, ref: str) -> None:
        try:
            if slack:
                slack.call("chat.postMessage", {"channel": slack.channel, "text": escape(text)})
            elif hook:
                request = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                                 headers={"content-type": "application/json"})
                urllib.request.urlopen(request, timeout=10).close()
        except (OSError, ValueError) as e:
            ledger.audit("webhook_failed", "connect", f"{ref} {e}")
            print(f"connect: the alert was not sent: {e}", file=sys.stderr)

    return send


def add_arguments(p) -> None:
    p.add_argument("source", choices=SOURCES)
    when = p.add_mutually_exclusive_group()
    when.add_argument("--month", help="YYYY-MM")
    when.add_argument("--since", help="YYYY-MM-DD or an ISO time; default where the last pull stopped")
    p.add_argument("--until", help="YYYY-MM-DD or an ISO time; default now")
    p.add_argument("--file", help="litellm: a CSV or JSON export of spend logs; openrouter-gen: the ids;"
                   " csv: the file")
    p.add_argument("--map", help="JSON text or a path: which fields name the agent, customer and task,"
                   " and which projects, workspaces or keys are which agent")
    p.add_argument("--vendor", help="csv: the vendor for rows without a vendor column")
    p.add_argument("--name", help="csv: the source name, so two different files dedupe apart (default csv)")
    p.add_argument("--spike-multiple", type=float, default=Spike.multiple,
                   help="alert when a day's observed spend passes this many times the usual (default 3)")
    p.add_argument("--spike-floor-usd", default="20", help="and this much (default 20)")
    p.add_argument("--no-alerts", action="store_true", help="book only; send no spike or budget alerts")


def run(args, ledger: Ledger, caps: list, env=None) -> None:
    env = os.environ if env is None else env
    source = args.source
    label = args.name if source == "csv" and args.name else source
    explicit = bool(args.month or args.since)
    try:
        mapping = load_map(source, args.map)
        start, end = connect.period(ledger, label, args.month, args.since, args.until)
        pulled = pull(ledger, source, start, end, env, mapping, args.file, args.vendor, label,
                      explicit=explicit)
        send = None if args.no_alerts else notifier(ledger, env)
    except (OSError, ValueError, KeyError) as e:  # a bad reply, file or map; json errors are ValueErrors
        sys.exit(f"connect {source}: {e}")
    span = "the file" if args.file and not explicit else f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC"
    print(f"{label}, {span}: {pulled.booked} new, {pulled.revised} revised, {pulled.unchanged} already booked;"
          f" {'-' if pulled.amount < 0 else ''}${usd_display(abs(pulled.amount))} added as observed spend")
    if pulled.changed_final:
        print(f"  {pulled.changed_final} record(s) differ from what was booked before; the first is kept",
              file=sys.stderr)
    if source in TOTALS:
        print("  vendor totals book only what no other source or the gateway already holds for that day")
    if send is not None:
        rule = Spike(args.spike_multiple, to_micros(args.spike_floor_usd))
        for text in connect.alert(ledger, caps, pulled.days, rule, send):
            print(f"  alert: {text}")
