import json

from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger


def spend(ledger, agent, customer, usd, at):
    _, call = ledger.hold([], {"agent": agent, "customer": customer, "task": f"t-{agent}"}, "openai", "gpt-4o",
                          to_micros(usd))
    ledger.settle(call, to_micros(usd))
    ledger.db.execute("update entries set at = ? where call_id = ?", (at, call))


def test_board_page_agrees_with_the_reports_it_is_built_from(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    spend(ledger, "support-bot", "acme", 30, "2026-10-05T00:00:00+00:00")
    spend(ledger, "support-bot", "globex", 10, "2026-10-06T00:00:00+00:00")
    spend(ledger, "claude-code", None, 20, "2026-10-07T00:00:00+00:00")
    spend(ledger, "support-bot", "acme", 40, "2026-09-05T00:00:00+00:00")
    ledger.add_outcome("ticket_resolved", "t-support-bot", at="2026-10-08T00:00:00")
    (tmp_path / "rev.csv").write_text("customer,amount\nacme,100\n")
    (tmp_path / "centres.json").write_text(json.dumps({"class": {"claude-code": "R&D"}}))

    main(["--db", db, "board", "--month", "2026-10", "--revenue", str(tmp_path / "rev.csv"),
          "--cost-centres", str(tmp_path / "centres.json")])
    page = capsys.readouterr().out
    assert "Total AI spend $60.00, against $40.00 in 2026-09 (50.0%)." in page
    assert "| AI vendor costs (R&D) | $20.00 | $0.00 |" in page
    assert "| Cost of revenue: AI | $40.00 | $40.00 |" in page
    assert "margin after AI cost $60.00 (60.0%)" in page
    assert "1 customer(s) cost $10.00 in AI and paid nothing this month." in page
    assert page.index("| acme |") < page.index("| globex |")
    assert "| ticket_resolved | 1 |" in page
    assert "Reconciled with the vendors' own figures: none. Not reconciled: openai." in page
    assert "—" not in page

    main(["--db", db, "board", "--month", "2026-10"])
    assert "No revenue was given, so no margin" in capsys.readouterr().out
