import json

import pytest

from quarter_ledger.cli import main
from quarter_ledger.margin import margins
from quarter_ledger.money import to_micros
from quarter_ledger.reprice import read_as_model, reprice
from quarter_ledger.store import Ledger

# $10 a million input tokens and $30 a million output; the mini model a tenth of that
BASE = {"openai": {"big": {"input_per_million": 10, "output_per_million": 30},
                   "mini": {"input_per_million": 1, "output_per_million": 3}}}


def call(ledger, customer, model, usd, tokens=(1_000_000, 0), route="api"):
    _, ref = ledger.hold([], {"agent": "a", "customer": customer}, "openai", model, to_micros(usd))
    ledger.settle(ref, to_micros(usd))
    zero = dict.fromkeys(("tools_cost", "system_cost", "history_cost", "output_cost", "break_cost", "saved"), 0)
    ledger.record_call({**zero, "call_id": ref, "at": "2026-01-01T00:00:00+00:00", "vendor": "openai",
                        "model": model, "route": route, "input": tokens[0], "output": tokens[1],
                        "cache_read": 0, "cache_write": 0, "cost": to_micros(usd), "list_cost": to_micros(usd),
                        "fingerprint": "f"})
    return ref


def month(ledger):
    return ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]


def test_before_is_margin_and_only_named_calls_move(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    call(ledger, "acme", "big", 10)  # 1M input tokens at $10
    call(ledger, "acme", "mini", 1)
    call(ledger, "globex", "big", 10, route="subscription")
    m = month(ledger)
    revenue = {"acme": to_micros(15), "globex": to_micros(20)}

    same = reprice(ledger, m, revenue, BASE)
    now = margins(ledger, m, revenue)
    assert (same["ai_cost"], same["margin"], same["ai_cost_after"]) == (now["ai_cost"], now["margin"], now["ai_cost"])

    # big doubles in price: acme's big call is repriced; globex's is paid by a subscription
    out = reprice(ledger, m, revenue, BASE, prices={"openai": {"big": {"input_per_million": 20}}})
    acme = next(r for r in out["rows"] if r["customer"] == "acme")
    assert (acme["ai_cost"], acme["ai_cost_after"]) == (to_micros(11), to_micros(21))
    assert acme["turns_negative"] and out["calls_repriced"] == 1
    assert out["not_repriced"] == to_micros(10)
    assert out["rows"][0]["customer"] == "acme"  # worst margin after comes first


def test_moving_a_model_and_raising_prices(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    call(ledger, "acme", "big", 40, tokens=(1_000_000, 1_000_000))  # $10 in + $30 out
    m = month(ledger)
    out = reprice(ledger, m, {"acme": to_micros(50)}, BASE, as_model=read_as_model(["openai/big=mini"]),
                  revenue_change_pct=10)
    (acme,) = out["rows"]
    assert (acme["ai_cost_after"], acme["revenue_after"], acme["margin_after"]) == (
        to_micros(4), to_micros(55), to_micros(51))
    with pytest.raises(ValueError, match="no price for openai/nano"):
        reprice(ledger, m, {}, BASE, as_model={("openai", "big"): "nano"})
    with pytest.raises(ValueError, match="VENDOR/FROM=TO"):
        read_as_model(["big=mini"])


def test_cli(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    call(ledger, "acme", "big", 10)
    monkeypatch.setenv("QUARTER_PRICES", json.dumps(BASE))
    revenue = tmp_path / "revenue.csv"
    revenue.write_text("customer,amount\nacme,15\n")
    main(["--db", db, "reprice", "--month", month(ledger), "--revenue", str(revenue), "--as-model", "openai/big=mini"])
    text = capsys.readouterr().out
    assert "1 call(s) repriced from their own tokens: $10.00 -> $1.00" in text
    assert "margin   33.3% ->   93.3%" in text
    main(["--db", db, "reprice", "--month", month(ledger), "--revenue", str(revenue), "--csv",
          "--prices", '{"openai": {"big": {"input_per_million": 20}}}'])
    assert capsys.readouterr().out.splitlines()[1] == "acme,15.00,10.00,5.00,15.00,20.00,-5.00,33.3,-33.3,yes"
