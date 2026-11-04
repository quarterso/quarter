import io
import json
import urllib.error
import zipfile
from datetime import UTC, datetime

import pytest

from quarter_ledger import anchor, cli
from quarter_ledger.store import CHAINED, Ledger, chain_hash

MONTH = datetime.now(UTC).strftime("%Y-%m")
PENDING_REPLY = b"\xf0\x10" + b"n" * 16 + b"\x08\x00" + anchor.PENDING + b"\x10\x0fhttps://calendar"
COMPLETE_REPLY = b"\x08\x00" + anchor.BITCOIN + b"\x03\xa0\x8d\x06"


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def calendar(monkeypatch):
    """No test reaches the network: every urlopen in anchor.py lands here."""
    sent = []

    def urlopen(req, timeout):
        sent.append((req, timeout))
        reply = calendar.reply
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply)

    calendar.reply = PENDING_REPLY
    calendar.sent = sent
    monkeypatch.setattr(anchor.urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("QUARTER_ANCHOR_CALENDAR", raising=False)
    return calendar


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # verify opens --db's default, ledger.db, as every command does
    path = tmp_path / "ledger.db"
    ledger = Ledger(str(path))
    for amount in (100, 200, 300):
        _, call_id = ledger.hold([], {"agent": "a"}, "anthropic", "m", amount)
        ledger.settle(call_id, amount)
    ledger.audit("credits", "cli", "a 5")
    return path


def run(capsys, *args):
    capsys.readouterr()
    cli.main(list(args))
    return capsys.readouterr()


def verify(capsys, export, anchor_file):
    capsys.readouterr()
    try:
        cli.main(["anchor", "verify", str(export), "--anchor", str(anchor_file)])
        code = 0
    except SystemExit as e:
        code = e.code
    return code, json.loads(capsys.readouterr().out)


def test_anchor_is_off_by_default_and_recorded_in_the_ledger(db, tmp_path, calendar, capsys):
    out = tmp_path / "anchor.json"
    run(capsys, "--db", str(db), "anchor", "--out", str(out))
    record = json.loads(out.read_text())
    assert calendar.sent == []
    assert record["ots"] == {"status": "off"}
    assert record["heads"]["entries"]["id"] == 9 and record["heads"]["entries"]["rows"] == 9
    assert record["heads"]["audit"]["rows"] == 1
    assert record["digest"] == anchor.digest(record["heads"])
    ledger = Ledger(str(db))
    row = ledger.db.execute("select detail from audit where action = 'anchor'").fetchone()
    assert json.loads(row[0])["digest"] == record["digest"]
    assert ledger.verify_chain()["audit"]["intact"]
    assert not (tmp_path / "anchor.ots").exists()


def test_verify_finds_the_head_in_a_backup_an_audit_pack_and_an_incident_pack(db, tmp_path, calendar, capsys):
    out = tmp_path / "anchor.json"
    run(capsys, "--db", str(db), "anchor", "--out", str(out))
    ledger = Ledger(str(db))
    _, call_id = ledger.hold([], {"agent": "a"}, "anthropic", "m", 50)  # after the anchor
    ledger.settle(call_id, 50)
    pack, incident = tmp_path / "pack.zip", tmp_path / "incident.json"
    run(capsys, "--db", str(db), "audit-pack", "--month", MONTH, "--out", str(pack))
    run(capsys, "--db", str(db), "incident", "--from", "2020-01-01", "--to", "2100-01-01", "--out", str(incident))
    for export in (db, pack, incident):
        code, result = verify(capsys, export, out)
        assert code == 0 and result["ok"], export
        assert result["chains"]["entries"]["found"] and result["chains"]["audit"]["found"]
    _, result = verify(capsys, db, out)
    assert result["chains"]["entries"]["rows_match"] is True
    _, result = verify(capsys, pack, out)
    assert result["chains"]["entries"]["rows_match"] is None  # a month's pack: the count is not checkable
    assert result["ots"] == {"status": "off"}


def rewrite_chain(path, table, row_id, column, value):
    """What someone with write access could do: edit a row and recompute every hash."""
    ledger = Ledger(str(path))
    ledger.db.execute(f"update {table} set {column} = ? where id = ?", (value, row_id))
    prev, columns = "", CHAINED[table]
    for each, *values in ledger.db.execute(
        f"select id, {', '.join(columns)} from {table} order by id"
    ).fetchall():
        prev = chain_hash(prev, tuple(values))
        ledger.db.execute(f"update {table} set hash = ? where id = ?", (prev, each))
    assert ledger.verify_chain()[table]["intact"]


def test_a_rewritten_history_does_not_hold_the_anchored_head(db, tmp_path, calendar, capsys):
    out = tmp_path / "anchor.json"
    run(capsys, "--db", str(db), "anchor", "--out", str(out))
    rewrite_chain(db, "entries", 2, "amount", 1)
    code, result = verify(capsys, db, out)
    assert code == 1 and not result["ok"]
    assert result["chains"]["entries"]["found"] is False
    assert result["chains"]["audit"]["found"] is True


def test_an_edited_audit_pack_is_caught(db, tmp_path, calendar, capsys):
    out, pack, edited = tmp_path / "anchor.json", tmp_path / "pack.zip", tmp_path / "edited.zip"
    run(capsys, "--db", str(db), "anchor", "--out", str(out))
    run(capsys, "--db", str(db), "audit-pack", "--month", MONTH, "--out", str(pack))
    with zipfile.ZipFile(pack) as src, zipfile.ZipFile(edited, "w") as dst:
        for name in src.namelist():
            data = src.read(name).decode()
            if name == "entries.csv":
                data = data.replace(",300,", ",1,", 1)  # not the first row: its own link is not in the pack
            dst.writestr(name, data)
    code, result = verify(capsys, edited, out)
    assert code == 1 and not result["chains"]["entries"]["intact"]


def test_an_anchor_edited_after_it_was_made_is_caught(db, tmp_path, calendar, capsys):
    out = tmp_path / "anchor.json"
    run(capsys, "--db", str(db), "anchor", "--out", str(out))
    record = json.loads(out.read_text())
    record["heads"]["entries"]["rows"] = 5
    out.write_text(json.dumps(record))
    code, result = verify(capsys, db, out)
    assert code == 1 and result["digest_matches"] is False


def test_submit_sends_only_the_digest_and_keeps_a_pending_proof(db, tmp_path, calendar, capsys):
    out = tmp_path / "anchor.json"
    printed = run(capsys, "--db", str(db), "anchor", "--out", str(out), "--submit").out
    record = json.loads(out.read_text())
    (req, timeout), = calendar.sent
    assert req.full_url == "https://a.pool.opentimestamps.org/digest"
    assert req.data == bytes.fromhex(record["digest"]) and len(req.data) == 32
    assert req.get_header("Accept") == "application/vnd.opentimestamps.v1"
    assert timeout == anchor.TIMEOUT
    assert record["ots"] == {"status": "submitted", "calendar": anchor.CALENDAR, "file": "anchor.ots"}
    proof = (tmp_path / "anchor.ots").read_bytes()
    assert proof == anchor.OTS_HEADER + req.data + PENDING_REPLY
    assert "ots upgrade anchor.ots" in printed
    _, result = verify(capsys, db, out)
    assert result["ots"]["status"] == "pending"
    assert f"ots verify -d {record['digest']}" in result["ots"]["next"]


def test_a_complete_proof_is_reported_complete(db, tmp_path, calendar, capsys, monkeypatch):
    calendar.reply = COMPLETE_REPLY
    monkeypatch.setenv("QUARTER_ANCHOR_CALENDAR", "https://calendar.example")
    out = tmp_path / "anchor.json"
    run(capsys, "--db", str(db), "anchor", "--out", str(out))
    assert calendar.sent[0][0].full_url == "https://calendar.example/digest"
    _, result = verify(capsys, db, out)
    assert result["ots"]["status"] == "complete"
    assert result["ots"]["next"].startswith("ots verify -d ")


@pytest.mark.parametrize("reply", [
    urllib.error.URLError("no route"), TimeoutError("timed out"), b"<html>busy</html>",
])
def test_a_failed_submission_is_recorded_not_submitted(db, tmp_path, calendar, capsys, reply):
    calendar.reply = reply
    out = tmp_path / "anchor.json"
    printed = run(capsys, "--db", str(db), "anchor", "--out", str(out), "--submit")
    record = json.loads(out.read_text())
    assert record["ots"]["status"] == "not submitted" and record["ots"]["reason"]
    assert "not submitted" in printed.err
    assert not (tmp_path / "anchor.ots").exists()
    detail = Ledger(str(db)).db.execute("select detail from audit where action = 'anchor'").fetchone()[0]
    assert json.loads(detail)["ots"]["status"] == "not submitted"
    code, result = verify(capsys, db, out)
    assert code == 0 and result["ots"]["status"] == "not submitted"


def test_verify_refuses_a_file_it_cannot_read(db, tmp_path, calendar, capsys):
    out, other = tmp_path / "anchor.json", tmp_path / "other.json"
    run(capsys, "--db", str(db), "anchor", "--out", str(out))
    other.write_text("{}")
    with pytest.raises(SystemExit, match="cannot check"):
        cli.main(["anchor", "verify", str(other), "--anchor", str(out)])
