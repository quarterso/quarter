import json
import re
from itertools import count

from quarter_ledger import flux
from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.pricing import PriceBook
from quarter_ledger.store import Ledger

# $10 a million input tokens for big, $1 for mini, $5 for old
BOOK = PriceBook.load({"openai": {m: {"input_per_million": p, "output_per_million": p}
                                  for m, p in (("big", 10), ("mini", 1), ("old", 5))}})
ZERO = dict.fromkeys(("tools_cost", "system_cost", "history_cost", "output_cost", "break_cost", "saved"), 0)
ids = count()


def charge(ledger, vendor, model, usd, at, tokens=None):
    cid = f"c{next(ids)}"
    with ledger._write():
        ledger._append(cid, "charge", {}, vendor, model, to_micros(usd), at=at)
    if tokens is not None:
        ledger.record_call({**ZERO, "call_id": cid, "at": at, "vendor": vendor, "model": model, "route": "api",
                            "input": tokens, "output": 0, "cache_read": 0, "cache_write": 0,
                            "cost": to_micros(usd), "list_cost": to_micros(usd), "fingerprint": "f"})


def test_the_drivers_split_use_price_new_and_stopped_and_add_up(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    charge(ledger, "openai", "big", 10, "2026-09-10T00:00:00+00:00", 1_000_000)  # list $10, paid $10
    charge(ledger, "openai", "old", 5, "2026-09-11T00:00:00+00:00", 1_000_000)
    charge(ledger, "openai", "big", 16, "2026-10-10T00:00:00+00:00", 2_000_000)  # list $20, paid $16
    charge(ledger, "openai", "mini", 1, "2026-10-12T00:00:00+00:00", 1_000_000)
    [b] = flux.bridges(ledger, "2026-10", BOOK, [], (to_micros(1), 10))
    got = {d.kind: d.micros for d in b.drivers}
    assert got == {"volume": to_micros(10), "rate": to_micros(-4), "new": to_micros(1), "stopped": to_micros(-5)}
    assert sum(got.values()) == b.now - b.before == to_micros(2)
    assert b.needs_note and "rose $2.00 (13%)" in b.summary
    assert "No published price change covers it" in b.drivers[1].text


def test_a_price_change_is_matched_to_its_notice(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    charge(ledger, "openai", "big", 10, "2026-09-10T00:00:00+00:00", 1_000_000)
    charge(ledger, "openai", "big", 5, "2026-10-10T00:00:00+00:00", 1_000_000)
    listed = [{"date": "2026-10-01", "vendor": "openai", "pattern": re.compile("big"), "note": "Big halved.",
               "source": "https://example.com/n", "kind": "price"}]
    [b] = flux.bridges(ledger, "2026-10", BOOK, listed)
    [rate] = b.drivers
    assert rate.kind == "rate" and rate.micros == to_micros(-5)
    assert "Big halved." in rate.text and rate.sources == ["https://example.com/n"]


def test_spend_from_a_cost_report_is_not_split(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    charge(ledger, "anthropic", "claude-sonnet-5-5", 100, "2026-09-02T00:00:00+00:00")
    charge(ledger, "anthropic", "claude-sonnet-5-5", 130, "2026-10-02T00:00:00+00:00")
    [b] = flux.bridges(ledger, "2026-10", BOOK, [])
    assert [(d.kind, d.micros) for d in b.drivers] == [("unsplit", to_micros(30))]


def test_coming_changes_are_priced_on_this_months_use(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    charge(ledger, "gemini", "gemini-4-argon", 50, "2026-10-05T00:00:00+00:00")
    _, listed = flux.changes()
    [c] = flux.coming(ledger, "2026-10", BOOK, listed)
    assert c["date"] == "2026-12-01" and c["impact"] == to_micros(50)
    assert "about +$50.00 a month" in c["text"]


def test_steady_use_is_flagged_and_weekday_use_is_not(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for d in range(1, 32):
        at = f"2026-10-{d:02d}T03:00:00+00:00"
        charge(ledger, "openai", "poller", 1, at)
        weekend = d % 7 in (3, 4)  # 2026-10-03 is a Saturday
        charge(ledger, "openai", "people", 0.2 if weekend else 2, at)
    daily, items = flux.steady(ledger, "2026-10", BOOK)
    assert daily and [i["model"] for i in items] == ["poller"]
    assert items[0]["floor"] == to_micros(31)
    assert flux.steady(Ledger(str(tmp_path / "e.db")), "2026-10", BOOK) == (False, [])


def test_the_command_prints_markdown_and_json(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    charge(ledger, "openai", "gpt-4o", 10, "2026-09-10T00:00:00+00:00")
    charge(ledger, "openai", "gpt-4o", 30, "2026-10-10T00:00:00+00:00")
    main(["--db", db, "flux", "--month", "2026-10", "--over-usd", "5"])
    out = capsys.readouterr().out
    assert out.startswith("# Why AI spend moved, October 2026")
    assert "OpenAI rose $20.00 (200%)" in out and "needs a note" in out
    main(["--db", db, "flux", "--month", "2026-10", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["bridges"][0]["drivers"][0]["kind"] == "unsplit"


def test_the_change_list_loads_and_every_pattern_compiles():
    checked, listed = flux.changes()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked) and listed
    for c in listed:
        assert c["vendor"] in flux.NAMES and c["source"].startswith("https://")
        assert c["kind"] in {"price", "cache", "tier", "reroute", "retire", "billing", "promo"}


def test_gemini_changes_apply_to_google_cloud_rows(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    charge(ledger, "google", "Gemini 4 Argon Input Tokens", 50, "2026-10-05T00:00:00+00:00")
    _, listed = flux.changes()
    assert [c["date"] for c in flux.coming(ledger, "2026-10", BOOK, listed)] == ["2026-12-01"]
