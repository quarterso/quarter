import csv
from datetime import UTC, datetime
from io import StringIO

import pytest

from quarter_ledger.export import journal
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger, agent_summary

AGENT = {"agent": "researcher", "task": None, "customer": "acme"}
MONTH = datetime.now(UTC).strftime("%Y-%m")


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "l.db"))


def spend(ledger, amount):
    _, call = ledger.hold([], AGENT, "openai", "m", amount)
    ledger.settle(call, amount)


def test_monthly_cost_counts_until_ended(ledger):
    cost = ledger.add_cost("seat", "cursor", to_micros(40), "2026-01", monthly=True, agent="coder")
    ledger.add_cost("compute", "aws", to_micros(15), "2026-02", agent="coder")
    assert [c["amount"] for c in ledger.costs_in("2025-12")] == []
    assert sum(c["amount"] for c in ledger.costs_in("2026-02")) == to_micros(55)
    assert ledger.end_cost(cost, "2026-03")
    assert len(ledger.costs_in("2026-03")) == 1
    assert ledger.costs_in("2026-04") == []


def test_summary_and_journal_include_both_directions(ledger):
    spend(ledger, to_micros("1.50"))
    ledger.add_cost("subscription", "exa", to_micros(49), MONTH, monthly=True, agent="researcher")
    ledger.add_cost("people", "ops contractor", to_micros(300), MONTH)

    rows = {r["agent"]: r for r in agent_summary(ledger, MONTH)}
    assert rows["researcher"]["by"] == to_micros("1.50")
    assert rows["researcher"]["on"] == to_micros(49)
    assert rows["(company)"]["on"] == to_micros(300)

    lines = list(csv.DictReader(StringIO(journal(ledger, MONTH))))
    debits = sum(round(float(r["Debit"]) * 100) for r in lines if r["Debit"])
    credits = sum(round(float(r["Credit"]) * 100) for r in lines if r["Credit"])
    assert debits == credits == 150 + 4900 + 30000
