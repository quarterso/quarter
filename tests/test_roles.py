import base64
import json
import zipfile
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, FakeVendor, call, start

from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

PEOPLE = {
    "Vic": {"key": "viewer-key", "role": "viewer"},
    "Ann": {"key": "approver-key", "role": "approver"},
    "Fay": {"key": "finance-key", "role": "finance"},
    "Ada": {"key": "admin-person-key", "role": "admin"},
}
BIG = {"model": "m", "max_tokens": 12_000}  # over the approval line


def as_(name):
    return {"x-quarter-key": PEOPLE[name]["key"]}


@pytest.fixture
def team(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    FakeVendor.calls = []
    ledger = Ledger(str(tmp_path / "ledger.db"))
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros("0.02"))]
    base = f"http://127.0.0.1:{vendor.server_port}"
    gw = Gateway(ledger, caps, PRICES, {"fake": base}, {AGENT_KEY}, ADMIN_KEY, people=PEOPLE)
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", ledger
    proxy.shutdown()
    vendor.shutdown()


COST = {"category": "seat", "vendor": "x", "amount_usd": "1", "month": "2026-10"}


def waiting(url):
    return json.loads(call(url + "/fake/v1/chat", BIG)[1])["approval_id"]


def test_viewer_reads_but_cannot_decide_or_record(team):
    url, ledger = team
    apr = waiting(url)
    for path in ("/approvals", "/audit", "/forecast"):
        assert call(url + path, headers=as_("Vic"))[0] == 200, path
    assert call(url + f"/approvals/{apr}/approve", {}, as_("Vic"))[0] == 403
    assert call(url + "/costs", COST, as_("Vic"))[0] == 403
    ledger.pause("a1", "loop")
    assert call(url + "/agents/a1/resume", {}, as_("Vic"))[0] == 403
    assert ledger.approval(apr)["status"] == "pending" and ledger.paused("a1")
    refused = [r for r in ledger.audit_log() if r["action"] == "role_refused"]
    assert len(refused) == 3 and refused[0]["client"] == "Vic@127.0.0.1"


def test_approver_decides_but_cannot_record(team):
    url, ledger = team
    apr = waiting(url)
    assert call(url + f"/approvals/{apr}/approve", {}, as_("Ann"))[0] == 200
    assert ledger.approval(apr)["decided_by"] == "Ann"
    assert call(url + "/costs", COST, as_("Ann"))[0] == 403
    ledger.pause("a1", "loop")
    assert call(url + "/agents/a1/resume", {}, as_("Ann"))[0] == 200
    assert ledger.db.execute("select resumed_by from pauses").fetchone()[0] == "Ann"


def test_finance_records_and_decides(team):
    url, ledger = team
    assert call(url + "/costs", COST, as_("Fay"))[0] == 200
    apr = waiting(url)
    assert call(url + f"/approvals/{apr}/deny", {}, as_("Fay"))[0] == 200
    clients = {r["action"]: r["client"] for r in ledger.audit_log()}
    assert clients["cost"] == clients["deny"] == "Fay@127.0.0.1"


def test_admin_person_and_legacy_admin_key_do_everything(team):
    url, ledger = team
    assert call(url + "/costs", COST, as_("Ada"))[0] == 200
    legacy = {"x-quarter-key": ADMIN_KEY}
    assert call(url + "/costs", COST, legacy)[0] == 200
    assert ledger.audit_log()[0]["client"] == "admin@127.0.0.1"
    apr = waiting(url)
    assert call(url + f"/approvals/{apr}/approve", {}, legacy)[0] == 200
    assert ledger.approval(apr)["decided_by"] == "admin"
    named = waiting(url)  # the shared key may still name the approver
    sam = {**legacy, "x-quarter-approver": "Sam"}
    assert call(url + f"/approvals/{named}/approve", {}, sam)[0] == 200
    assert ledger.approval(named)["decided_by"] == "Sam"


