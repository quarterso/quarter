import json

import pytest

from quarter_ledger.capitalise import capitalised, read_projects, reclass
from quarter_ledger.cli import main
from quarter_ledger.export import journal
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger

PROJECTS = [
    {"project": "Checkout rebuild", "from": "2026-10-10", "agents": ["claude-code"], "tasks": ["checkout-*"]},
    {"project": "Search", "from": "2026-10-01", "to": "2026-10-20", "tasks": ["search-*"]},
]


def spend(ledger, agent, task, amount, at, customer=None):
    ids = {"agent": agent, "task": task, "customer": customer}
    _, call = ledger.hold([], ids, "anthropic", "m", to_micros(amount))
    ledger.settle(call, to_micros(amount))
    ledger.db.execute("update entries set at = ? where call_id = ?", (at, call))
    return call


def test_attributes_spend_by_project_window_agent_and_task(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    spend(ledger, "claude-code", "checkout-api", "3.50", "2026-10-12T10:00:00+00:00")
    spend(ledger, "claude-code", "checkout-ui", "1.25", "2026-10-31T23:00:00+00:00")
    spend(ledger, "claude-code", "checkout-api", "9", "2026-10-09T23:59:00+00:00")  # before from
    spend(ledger, "other", "checkout-api", "4", "2026-10-12T10:00:00+00:00")  # agent not listed
    spend(ledger, "claude-code", "search-index", "2", "2026-10-20T12:00:00+00:00")  # last day counts
    spend(ledger, "claude-code", "search-index", "5", "2026-10-21T00:00:00+00:00")  # after to
    spend(ledger, "claude-code", "checkout-api", "7", "2026-11-01T00:00:00+00:00")  # next month
    out = capitalised(ledger, "2026-10", read_projects(json.dumps(PROJECTS)))
    assert out["projects"] == [
        {"project": "Checkout rebuild", "amount": to_micros("4.75")},
        {"project": "Search", "amount": to_micros(2)},
    ]
    assert out["total"] == to_micros("6.75") and not out["conflicts"]
    assert [(t["task"], t["calls"]) for t in out["tasks"]] == [("checkout-api", 1), ("checkout-ui", 1), ("search-index", 1)]


def test_overlap_is_counted_in_neither_and_reclass_balances(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    projects = read_projects(json.dumps([*PROJECTS, {"project": "Platform", "from": "2026-10-01", "agents": ["claude-code"]}]))
    both = spend(ledger, "claude-code", "checkout-api", "3", "2026-10-12T10:00:00+00:00")
    spend(ledger, "bot", "search-x", "2", "2026-10-02T10:00:00+00:00", customer="acme")
    out = capitalised(ledger, "2026-10", projects)
    assert out["conflicts"][0]["call_id"] == both and out["total"] == to_micros(2)
    rows = reclass(out)
    assert sum(r.debit for r in rows) == sum(r.credit for r in rows) == 200
    assert {r.account for r in rows} == {"Capitalised software", "Cost of revenue: AI"}
    text = journal(ledger, "2026-10", fmt="xero", rows=rows, number="AGENT-CAP-2026-10", narration="cap")
    assert "Capitalised software" in text and "-2.00" in text and "AI vendor costs" not in text


@pytest.mark.parametrize(
    "bad",
    [
        {"project": "x", "from": "2026-10-01"},
        {"project": "x", "from": "October", "agents": ["a"]},
        {"project": "x", "from": "2026-10-05", "to": "2026-10-01", "agents": ["a"]},
        {"project": "x", "from": "2026-10-01", "tasks": []},
        {"from": "2026-10-01", "agents": ["a"]},
    ],
)
def test_rejects_unclear_projects(bad):
    with pytest.raises(ValueError):
        read_projects(json.dumps([bad]))


def test_cli(tmp_path, capsys):
    db, projects, evidence = str(tmp_path / "l.db"), tmp_path / "p.json", tmp_path / "e.csv"
    spend(Ledger(db), "claude-code", "checkout-api", "3.50", "2026-10-12T10:00:00+00:00")
    projects.write_text(json.dumps(PROJECTS))
    run = ["--db", db, "capitalise", "--month", "2026-10", "--projects", str(projects)]
    main([*run, "--evidence", str(evidence)])
    out = capsys.readouterr().out
    assert "$3.50" in out and "Checkout rebuild" in out
    assert "checkout-api" in evidence.read_text()
    main([*run, "--journal", "quickbooks"])
    out = capsys.readouterr().out
    assert "AGENT-CAP-2026-10" in out and "10/31/2026" in out
    projects.write_text("[{}]")
    with pytest.raises(SystemExit, match="needs a name"):
        main(run)
