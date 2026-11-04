import json
import urllib.error
import urllib.request

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, PRICES, start

from quarter_ledger.cli import main
from quarter_ledger.dashboard import render
from quarter_ledger.money import to_micros
from quarter_ledger.outcomes import cost_per_outcome, read_outcomes
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger


def spend(ledger, task, usd, customer="acme"):
    _, call = ledger.hold([], {"agent": "bot", "task": task, "customer": customer}, "openai", "m", to_micros(usd))
    ledger.settle(call, to_micros(usd))


def this_month(ledger):
    return ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]


def test_task_cost_is_split_across_its_outcomes(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    spend(ledger, "t1", 3)
    spend(ledger, "t2", 1)
    spend(ledger, "t3", 5)  # produced nothing
    ledger.add_outcome("ticket_resolved", "t1", value=to_micros(20))
    ledger.add_outcome("ticket_resolved", "t1", value=to_micros(20))
    ledger.add_outcome("ticket_resolved", "t2")
    ledger.add_outcome("lead_qualified", "never-spent")
    out = cost_per_outcome(ledger, this_month(ledger))
    tickets = next(r for r in out["rows"] if r["kind"] == "ticket_resolved")
    assert tickets["count"] == 3 and tickets["cost"] == to_micros(4)
    assert tickets["per_outcome"] == to_micros(4) // 3
    assert tickets["value_per_dollar"] == 10.0  # $40 reported against $4
    leads = next(r for r in out["rows"] if r["kind"] == "lead_qualified")
    assert leads["unpriced"] == 1 and leads["per_outcome"] is None
    assert (out["idle_tasks"], out["idle_cost"]) == (1, to_micros(5))


def test_an_outcome_sent_twice_with_its_id_counts_once(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    first = ledger.add_outcome("ticket_resolved", "t1", ref="zd-1")
    assert ledger.add_outcome("ticket_resolved", "t1", ref="zd-1") == (first[0], False)


def test_read_outcomes_from_a_csv():
    rows = read_outcomes("﻿Kind,Task,Value_USD,id,at\nticket_resolved,t1,12.50,zd-1,2026-10-05\n")
    assert rows == [{"kind": "ticket_resolved", "task": "t1", "at": "2026-10-05T00:00:00", "ref": "zd-1",
                     "customer": None, "agent": None, "value": to_micros("12.50")}]
    with pytest.raises(ValueError, match="kind column"):
        read_outcomes("what,task\nx,t\n")
    with pytest.raises(ValueError, match="line 2"):
        read_outcomes("kind,task,value_usd\nx,t,ten\n")


def test_agents_report_outcomes_over_the_gateway(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, key_agents={AGENT_KEY: "bot"})
    proxy = start(serve(gw, port=0))

    def post(body, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{proxy.server_port}/outcomes", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        key = {"x-quarter-key": AGENT_KEY}
        assert post({"kind": "ticket_resolved", "task": "t1"})[0] == 401
        status, body = post({"kind": "ticket_resolved", "id": "zd-9", "value_usd": "15"},
                            {**key, "x-quarter-task": "t1", "x-quarter-customer": "acme"})
        assert status == 201 and body["recorded"] is True
        assert post({"kind": "ticket_resolved", "id": "zd-9"}, {**key, "x-quarter-task": "t1"})[1]["recorded"] is False
        assert post({"kind": "ticket_resolved"}, key)[0] == 400  # no task anywhere
        assert post({"kind": "x", "task": "t"}, {**key, "x-quarter-agent": "someone-else"})[0] == 403
        row = ledger.db.execute("select kind, task, agent, customer, value from outcomes").fetchone()
        assert row == ("ticket_resolved", "t1", "bot", "acme", to_micros(15))
    finally:
        proxy.shutdown()


def test_outcomes_command_imports_and_reports(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    spend(ledger, "t1", 2)
    spend(ledger, "t9", 1)
    month = this_month(ledger)
    found = tmp_path / "outcomes.csv"
    found.write_text(f"kind,task,id,value_usd,at\ninvoice_closed,t1,a,10,{month}-02\n")
    main(["--db", db, "outcomes", "--month", month, "--add", str(found)])
    text = capsys.readouterr().out
    assert "1 outcome(s) recorded, 0 already there" in text
    assert "invoice_closed" in text and "per outcome      $2.00" in text and "returns 5.0x" in text
    assert "1 task(s) spent $1.00 and reported no outcome" in text
    main(["--db", db, "outcomes", "--month", month, "--add", str(found), "--csv"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "0 outcome(s) recorded, 1 already there"
    assert lines[2] == "invoice_closed,1,2.00,2.00,10.00,5.00"


def test_dashboard_shows_cost_per_outcome_once_one_is_reported(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    spend(ledger, "t1", 2)
    month = this_month(ledger)
    assert "Cost per outcome" not in render(ledger, [], month=month)
    ledger.add_outcome("ticket_resolved", "t1")
    page = render(ledger, [], month=month)
    assert "<td>ticket_resolved</td><td class=n>1</td><td class=n>$2.00</td><td class=n>$2.00</td>" in page
