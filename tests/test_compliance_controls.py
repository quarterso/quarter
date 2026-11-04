import json
import zipfile
from datetime import UTC, datetime

import pytest

from quarter_ledger.evidence import audit_pack
from quarter_ledger.policy import Cap
from quarter_ledger.store import Ledger

MONTH = f"{datetime.now(UTC):%Y-%m}"
PEOPLE = {"Ann": "approver", "Fay": "finance", "Ada": "admin"}


def seeded(tmp_path) -> Ledger:
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.record_config({"caps": [], "people": PEOPLE})
    ledger.record_config({"caps": [{"agent": "bot"}], "people": PEOPLE})
    ledger.set_owner("bot", "Dana")
    caps = [Cap(scope="agent", key="bot", window="total", limit=100, approve_above=1)]
    asked = [ledger.hold(caps, {"agent": "bot"}, "openai", "m", 5)[1] for _ in range(3)]
    with pytest.raises(PermissionError):
        ledger.decide_approval(asked[0], True, by="Dana")
    ledger.decide_approval(asked[0], True, by="Ann")
    ledger.decide_approval(asked[1], False, by="Ann")
    # Fixed waits so the median is known: 60 s and 180 s; the third stays pending.
    for approval, seconds in ((asked[0], 60), (asked[1], 180)):
        ledger.db.execute(
            "update approvals set created_at = ?, decided_at = ? where id = ?",
            (f"{MONTH}-01T10:00:00+00:00", f"{MONTH}-01T10:{seconds // 60:02d}:00+00:00", approval),
        )
    ledger.pause("bot", "sent the same request 12 times in 2 minutes")
    ledger.resume("bot", "Ann")
    ledger.pause("bot", "spent $4 in 10 minutes, 20x its usual rate")
    ledger.resume("bot", "Ann")
    ledger.pause("bot", "made the same tool call 8 times in 5 minutes", task="t1")
    ledger.audit("budget_alert", None, "agent:bot:80")
    ledger.audit("role_refused", "Fay", "finance POST /approvals")
    ledger.audit("login_failed", "1.2.3.4", "/dashboard")
    return ledger


def read_pack(path):
    z = zipfile.ZipFile(path)
    return z.read("controls.md").decode(), json.loads(z.read("controls.json"))


def test_seeded_ledger_gives_expected_counts(tmp_path):
    ledger, out = seeded(tmp_path), str(tmp_path / "pack.zip")
    audit_pack(ledger, MONTH, out, retention_days=2555, people=PEOPLE)
    md, data = read_pack(out)
    facts = data["facts"]

    assert facts["access"]["roles"] == {"admin": 1, "approver": 1, "finance": 1}
    assert facts["access"]["refusals"]["role_refused"] == 1
    assert facts["access"]["refusals"]["login_failed"] == 1
    assert facts["access"]["self_approval_refused"] == 1
    assert facts["monitoring"] == {
        "pauses": 3, "loop_pauses": 2, "runaway_pauses": 1, "budget_alerts": 1,
    }
    assert facts["change"]["config_changes"] == 2
    assert facts["change"]["sections_changed"] == ["caps", "people"]
    assert facts["change"]["owner_changes"] == 1
    assert facts["approvals"]["count"] == 3
    assert facts["approvals"]["by_status"] == {"approved": 1, "denied": 1, "pending": 1}
    assert facts["approvals"]["decided_by"] == {"Ann": 2}
    assert facts["approvals"]["median_seconds_to_decision"] == 120
    assert [w.split(" (")[0].split(".")[0] for w in data["warnings"]] == [
        "The month's 10 exception(s) have no review that matches them as they stand",
        "2 config change(s) this month lack sign-off by 2 people",
    ]

    assert md.splitlines()[2].startswith("Quarter provides evidence for these controls. It does not certify")
    assert "### Art. 26(6): Deployer keeps logs at least six months" in md
    assert "Config changes: 2 (sections: caps, people); agent owner changes: 1" in md
    assert "receipts.jsonl" not in md  # no signing key, so the pack has no receipts
    assert "\u2014" not in md and "\u2013" not in md
    refs = {c["ref"] for c in data["controls"]}
    assert {"CC6.1", "CC6.2", "CC6.3", "CC7.2", "CC8.1", "Art. 12(1)", "Art. 19(1)",
            "Art. 26(2)", "Art. 26(6)"} <= refs


