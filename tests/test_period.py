import json
from datetime import UTC, datetime

import pytest

from quarter_ledger import connect, period
from quarter_ledger.cli import main
from quarter_ledger.connect import Observed
from quarter_ledger.money import to_micros
from quarter_ledger.pricing import PriceBook
from quarter_ledger.store import Ledger

BOOK = PriceBook.load({})
LATER = datetime(2027, 1, 5, tzinfo=UTC)


def day(ledger, at, usd, model="gpt-4o", id_=None):
    """A vendor's daily total, as connect books it from a cost report."""
    connect.book(ledger, "openai", [Observed(id_ or at, f"{at}T00:00:00+00:00", "openai", to_micros(usd),
                                             model=model, bucket=True)])


def charged(ledger, month):
    return ledger.db.execute("select coalesce(sum(amount), 0) from entries where kind = 'charge'"
                             " and substr(at, 1, 7) = ?", (month,)).fetchone()[0]


def test_lock_writes_a_package_the_ledger_can_verify(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    day(ledger, "2026-10-03", 100)
    folder = period.lock(ledger, "2026-10", "Sam", tmp_path, BOOK, "test")
    assert folder.name == "close-2026-10-v1"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert set(manifest["files"]) == {"journal.csv", "flux.md", "flux.json", "restatements.csv"}
    assert manifest["entries"]["charged_micros"] == to_micros(100) and manifest["locked_by"] == "Sam"
    assert "AGENT-2026-10-V1" in (folder / "journal.csv").read_text()
    assert period.verify(ledger, folder) == []
    (folder / "journal.csv").write_text("edited")
    assert period.verify(ledger, folder) == ["journal.csv changed since the lock"]


def test_a_month_must_have_ended_and_locks_once(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    with pytest.raises(ValueError, match="has not ended"):
        period.lock(ledger, "2026-11", "Sam", tmp_path, BOOK, "t", at=datetime(2026, 11, 20, tzinfo=UTC))
    period.lock(ledger, "2026-10", "Sam", tmp_path, BOOK, "t")
    with pytest.raises(ValueError, match="locked already"):
        period.lock(ledger, "2026-10", "Sam", tmp_path, BOOK, "t")


def test_a_revision_after_the_lock_is_booked_in_the_next_open_month(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    day(ledger, "2026-10-03", 100)
    folder = period.lock(ledger, "2026-10", "Sam", tmp_path, BOOK, "t")
    day(ledger, "2026-10-03", 130)  # the vendor revised the day up by $30
    assert charged(ledger, "2026-10") == to_micros(100)
    assert charged(ledger, "2026-11") == to_micros(30)
    [r] = period.restated_into(ledger, "2026-11")
    assert (r["spent_at"][:10], r["booked_at"][:10], r["amount"]) == ("2026-10-03", "2026-11-01", to_micros(30))
    assert period.verify(ledger, folder) == []  # the locked month did not move
    nov = period.lock(ledger, "2026-11", "Sam", tmp_path, BOOK, "t", at=LATER)
    assert "2026-10-03" in (nov / "restatements.csv").read_text()
    day(ledger, "2026-10-03", 125)  # down $5, with October and November both locked
    assert charged(ledger, "2026-12") == to_micros(-5)


def test_reopen_needs_a_reason_and_the_next_lock_is_a_new_version(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    day(ledger, "2026-10-03", 100)
    v1 = period.lock(ledger, "2026-10", "Sam", tmp_path, BOOK, "t")
    with pytest.raises(ValueError, match="why"):
        period.reopen(ledger, "2026-10", "Sam", " ")
    assert period.reopen(ledger, "2026-10", "Sam", "vendor credit missed") == 1
    day(ledger, "2026-10-04", 20)  # open again, so it lands in October
    assert charged(ledger, "2026-10") == to_micros(120)
    assert period.verify(ledger, v1)[-1].startswith("the month's entries changed since the lock")
    v2 = period.lock(ledger, "2026-10", "Ana", tmp_path, BOOK, "t")
    assert v2.name == "close-2026-10-v2" and "AGENT-2026-10-V2" in (v2 / "journal.csv").read_text()
    assert [(r["version"], bool(r["reopened_at"])) for r in period.locks(ledger)] == [(1, True), (2, False)]
    audit = [a for (a,) in ledger.db.execute("select action from audit where action like 'period_%' order by id")]
    assert audit == ["period_lock", "period_reopen", "period_lock"]


def test_the_command(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    day(Ledger(db), "2026-10-03", 100)
    main(["--db", db, "period", "lock", "--month", "2026-10", "--by", "Sam", "--out", str(tmp_path)])
    assert "2026-10 locked" in capsys.readouterr().out
    main(["--db", db, "period", "verify", str(tmp_path / "close-2026-10-v1")])
    assert "agree" in capsys.readouterr().out
    main(["--db", db, "period", "list"])
    assert "2026-10 v1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--db", db, "period", "lock", "--month", "2026-10", "--by", "Sam", "--out", str(tmp_path)])


def test_a_vendor_total_counts_what_another_source_restated(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    period.lock(ledger, "2026-10", "Sam", tmp_path, BOOK, "t")
    # a detailed source books a $40 call on a locked day: restated into November
    connect.book(ledger, "litellm", [Observed("call-1", "2026-10-03T10:00:00+00:00", "openai", to_micros(40),
                                              model="gpt-4o")])
    # the vendor's total for that day is $100: only the other $60 is new
    records = connect.remainder(ledger, "openai", "openai", {("2026-10-03", "gpt-4o"): {None: to_micros(100)}})
    assert [r.amount for r in records] == [to_micros(60)]
