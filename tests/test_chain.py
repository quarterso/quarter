from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger


def test_chain_detects_edits_and_deletes(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for _ in range(3):
        _, ref = ledger.hold([], {"agent": "a"}, "v", "m", to_micros(1))
        ledger.settle(ref, to_micros("0.5"))
    ledger.audit("credits", "1.2.3.4", "a 5")
    ok = ledger.verify_chain()
    assert ok["entries"] == {"checked": 9, "intact": True, "first_bad_id": None}
    assert ok["audit"]["intact"]

    ledger.db.execute("update entries set amount = 1 where id = 6")
    assert ledger.verify_chain()["entries"]["first_bad_id"] == 6
    ledger.db.execute("update entries set amount = ? where id = 6", (to_micros("0.5"),))
    assert ledger.verify_chain()["entries"]["intact"]
    ledger.db.execute("delete from entries where id = 5")
    assert ledger.verify_chain()["entries"]["first_bad_id"] == 6


def test_old_database_gains_the_columns(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute(
        "create table entries (id integer primary key, call_id text not null, kind text not null,"
        " at text not null, agent text, task text, customer text, vendor text not null,"
        " model text, amount integer not null)"
    )
    db.execute(
        "insert into entries (call_id, kind, at, vendor, amount) values ('x','hold','t','v',1)"
    )
    db.commit()
    db.close()
    ledger = Ledger(path)
    ledger.hold([], {"agent": "a"}, "v", "m", 1)
    assert ledger.verify_chain()["entries"] == {"checked": 1, "intact": True, "first_bad_id": None}


def test_audit_pack_zip(tmp_path):
    import json
    import zipfile
    from datetime import UTC, datetime

    from quarter_ledger.evidence import audit_pack
    from quarter_ledger.policy import Cap

    ledger = Ledger(str(tmp_path / "l.db"))
    _, ref = ledger.hold([], {"agent": "a", "customer": "acme"}, "v", "m", to_micros(1))
    ledger.settle(ref, to_micros("0.4"))
    ledger.audit("credits", "cli", "a 5")
    out = str(tmp_path / "pack.zip")
    month = f"{datetime.now(UTC):%Y-%m}"
    summary = audit_pack(ledger, month, out, [Cap("agent", "a", "day", 1)], "k" * 32)
    names = set(zipfile.ZipFile(out).namelist())
    assert {
        "README.txt",
        "entries.csv",
        "approvals.csv",
        "audit_log.csv",
        "pauses.csv",
        "caps.json",
        "journal.csv",
        "chain.json",
        "receipts.jsonl",
        "controls.md",
        "controls.json",
        "approval_record/README.txt",
        "approval_record/approvals.csv",
        "approval_record/chain.json",
    } == names
    assert summary["chain"]["entries"]["intact"] and summary["heads"]["entries"]["hash"]
    chain = json.loads(zipfile.ZipFile(out).read("chain.json"))
    assert chain["chain"]["entries"]["checked"] == 3
    from quarter_ledger.evidence import _csv, _query

    pack = zipfile.ZipFile(out)  # entries.csv is streamed: the same bytes, mode and order as the rest
    assert pack.read("entries.csv").decode() == _csv(*reversed(_query(ledger, "select * from entries order by id")))
    assert pack.getinfo("entries.csv").external_attr == pack.getinfo("README.txt").external_attr
    assert pack.namelist()[:2] == ["README.txt", "entries.csv"] and pack.testzip() is None