@pytest.mark.parametrize(("days", "warned"), [(None, "No retention period is set"),
                                              (90, "Retention is 90 days"),
                                              (183, "Retention is 183 days"),
                                              (184, None)])
def test_short_or_missing_retention_is_warned(tmp_path, days, warned):
    out = str(tmp_path / "pack.zip")
    summary = audit_pack(Ledger(str(tmp_path / "l.db")), MONTH, out, retention_days=days, people={})
    md, _ = read_pack(out)
    if warned:
        assert warned in md and "## Warnings" in md
        assert any(warned in w for w in summary["warnings"])
    else:
        assert "## Warnings" not in md


def test_broken_chain_is_reported_not_hidden(tmp_path):
    ledger, out = seeded(tmp_path), str(tmp_path / "pack.zip")
    ledger.db.execute("update audit set detail = 'edited' where action = 'budget_alert'")
    summary = audit_pack(ledger, MONTH, out, retention_days=2555, people=PEOPLE)
    md, data = read_pack(out)
    assert not data["facts"]["integrity"]["audit"]["intact"]
    assert "The audit hash chain is BROKEN at row" in md
    assert "a hash chain is BROKEN" in md
    assert summary["warnings"][0].startswith("The audit hash chain is BROKEN")


def test_cli_pack_lists_roles_but_never_keys(tmp_path, monkeypatch):
    from quarter_ledger.cli import main

    db, out = str(tmp_path / "l.db"), str(tmp_path / "pack.zip")
    monkeypatch.setenv("QUARTER_PEOPLE", json.dumps({"Ann": {"key": "secret-key-1", "role": "approver"}}))
    main(["--db", db, "audit-pack", "--month", MONTH, "--out", out])
    md, data = read_pack(out)
    assert data["facts"]["access"]["people"] == {"Ann": "approver"}
    assert "secret-key-1" not in md + json.dumps(data)
    assert "No retention period is set" in md


def test_aiuc1_controls_sit_next_to_what_the_gateway_did(tmp_path):
    ledger = seeded(tmp_path)
    now = datetime.now(UTC).isoformat()
    base = {"at": now, "vendor": "anthropic", "route": "api", "fingerprint": "f", **dict.fromkeys(
        ("input", "output", "cache_read", "cache_write", "cost", "list_cost", "tools_cost", "system_cost",
         "history_cost", "output_cost", "break_cost", "saved"), 0)}
    ledger.record_call({**base, "call_id": "c1", "redacted": "card:1,email:2"})
    ledger.record_call({**base, "call_id": "c2", "redacted": "email:1", "upstream": "anthropic-eu"})
    ledger.audit("feed_applied", None, json.dumps({"version": 3}))
    out = tmp_path / "pack.zip"
    audit_pack(ledger, MONTH, out, retention_days=2555, people=PEOPLE,
               rules=[{"tool": "Bash", "command": "drop", "action": "approve", "why": "drops a table"}])
    md, data = read_pack(out)
    assert data["facts"]["data"] == {"masked_values": {"card": 1, "email": 3},
                                     "calls_by_destination": {"anthropic": 1, "anthropic-eu": 1},
                                     "feed_versions": [3]}
    assert md.index("## AIUC-1") < md.index("## SOC 2")
    assert "### D003: Restrict unsafe tool calls" in md
    assert "- Quarter feed in force: version 3" in md
    assert "- rule 1: approve, drops a table" in md
    assert "Values masked before leaving for a model, by kind: card 1, email 3" in md
    assert {"A006", "B006", "B007", "C007", "C009", "D003", "E004", "E009", "E011", "E015"} <= {
        c["ref"] for c in data["controls"] if c["framework"] == "AIUC-1"}


