import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_issuing import post, request
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, call, start

from quarter_ledger import bills, sanctions
from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

FIXTURES = Path(__file__).parent / "fixtures" / "ofac"
WALLET = "0xAbC0000000000000000000000000000000000001"


def opener(files: dict[str, bytes]):
    """urlopen for update(): OFAC's files by name, no network."""
    def urlopen(url, timeout):
        return io.BytesIO(files[url.rsplit("/", 1)[1]])
    return urlopen


def fixture_files() -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in FIXTURES.glob("*.XML")}


@pytest.fixture
def screen(tmp_path):
    folder = str(tmp_path / "lists")
    sanctions.update(folder, opener(fixture_files()))
    return sanctions.Screen(folder)


def age(screen: sanctions.Screen, days: int) -> None:
    """Make the stored lists look downloaded `days` ago."""
    meta = Path(screen.folder, "lists.json")
    lists = json.loads(meta.read_text())
    for info in lists.values():
        info["fetched"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    meta.write_text(json.dumps(lists))
    os.utime(meta, ns=(1, 1))  # a new stamp, so the screen reads it again


def screens(ledger: Ledger) -> list[dict]:
    return [json.loads(a["detail"]) for a in ledger.audit_log() if a["action"] == "sanctions_screen"]


def test_names_compare_without_case_accents_punctuation_suffixes_or_order():
    assert sanctions.words("Banco Nacional de Cuba, S.A.") == ["banco", "cuba", "de", "nacional"]
    assert sanctions.words("ALI, Hassan") == sanctions.words("Hassan Ali")
    assert sanctions.words("Société Générale") == ["generale", "societe"]
    assert sanctions.words("Acme Holdings LLC") == sanctions.words("ACME HOLDINGS, L.L.C.")


def test_update_keeps_both_lists_with_publish_date_and_hash(tmp_path):
    folder = tmp_path / "lists"
    lists = sanctions.update(str(folder), opener(fixture_files()))
    assert lists["sdn"]["published"] == "2026-10-28" and lists["sdn"]["records"] == 4
    assert lists["consolidated"]["published"] == "2026-10-19"
    assert lists["sdn"]["url"] == "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
    assert len(lists["sdn"]["sha256"]) == 64
    assert json.loads((folder / "lists.json").read_text()) == lists
    assert (folder / "sdn.xml").read_bytes() == (FIXTURES / "SDN.XML").read_bytes()


def test_a_bad_download_leaves_the_lists_kept_before(tmp_path):
    folder = str(tmp_path / "lists")
    sanctions.update(folder, opener(fixture_files()))
    before = Path(folder, "lists.json").read_text()
    short = fixture_files() | {"SDN.XML": (FIXTURES / "SDN.XML").read_bytes().replace(b"<Record_Count>4", b"<Record_Count>9")}
    with pytest.raises(ValueError, match="says 9 records, holds 4"):
        sanctions.update(folder, opener(short))
    with pytest.raises(ValueError, match="not an OFAC list"):
        sanctions.update(folder, opener(fixture_files() | {"SDN.XML": b"<html></html>"}))
    assert Path(folder, "lists.json").read_text() == before


def test_matches(screen):
    bank = screen.check("National Bank of Cuba")  # a strong alias
    assert bank.outcome == "match" and bank.refused and bank.uid == "306" and bank.name == "BANCO NACIONAL DE CUBA"
    assert screen.check("banco nacional de cuba sa").refused
    close = screen.check("Banco Nacionale de Cuba S.A.")
    assert close.outcome == "close" and not close.refused and 0.9 <= close.score < 1
    person = screen.check("Ali Hassan")  # a common name: held for a person, never refused on the name alone
    assert person.outcome == "match" and person.type == "Individual" and not person.refused
    huawei = screen.check("Huawei Technologies")  # Consolidated: held, not refused
    assert huawei.outcome == "match" and huawei.list == "consolidated" and not huawei.refused
    wallet = screen.check(WALLET.lower())  # hex addresses in any case
    assert wallet.refused and wallet.name == "SUEX OTC, S.R.O."
    assert screen.check("1FakeXbtAddressForTests1111111").refused
    assert not screen.check("1fakexbtaddressfortests1111111").refused  # base58 is case sensitive
    for clear in ("BNC", "Northern Star", "OpenAI", "John Smith", "Bank of America", ""):
        assert screen.check(clear).outcome == "clear", clear  # weak aliases and vessels are left out


def test_every_screen_is_audited_with_the_lists_it_used(screen, tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    assert screen.review(ledger, "x402", WALLET, lambda: True)[0].startswith(f"{WALLET} matches the OFAC SDN list")
    refuse, ask = screen.review(ledger, "card", "Ali Hassan", lambda: True)
    assert refuse is None and "entry 6924 Hassan ALI" in ask
    assert screen.review(ledger, "card", "OpenAI", lambda: False) == (None, None)
    rows = screens(ledger)
    assert [r["outcome"] for r in rows] == ["clear", "held", "refused"]  # newest first
    assert rows[2]["uid"] == "24003" and rows[2]["published"] == {"sdn": "2026-10-28", "consolidated": "2026-10-19"}
    assert rows[1]["match"] == "match" and rows[1]["score"] == 1.0 and rows[0]["stale"] is False


def test_a_stale_list_holds_new_payees_and_lets_known_ones_through(screen, tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    age(screen, 8)
    assert screen.stale() and "STALE" in screen.summary()
    refuse, ask = screen.review(ledger, "card", "OpenAI", lambda: False)
    assert refuse is None and "last downloaded 8 days ago" in ask
    assert screen.review(ledger, "card", "OpenAI", lambda: True) == (None, None)
    assert screen.review(ledger, "x402", WALLET, lambda: True)[0]  # a match is refused all the same
    assert [r["stale"] for r in screens(ledger)] == [True, True, True]
    assert not sanctions.Screen(screen.folder, stale_days=30).stale()


def test_no_list_stored_screens_nothing_and_says_so(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    screen = sanctions.Screen(str(tmp_path / "none"))
    assert screen.review(ledger, "card", "anyone", lambda: False) == (None, None)
    assert screens(ledger) == [] and "not screened" in screen.summary()


@pytest.fixture
def gateway(tmp_path, screen):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    caps = [Cap("agent", "*", "day", to_micros(100), approve_above=to_micros(20))]
    gw = Gateway(ledger, caps, PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="k" * 32,
                 stripe_secret="whsec_test", sanctions=screen)
    server = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{server.server_port}", ledger, gw
    server.shutdown()


def pay(url, payee, headers=HEADERS):
    status, body = call(url + "/authorize", {"rail": "x402", "payee": payee, "amount_usd": "1"}, headers)
    return status, json.loads(body)


def test_payments_are_refused_or_held_on_a_match(gateway):
    url, ledger, _ = gateway
    status, body = pay(url, WALLET)
    assert status == 403 and body["error"] == "sanctions_match" and "24003" in body["reason"]
    status, body = pay(url, "0x1111111111111111111111111111111111111111")
    assert status == 200 and body["authorisation"]
    status, body = pay(url, "Ali Hassan")
    assert status == 403 and body["error"] == "approval_required" and "Hassan ALI" in body["reason"]
    ledger.decide_approval(body["approval_id"], True, "dana")  # a person checked: not this one
    status, body = pay(url, "Ali Hassan", {**HEADERS, "x-quarter-approval": body["approval_id"]})
    assert status == 200 and body["authorisation"]


def test_shadow_mode_does_not_pass_a_sanctions_match(gateway):
    url, _, gw = gateway
    gw.mode = "shadow"
    assert gw.shadowing.everything
    assert pay(url, WALLET)[1]["error"] == "sanctions_match"
    assert pay(url, "Ali Hassan")[1]["error"] == "approval_required"


def test_card_swipes_are_screened(gateway):
    url, _, _ = gateway
    status, body, _ = post(url, request(500, merchant="Banco Nacional de Cuba"))
    assert status == 200 and body["approved"] is False
    assert "OFAC SDN list entry 306" in body["metadata"]["quarter_reason"]
    status, body, _ = post(url, request(500, merchant="Huawei Technologies Co Ltd"))
    assert body["approved"] is False and body["metadata"]["quarter_approval"].startswith("apr_")
    assert post(url, request(500, merchant="OpenAI"))[1]["approved"] is True


def test_bills_are_screened(gateway):
    _, ledger, gw = gateway
    ids = {"agent": "ap", "task": None, "customer": None}
    b = bills.add(ledger, gw.caps, {}, ids, "National Bank of Cuba", "INV-1", to_micros(50), "123456789",
                  screen=gw.sanctions)
    assert b["status"] == "refused" and "OFAC SDN list entry 306" in b["reason"]
    b = bills.add(ledger, gw.caps, {}, ids, "Ali Hassan", "INV-2", to_micros(5), "123456789", screen=gw.sanctions)
    assert b["status"] == "waiting" and "Hassan ALI" in b["reason"]


def test_cli_check_and_status(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "l.db")
    sanctions.update(f"{db}.sanctions", opener(fixture_files()))
    main(["--db", db, "sanctions", "check", "Ali, Hassan"])
    out = capsys.readouterr().out
    assert "exact match, waits for a person" in out and "entry 6924" in out
    main(["--db", db, "sanctions", "check", "OpenAI"])
    assert "OpenAI: no match" in capsys.readouterr().out
    monkeypatch.setenv("QUARTER_SANCTIONS_STALE_DAYS", "0")
    age(sanctions.Screen(f"{db}.sanctions"), 1)
    main(["--db", db, "status"])
    assert "Sanctions: OFAC sdn 2026-10-28, consolidated 2026-10-19" in (out := capsys.readouterr().out)
    assert "STALE (over 0 days)" in out


def test_cli_update_audits_what_it_fetched(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "l.db")
    monkeypatch.setattr(sanctions.urllib.request, "urlopen", opener(fixture_files()))
    main(["--db", db, "sanctions", "update"])
    assert "sdn: published 2026-10-28, 4 records" in capsys.readouterr().out
    rows = [json.loads(a["detail"]) for a in Ledger(db).audit_log() if a["action"] == "sanctions_update"]
    assert {r["list"] for r in rows} == {"sdn", "consolidated"} and all(len(r["sha256"]) == 64 for r in rows)
