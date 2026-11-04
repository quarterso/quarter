import pytest

from quarter_ledger.cli import main
from quarter_ledger.margin import margins, read_revenue
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger


def spend(ledger, customer, usd):
    _, call = ledger.hold([], {"agent": "a", "customer": customer}, "openai", "m", to_micros(usd))
    ledger.settle(call, to_micros(usd))


def this_month(ledger):
    return ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]


def test_read_revenue_matches_headers_and_adds_repeats():
    text = '\ufeffCustomer ID,Amount Paid\nacme,"$1,200.50"\nacme,99.50\n,\nglobex,0\n'
    assert read_revenue(text) == {"acme": to_micros(1300), "globex": 0}


def test_read_revenue_names_the_bad_line():
    with pytest.raises(ValueError, match="line 3"):
        read_revenue("customer,amount\nacme,10\nglobex,ten\n")
    with pytest.raises(ValueError, match="line 2"):
        read_revenue("customer,amount\nacme,Infinity\n")
    with pytest.raises(ValueError, match="customer column"):
        read_revenue("name,price\nacme,10\n")


def test_margin_by_customer_worst_first(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    spend(ledger, "acme", 30)
    spend(ledger, "globex", 120)
    spend(ledger, "initech", 5)
    spend(ledger, None, 7)
    month = this_month(ledger)
    ledger.add_cost("seat", "cursor", to_micros(20), month, customer="acme")

    out = margins(
        ledger, month, {"acme": to_micros(100), "globex": to_micros(100), "hooli": to_micros(50)}
    )
    rows = {r["customer"]: r for r in out["rows"]}
    assert [r["customer"] for r in out["rows"]] == ["initech", "globex", "acme", "hooli"]
    assert rows["acme"]["ai_cost"] == to_micros(50) and rows["acme"]["margin_pct"] == 50
    assert rows["globex"]["margin"] == to_micros(-20)
    assert rows["initech"]["margin_pct"] is None
    assert out["unbilled_customers"] == 1 and out["unbilled_cost"] == to_micros(5)
    assert out["unassigned_cost"] == to_micros(7)
    assert out["margin"] == to_micros(250 - 175)
    assert out["heavy_customers"] == 1 and round(out["heavy_share_pct"]) == round(120 / 175 * 100)


def test_margin_command(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    spend(ledger, "acme", 150)
    month = this_month(ledger)
    revenue = tmp_path / "revenue.csv"
    revenue.write_text("customer,amount_usd\nacme,100\n")

    main(["--db", db, "margin", "--month", month, "--revenue", str(revenue)])
    text = capsys.readouterr().out
    assert "margin after AI cost -$50.00 -50.0%" in text
    assert "costs more than it pays" in text

    main(["--db", db, "margin", "--month", month, "--revenue", str(revenue), "--csv"])
    assert capsys.readouterr().out.splitlines()[1] == "acme,100.00,150.00,-50.00,-50.0"