def test_signed_reviewed_and_anchored_month_has_no_warnings(tmp_path):
    from quarter_ledger import anchor
    from quarter_ledger.controls import review_exceptions
    from quarter_ledger.store import month_range

    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.record_config({"caps": []})
    ledger.sign_config("Ann")
    with pytest.raises(ValueError, match="already signed"):
        ledger.sign_config("Ann")
    ledger.sign_config("Fay")
    ledger.record_config({"caps": [{"agent": "bot"}]})
    ledger.sign_config("Ann")
    caps = [Cap(scope="agent", key="bot", window="total", limit=100, approve_above=1)]
    ledger.decide_approval(ledger.hold(caps, {"agent": "bot"}, "openai", "m", 5)[1], True, by="Ann")
    out = str(tmp_path / "pack.zip")
    first = audit_pack(ledger, MONTH, out, retention_days=2555, people=PEOPLE)
    assert any("1 config change(s)" in w for w in first["warnings"])  # the second has one signer
    ledger.sign_config("Fay")
    anchor.make(ledger, str(tmp_path / "anchor.json"))
    start, end = month_range(MONTH)
    review_exceptions(ledger, MONTH, (start.isoformat(), end.isoformat()), "Fay")
    summary = audit_pack(ledger, MONTH, out, retention_days=2555, people=PEOPLE)
    assert summary["warnings"] == []
    md, _ = read_pack(out)
    rows = zipfile.ZipFile(out).read("exceptions.csv").decode().splitlines()
    assert rows[0] == "at,kind,subject,detail" and ",approval_approved,bot," in rows[1]
    assert "## COSO" in md and "### P11: Technology controls" in md
    assert "Config changes signed off by 2 people: 2 of 2" in md
    assert "reviewed as they stand by: Fay" in md


def test_config_sign_and_exceptions_review_from_the_command_line(tmp_path, capsys):
    from quarter_ledger.cli import main

    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    with pytest.raises(SystemExit, match="no config has been recorded"):
        main(["--db", db, "config-sign", "--as", "Ann"])
    ledger.record_config({"caps": []})
    main(["--db", db, "config-sign", "--as", "Ann"])
    assert "signed off by Ann; 1 more person needed" in capsys.readouterr().out
    main(["--db", db, "config-sign", "--as", "Fay"])
    assert "signed off by Ann, Fay; authorised" in capsys.readouterr().out
    caps = [Cap(scope="agent", key="bot", window="total", limit=100, approve_above=1)]
    ledger.decide_approval(ledger.hold(caps, {"agent": "bot"}, "openai", "m", 5)[1], False, by="Ann")
    month = ledger.db.execute("select substr(created_at, 1, 7) from approvals").fetchone()[0]
    main(["--db", db, "exceptions", "--month", month])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "at,kind,subject,detail" and ",approval_denied,bot," in lines[1]
    main(["--db", db, "exceptions", "--month", month, "--review", "--as", "Fay"])
    assert capsys.readouterr().out.strip() == f"1 exception(s) for {month} reviewed by Fay"


def test_config_sign_and_exceptions_review_over_the_gateway(tmp_path):
    import urllib.error
    import urllib.request

    from test_proxy import ADMIN_KEY, PRICES, start

    from quarter_ledger.proxy import Gateway, serve

    ledger = Ledger(str(tmp_path / "l.db"))
    proxy = start(serve(Gateway(ledger, [], PRICES, {}, set(), ADMIN_KEY), port=0))

    def post(path, key=ADMIN_KEY):
        req = urllib.request.Request(f"http://127.0.0.1:{proxy.server_port}{path}", data=b"",
                                     headers={"x-quarter-key": key} if key else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert post("/config/sign", key=None)[0] == 401
        ledger.record_config({"caps": []})
        status, body = post("/config/sign")
        assert status == 200 and body["authorised"] is False and len(body["signed_by"]) == 1
        assert post("/config/sign")[0] == 409  # the same person cannot sign twice
        status, body = post("/exceptions/review")
        assert status == 200 and body["rows"] == 0 and body["digest"]
        assert post("/exceptions/review?month=bad")[0] == 400
    finally:
        proxy.shutdown()
