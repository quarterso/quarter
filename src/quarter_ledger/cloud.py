"""The AI lines in the cost exports AWS, Azure and Google Cloud write to your own storage.

`quarter-ledger connect cloud --file <file or folder>` reads CSV files, gzipped or not, from an
AWS Data Export (CUR 2.0 or legacy CUR), an Azure Cost Management export (cost details or
FOCUS), a CSV or JSON-lines extract of Google Cloud's BigQuery billing export, or any FOCUS file.
A folder is read as the export left it: by its manifests where it has them, else every file, so
a month split over several files counts once. Parquet is refused: export CSV.

Only AI services are kept: Bedrock, SageMaker and the models sold through them (Claude bought from AWS
Marketplace shows under its own product), Vertex AI and Gemini, Azure OpenAI and Foundry.
Tax and commitment purchases are left out; credits on the same row count against the cost.
Each day, vendor and SKU becomes one vendor total that the cloud can still revise: a later
export of the same month books only the difference, and a revision of a locked month restates
into the next open one (period.py). Cost is in US dollars, from a USD column where the file has
one; a file in another currency with no USD column is refused. Cost is booked on the UTC day it
was used, so Google usage in the last hours of a month, which its invoice puts in that month by
Pacific time, falls on the next month here.
"""

import csv
import gzip
import io
import json
import re
from collections.abc import Iterable
from pathlib import Path

from .connect import Observed, usd

# First match wins. Day columns only: a month-only column such as invoice_month would put the
# whole month on its first day.
DAY = ["chargeperiodstart", "usage_start_time", "line_item_usage_start_date", "lineitem/usagestartdate",
       "usagedate", "usagedatetime", "date", "usage_date", "usage date", "usage start date"]
AMOUNT = ["effectivecost", "costinusd", "line_item_net_unblended_cost", "line_item_unblended_cost", "lineitem/unblendedcost", "pretaxcost",
          "costinbillingcurrency", "cost", "billedcost"]
USD_AMOUNT = {"costinusd"}
SKU = ["skumeter", "chargedescription", "sku.description", "sku_description", "line_item_usage_type",
       "lineitem/usagetype", "metername", "product"]
SERVICE = ["servicename", "servicecategory", "service.description", "service_description", "metercategory",
           "metersubcategory", "consumedservice", "publishername", "line_item_product_code",
           "lineitem/productcode", "product_product_name", "product/productname",
           "line_item_line_item_description", "lineitem/lineitemdescription", "product_product_family",
           "product"]
CREDITS = ["credits.amount", "credits_amount", "credits"]  # "credits": a job that summed them
KIND = ["chargecategory", "cost_type", "line_item_line_item_type", "lineitem/lineitemtype", "chargetype"]
CURRENCY = ["billingcurrency", "billingcurrencycode", "currency", "line_item_currency_code",
            "lineitem/currencycode"]
PROVIDER = ["serviceprovidername", "invoiceissuername", "providername"]

AI = re.compile(r"ai and machine learning|bedrock|sagemaker|gemini|generative language|vertex ai|agent platform|cognitive ?services|openai"
                r"|foundry|anthropic|claude|mistral|llama|cohere|ai21|titan|nova", re.IGNORECASE)
LEFT_OUT = re.compile(r"^(tax|purchase)$", re.IGNORECASE)


def _vendor(lower: list[str], provider: str) -> str:
    """aws, azure or google, from a FOCUS provider name or the export's own columns."""
    p = provider.lower()
    for name, words in (("aws", ("aws", "amazon")), ("azure", ("microsoft", "azure")), ("google", ("google",)),
                        ("openai", ("openai",)), ("anthropic", ("anthropic",))):
        if any(w in p for w in words):
            return name
    if any(h.startswith(("line_item_", "lineitem/")) for h in lower):
        return "aws"
    if "metercategory" in lower or "costinbillingcurrency" in lower:
        return "azure"
    if any(h.startswith(("sku", "service.description", "service_description")) for h in lower):
        return "google"
    raise ValueError("not an AWS, Azure, Google Cloud or FOCUS cost export: no columns it could place")


def _day(raw: str) -> str:
    text = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    us = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if us:
        return f"{us[3]}-{int(us[1]):02d}-{int(us[2]):02d}"
    raise ValueError(f"{raw!r} is not a date")


PERIOD = re.compile(r"BILLING_PERIOD=|^\d{8}-\d{8}$")
TABLE = re.compile(r"\.(csv|jsonl?|ndjson)(\.gz)?$", re.IGNORECASE)


def _named(manifest: Path) -> list[str]:
    """Every file path a manifest names, whatever its keys are called (AWS and Azure differ)."""
    found: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and re.search(r"\.(csv|parquet|jsonl?)(\.gz)?$", v, re.IGNORECASE):
            found.append(v)

    walk(json.loads(manifest.read_text()))
    return found


def _tail(text: str, n: int = 3) -> tuple[str, ...]:
    return tuple(text.replace("\\", "/").rstrip("/").split("/")[-n:])


