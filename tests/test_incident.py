import json
from datetime import UTC, datetime, timedelta

import pytest

from quarter_ledger import cli, incident, receipts
from quarter_ledger.store import Ledger, chain_hash

KEY = "ed25519:" + "07" * 32
TODAY = datetime.now(UTC).date()


def spend(ledger, agent, task, amount):
    _, call_id = ledger.hold([], {"agent": agent, "task": task, "customer": None}, "anthropic", "m", amount)
    ledger.settle(call_id, amount)


@pytest.fixture
def ledger_with_a_statement(tmp_path, monkeypatch):
    """Spending and actions, a signed statement, then more spending after it."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(str(db))
    spend(ledger, "deployer", "t1", 500)
    spend(ledger, "reader", None, 300)
    ledger.audit("upstream_call", "127.0.0.1", json.dumps(
        {"agent": "deployer", "task": "t1", "upstream": "github", "method": "DELETE",
         "path": "/repos/x", "decision": "denied"}))
    ledger.pause("deployer", "made the same tool call 8 times", task="t1")
    ledger.audit("paused", None, "task t1 of deployer: made the same tool call 8 times")
    monkeypatch.setenv("QUARTER_SIGNING_KEY", KEY)
    statement = tmp_path / "statement.json"
    cli.main(["--db", str(db), "underwriting", "--month", TODAY.strftime("%Y-%m"), "--out", str(statement)])
    ledger.resume_task("t1", "Dana")
    spend(ledger, "deployer", "t1", 700)  # after the statement: not yet anchored
    return db, statement


def make_pack(db, path, **scope):
    args = ["--db", str(db), "incident", "--from", str(TODAY), "--to", str(TODAY + timedelta(days=1)),
            "--out", str(path)]
    for name, value in scope.items():
        args += [f"--{name}", value]
    cli.main(args)
    return json.loads(path.read_text())


def check(capsys, pack_path, *statements):
    capsys.readouterr()
    args = ["check-incident", str(pack_path)]
    for s in statements:
        args += ["--statement", str(s)]
    cli.main(args)
    return json.loads(capsys.readouterr().out)["chains"]


def test_an_incident_is_proven_up_to_the_statement_an_insurer_holds(
        ledger_with_a_statement, tmp_path, monkeypatch, capsys):
    db, statement = ledger_with_a_statement
    pack_path = tmp_path / "incident.json"
    pack = make_pack(db, pack_path, agent="deployer")
    assert pack["schema"] == "quarter.incident/1"
    assert {c["agent"] for c in pack["calls"]} <= {"deployer"}
    # deployer's two holds, releases and charges; the upstream call and the pause
    assert len(pack["in_scope"]["entries"]) == 6 and len(pack["in_scope"]["audit"]) == 2

    monkeypatch.setenv("QUARTER_SIGNING_KEY", receipts.public_key(KEY))
    chains = check(capsys, pack_path, statement)
    assert chains["entries"]["intact"] and chains["audit"]["intact"]
    # the first spend was in the ledger when the statement was signed; the second came after
    assert chains["entries"]["in_scope_anchored"] == 3
    assert chains["audit"]["in_scope_anchored"] == 2


def test_a_history_rewritten_after_the_statement_proves_nothing(ledger_with_a_statement, tmp_path):
    db, statement = ledger_with_a_statement
    pack = make_pack(db, tmp_path / "incident.json", agent="deployer")
    heads = incident.heads_of(json.loads(statement.read_text())["statement"])
    assert incident.check(pack, heads)["entries"]["in_scope_anchored"] == 3

    # Lower the first charge and recompute every hash after it, as someone rewriting would.
    chain = pack["chains"]["entries"]
    amount = chain["columns"].index("amount") + 1
    prev = chain["prev"]
    for i, row in enumerate(chain["rows"]):
        if i == 0:
            row[amount] = 1
        row[-1] = prev = chain_hash(prev, tuple(row[1:-1]))
    result = incident.check(pack, heads)["entries"]
    assert result["intact"] and result["anchored_through"] is None and result["in_scope_anchored"] == 0

    # Edited without recomputing, the chain itself breaks.
    pack = make_pack(db, tmp_path / "again.json", agent="deployer")
    pack["chains"]["entries"]["rows"][1][amount] = 1
    assert incident.check(pack, heads)["entries"]["first_bad_id"] == pack["chains"]["entries"]["rows"][1][0]


def test_a_task_scope_and_a_signed_statement_without_the_key(
        ledger_with_a_statement, tmp_path, monkeypatch):
    db, statement = ledger_with_a_statement
    pack = make_pack(db, tmp_path / "incident.json", task="t1")
    assert len(pack["in_scope"]["entries"]) == 6 and len(pack["in_scope"]["audit"]) == 2
    monkeypatch.delenv("QUARTER_SIGNING_KEY")
    with pytest.raises(SystemExit, match="public key"):
        cli.main(["check-incident", str(tmp_path / "incident.json"), "--statement", str(statement)])
    monkeypatch.setenv("QUARTER_SIGNING_KEY", receipts.public_key("ed25519:" + "08" * 32))
    with pytest.raises(SystemExit, match="does not match"):
        cli.main(["check-incident", str(tmp_path / "incident.json"), "--statement", str(statement)])