def test_approver_header_must_match_the_person(team):
    url, ledger = team
    apr = waiting(url)
    other = {**as_("Ann"), "x-quarter-approver": "Fay"}
    assert call(url + f"/approvals/{apr}/approve", {}, other)[0] == 403
    assert ledger.approval(apr)["status"] == "pending"
    assert ledger.audit_log()[0]["action"] == "approver_mismatch"
    same = {**as_("Ann"), "x-quarter-approver": "ann"}
    assert call(url + f"/approvals/{apr}/approve", {}, same)[0] == 200


def test_owner_rule_still_applies_to_a_person(team):
    url, ledger = team
    ledger.set_owner("a1", "Ann")
    apr = waiting(url)
    assert call(url + f"/approvals/{apr}/approve", {}, as_("Ann"))[0] == 403
    assert call(url + f"/approvals/{apr}/deny", {}, as_("Ann"))[0] == 200


def test_dashboard_names_the_person_and_hides_their_missing_buttons(team):
    url, ledger = team
    waiting(url)
    ledger.pause("a1", "loop")

    def page(name):
        token = base64.b64encode(f"x:{PEOPLE[name]['key']}".encode()).decode()
        status, body = call(url + "/dashboard", headers={"authorization": f"Basic {token}"})
        assert status == 200
        return body.decode()

    viewer, approver = page("Vic"), page("Ann")
    assert "Signed in as Vic (viewer)" in viewer
    assert "Approve" not in viewer and "Resume</button>" not in viewer
    assert "Signed in as Ann (approver)" in approver
    assert "Approve</button>" in approver and "Resume</button>" in approver


def test_unknown_key_and_agent_key_are_not_people(team):
    url, _ = team
    assert call(url + "/approvals", headers={"x-quarter-key": "nobody"})[0] == 401
    assert call(url + "/approvals", headers=HEADERS)[0] == 401


def test_people_need_a_role_and_a_key_of_their_own(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    bad = (
        {"Bo": {"key": "k" * 30, "role": "owner"}},
        {"Bo": {"role": "viewer"}},
        {"Bo": {"key": "k" * 30, "role": "viewer"}, "Cy": {"key": "k" * 30, "role": "admin"}},
        {"Bo": {"key": ADMIN_KEY, "role": "viewer"}},
        {"Bo": {"key": AGENT_KEY, "role": "viewer"}},
        {"admin": {"key": "k" * 30, "role": "admin"}},
    )
    for people in bad:
        with pytest.raises(ValueError):
            Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, people=people)


def test_retention_is_recorded_in_the_audit_pack(tmp_path, monkeypatch):
    from quarter_ledger.cli import main

    db, out = str(tmp_path / "l.db"), str(tmp_path / "pack.zip")
    monkeypatch.setenv("QUARTER_RETENTION_DAYS", "2555")
    main(["--db", db, "audit-pack", "--month", f"{datetime.now(UTC):%Y-%m}", "--out", out])
    assert json.loads(zipfile.ZipFile(out).read("chain.json"))["retention_days"] == 2555
    monkeypatch.setenv("QUARTER_RETENTION_DAYS", "seven years")
    with pytest.raises(SystemExit):
        main(["--db", db, "audit-pack", "--month", "2026-10", "--out", out])


def test_serve_loads_people_and_audits_roles_and_retention_without_keys(tmp_path, monkeypatch):
    from quarter_ledger import cli

    monkeypatch.setenv("QUARTER_PEOPLE", json.dumps(PEOPLE))
    monkeypatch.setenv("QUARTER_RETENTION_DAYS", "2555")
    started, polls = [], []

    class Server:
        def serve_forever(self, poll_interval=0.5):
            polls.append(poll_interval)

    monkeypatch.setattr(cli, "serve", lambda gw, host, port: started.append(gw) or Server())
    db = str(tmp_path / "l.db")
    cli.main(["--db", db, "serve"])
    assert started[0].person("approver-key") == ("Ann", "approver")
    log = Ledger(db).db.execute("select detail from audit").fetchall()
    assert any("people" in d and "retention_days" in d for (d,) in log)
    assert not any("approver-key" in d for (d,) in log)
