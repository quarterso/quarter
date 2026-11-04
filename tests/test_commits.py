import json
from datetime import UTC, datetime, timedelta

import pytest

from quarter_ledger import reconcile
from quarter_ledger.board import board
from quarter_ledger.cli import main
from quarter_ledger.commits import read_commits, standing
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger

TODAY = datetime.now(UTC).date()


def spend(ledger, usd):
    _, ref = ledger.hold([], {"agent": "a"}, "openai", "gpt-4o", to_micros(usd))
    ledger.settle(ref, to_micros(usd))


def commit(start, end, usd=100):
    return {"vendor": "openai", "amount_usd": usd, "from": start.isoformat(), "to": end.isoformat()}


def test_pace_projects_a_shortfall(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    spend(ledger, 30)
    (c,) = read_commits([commit(TODAY - timedelta(days=9), TODAY + timedelta(days=20))])
    out = standing(ledger, c, TODAY)
    # $30 over the 10 days the contract has run: $3 a day for 20 more days
    assert (out["spent"], out["per_day"], out["projected"], out["shortfall"], out["overage"]) == (
        to_micros(30), to_micros(3), to_micros(90), to_micros(10), 0)
    assert standing(ledger, c, TODAY + timedelta(days=40))["days_left"] == 0


def test_a_reconciled_month_counts_at_the_vendors_figure(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    spend(ledger, 30)
    month = f"{TODAY:%Y-%m}"
    seen, _ = reconcile.quarter_side(ledger, "openai", month)
    reconcile.record(ledger, "openai", month, "api", reconcile.compare({"gpt-4o": to_micros(25)}, seen, 2.0), "Fay")
    (c,) = read_commits([commit(TODAY.replace(day=1), TODAY.replace(day=1) + timedelta(days=400), 20)])
    out = standing(ledger, c, TODAY)
    assert (out["spent"], out["at_vendor_figures"]) == (to_micros(25), [month])
    assert out["overage"] > 0 and out["shortfall"] == 0


def test_bad_commits_are_refused():
    with pytest.raises(ValueError, match="commit 1"):
        read_commits([{"vendor": "openai", "amount_usd": 5, "from": "2026-13-01", "to": "2026-12-31"}])
    with pytest.raises(ValueError, match="before"):
        read_commits([commit(TODAY, TODAY - timedelta(days=1))])
    with pytest.raises(ValueError, match="non-empty"):
        read_commits({})


def test_cli_and_board(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    spend(ledger, 30)
    path = tmp_path / "commits.json"
    path.write_text(json.dumps([commit(TODAY - timedelta(days=9), TODAY + timedelta(days=20))]))
    main(["--db", db, "commits", str(path), "--as-of", TODAY.isoformat()])
    assert "SHORT by $10.00: paid for and not used" in capsys.readouterr().out
    page = board(ledger, f"{TODAY:%Y-%m}", commits=read_commits(json.loads(path.read_text())))
    assert "## Vendor commitments" in page and "**Projected shortfall:** openai $10.00" in page