def files(path: Path) -> list[Path]:
    """A file, or the files of a folder the export writes to. Where the folder has manifests, as
    AWS and Azure write, only the files the newest manifest of each billing period names are
    read: older runs and leftover parts are not the month's figures."""
    if not path.is_dir():
        if path.suffix.lower() == ".parquet":
            raise ValueError(f"{path.name} is Parquet; set the export to CSV")
        return [path]
    every = sorted(p for p in path.rglob("*") if p.is_file())
    manifests = [p for p in every if re.search(r"manifest.*\.json$", p.name, re.IGNORECASE)]
    if not manifests:
        found = [p for p in every if TABLE.search(p.name)]
        if not found and any(p.suffix.lower() == ".parquet" for p in every):
            raise ValueError(f"{path} holds Parquet files only; set the export to CSV")
        return found
    newest: dict[Path, Path] = {}
    for m in manifests:
        period = next((a for a in m.parents if PERIOD.search(a.name)), m.parent)
        if period not in newest or m.stat().st_mtime > newest[period].stat().st_mtime:
            newest[period] = m
    out = []
    for m in sorted(newest.values()):
        named = _named(m)
        if any(n.lower().endswith((".parquet", ".parquet.gz")) for n in named):
            raise ValueError(f"{m.name} lists Parquet files; set the export to CSV")
        for n in named:
            want = _tail(n)
            hit = [p for p in every if _tail(p.as_posix(), len(want)) == want] or [
                p for p in every if p.name == want[-1]]
            if len(hit) != 1:
                raise ValueError(f"{m.name} names {n}, and the folder has {len(hit)} files like it")
            out.append(hit[0])
    return sorted(set(out))


def _flat(obj: dict, prefix: str = "") -> dict:
    """A JSON row as flat columns: {"service": {"description": x}} is service.description, and
    Google's credits list is summed into credits.amount."""
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}".lower()
        if isinstance(v, dict):
            out |= _flat(v, key + ".")
        elif isinstance(v, list) and key == "credits":
            out["credits.amount"] = str(sum((usd(c.get("amount")) for c in v if isinstance(c, dict)), 0) / 1e6)
        elif not isinstance(v, list):
            out[key] = "" if v is None else str(v)
    return out


def _rows(path: Path) -> Iterable[list[str]]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8-sig")
    if re.search(r"\.(jsonl?|ndjson)(\.gz)?$", path.name, re.IGNORECASE):
        rows = [_flat(json.loads(line)) for line in text.splitlines() if line.strip()]
        header = sorted({k for r in rows for k in r})
        yield header
        for r in rows:
            yield [r.get(h, "") for h in header]
        return
    yield from csv.reader(io.StringIO(text))


def _columns(header: list[str]) -> dict:
    lower = [h.strip().lower() for h in header]

    def first(names):
        return next((lower.index(n) for n in names if n in lower), -1)

    cols = {"day": first(DAY), "amount": first(AMOUNT), "sku": first(SKU), "kind": first(KIND),
            "currency": first(CURRENCY), "provider": first(PROVIDER),
            "credits": [i for i, h in enumerate(lower) if h in CREDITS],
            "service": [i for i, h in enumerate(lower) if h in SERVICE], "lower": lower}
    if cols["day"] < 0 or cols["amount"] < 0:
        raise ValueError(f"no usage date and cost columns; the file has: {', '.join(header[:12])}")
    cols["usd"] = lower[cols["amount"]] in USD_AMOUNT
    return cols


def read(paths: list[Path]) -> tuple[list[Observed], dict]:
    """The files' AI cost as one vendor total per day, vendor and SKU, and what was left out."""
    totals: dict[tuple[str, str, str], int] = {}
    left = {"rows": 0, "micros": 0}
    for path in paths:
        rows = _rows(path)
        header = next(rows, None)
        if header is None:
            continue
        c = _columns(header)
        for n, row in enumerate(rows, start=2):
            if not any(f.strip() for f in row):
                continue

            def cell(i, row=row):
                return row[i].strip() if 0 <= i < len(row) else ""

            try:
                micros = usd(cell(c["amount"])) + sum(usd(cell(i)) for i in c["credits"])
                what = " ".join(cell(i) for i in c["service"])
                if LEFT_OUT.match(cell(c["kind"])) or not AI.search(what):
                    left["rows"] += 1
                    left["micros"] += micros
                    continue
                currency = cell(c["currency"]).upper()
                if currency and currency != "USD" and not c["usd"]:
                    raise ValueError(f"cost is in {currency} and the file has no cost in USD column")
                vendor = _vendor(c["lower"], cell(c["provider"]))
                key = (_day(cell(c["day"])), vendor, cell(c["sku"]) or "(no sku)")
            except (ValueError, ArithmeticError) as e:
                raise ValueError(f"{path.name} line {n}: {e}") from None
            totals[key] = totals.get(key, 0) + micros
    out = [Observed(f"{vendor}|{day}|{sku}", f"{day}T00:00:00+00:00", vendor, amount, model=sku, bucket=True)
           for (day, vendor, sku), amount in sorted(totals.items())]
    return out, left
