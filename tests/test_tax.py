import csv
import io
import json

import pytest

from quarter_ledger import bills, tax
from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger

IDS = {"agent": "ap", "task": None, "customer": None}
KEY = b"test key"
TIN = "12-3456789"


def paid(ledger, payee, invoice, usd, account="021000021 123456789"):
    b = bills.add(ledger, [], {}, IDS, payee, invoice, to_micros(usd), account)
    if b["status"] == "waiting":
        ledger.decide_approval(b["approval_id"], True, "dana")
    return bills.pay(ledger, [], b["id"], "sam", "manual", f"ref-{invoice}")


def test_thresholds_follow_the_year():
    assert tax.threshold(2024) == tax.threshold(2025) == to_micros(600)
    assert tax.threshold(2026) == tax.threshold(2027) == to_micros(2000)


def test_tax_details_keep_only_a_keyed_fingerprint_and_last_four(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    t = tax.record(ledger, "Acme", "2026-01-10", "individual", KEY, TIN, by="dana")
    assert t == {"w9_at": "2026-01-10", "entity": "individual", "tin_end": "6789", "pays_for": None}
    kept = ledger.db.execute("select tin from payee_accounts").fetchone()[0]
    assert kept.startswith("hmac-sha256:") and kept == tax.fingerprint("123456789", KEY)[0]
    assert kept != tax.fingerprint("123456789", b"other key")[0]
    assert ledger.audit_log()[0]["action"] == "payee_tax"
    with pytest.raises(ValueError, match="nine digits"):
        tax.record(ledger, "Acme", "2026-01-10", "individual", KEY, "12345")
    with pytest.raises(ValueError, match="TIN"):
        tax.record(ledger, "Acme", "2026-01-10", "c-corp", KEY)
    with pytest.raises(ValueError, match="entity"):
        tax.record(ledger, "Acme", "2026-01-10", "trust", KEY, TIN)
    assert tax.record(ledger, "Globex", "2026-02-01", "foreign", KEY)["tin_end"] is None


def test_tax_details_before_any_payment_still_ask_for_the_first_account(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    tax.record(ledger, "Acme", "2026-01-10", "individual", KEY, TIN)
    b = bills.add(ledger, [], {}, IDS, "Acme", "INV-1", to_micros(10), "021000021 123456789")
    assert b["status"] == "waiting" and "first payment" in b["reason"]
    assert "changed" not in b["reason"]
    ledger.decide_approval(b["approval_id"], True, "dana")
    assert bills.pay(ledger, [], b["id"], "sam", "manual", "r")["status"] == "paid"
    assert tax.details(ledger, "acme")["tin_end"] == "6789"  # paying kept the tax details


def test_no_w9_warns_on_approval_and_payment_but_never_blocks(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    b = bills.add(ledger, [], {}, IDS, "Acme", "INV-1", to_micros(10), "021000021 123456789")
    assert not any(r["action"] == "w9_missing" for r in ledger.audit_log())  # waiting, not approved
    ledger.decide_approval(b["approval_id"], True, "dana")
    assert bills.pay(ledger, [], b["id"], "sam", "manual", "r1")["status"] == "paid"
    warnings = [r for r in ledger.audit_log() if r["action"] == "w9_missing"]
    assert [w["detail"].split()[2] for w in reversed(warnings)] == ["approved", "paid"]
    assert warnings[0]["client"] == "sam" and warnings[1]["client"] == "dana"

    again = bills.add(ledger, [], {}, IDS, "Acme", "INV-2", to_micros(20), "021000021 123456789")
    assert again["status"] == "approved"
    assert ledger.audit_log()[0]["action"] == "w9_missing"
    assert [b["no_w9"] for b in bills.listed(ledger)] == [1, 1]

    tax.record(ledger, "ACME", "2026-01-10", "individual", KEY, TIN)
    assert [b["no_w9"] for b in bills.listed(ledger)] == [0, 0]
    bills.pay(ledger, [], again["id"], "sam", "manual", "r2")
    assert ledger.audit_log()[0]["action"] == "bill_paid"


def test_report_counts_paid_bills_in_the_year_and_says_who_is_reportable(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    tax.record(ledger, "Solo", "2026-01-10", "individual", KEY, TIN)
    tax.record(ledger, "BigCo", "2026-01-10", "c-corp", KEY, "98-7654321")
    tax.record(ledger, "LawCo", "2026-01-10", "s-corp", KEY, "11-1111111", pays_for="attorney")
    tax.record(ledger, "Parts", "2026-01-10", "llc-partnership", KEY, "22-2222222",
               pays_for="goods")
    paid(ledger, "solo", "S1", 1500)
    paid(ledger, "Solo", "S2", 600)  # the report names a payee as its latest bill did
    paid(ledger, "Small", "M1", 1999.99, "1111 2222")
    paid(ledger, "BigCo", "B1", 5000, "3333 4444")
    paid(ledger, "LawCo", "L1", 2500, "5555 6666")
    paid(ledger, "Parts", "P1", 9000, "7777 8888")
    paid(ledger, "Nobody", "N1", 3000, "9999 0000")
    bills.add(ledger, [], {}, IDS, "Pending", "X1", to_micros(9000), "1212 3434")
    old = paid(ledger, "Solo", "S0", 5000)
    with ledger._write():
        ledger.db.execute("update bills set paid_at = '2025-12-31T23:00:00+00:00' where id = ?",
                          (old["id"],))

    rows = {r["payee"]: r for r in tax.report(ledger, 2026)}
    assert set(rows) == {"Solo", "Small", "BigCo", "LawCo", "Parts", "Nobody"}
    assert rows["Solo"]["total"] == to_micros(2100) and rows["Solo"]["bills"] == 2
    assert rows["Solo"]["reportable"] == "yes" and not rows["Solo"]["w9_missing"]
    assert rows["Small"]["reportable"] == "no" and rows["Small"]["w9_missing"]
    assert rows["BigCo"]["reportable"] == "check"  # a corporation, but attorney or medical?
    assert rows["LawCo"]["reportable"] == "yes"
    assert rows["Parts"]["reportable"] == "no"
    assert rows["Nobody"]["reportable"] == "check" and rows["Nobody"]["w9_missing"]

    last_year = tax.report(ledger, 2025)
    assert [(r["payee"], r["reportable"]) for r in last_year] == [("Solo", "yes")]


def test_attorney_proceeds_under_the_threshold_are_checked(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    tax.record(ledger, "Counsel", "2026-01-10", "c-corp", KEY, TIN, pays_for="attorney")
    paid(ledger, "Counsel", "C1", 700)
    assert tax.report(ledger, 2026)[0]["reportable"] == "check"


def test_cli_records_tax_details_and_no_full_tin_appears_anywhere(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "l.db")
    monkeypatch.setattr("sys.stdin", io.StringIO("123-45-6789\n"))
    main(["--db", db, "bill", "payee-tax", "Solo", "--form-date", "2026-01-10",
          "--entity", "individual", "--by", "dana"])
    main(["--db", db, "bill", "add", "--payee", "Solo", "--invoice", "1", "--amount-usd", "2500",
          "--account", "021000021 555566667"])
    main(["--db", db, "bill", "add", "--payee", "NoForm", "--invoice", "1", "--amount-usd", "50",
          "--account", "021000021 111122223"])
    ledger = Ledger(db)
    for b in bills.listed(ledger):
        ledger.decide_approval(b["approval_id"], True, "dana")
        bills.pay(ledger, [], b["id"], "sam", "manual", "r-" + b["id"])
    main(["--db", db, "bill", "list"])
    out_csv = tmp_path / "1099.csv"
    main(["--db", db, "tax-1099", "--year", "2026", "--csv", str(out_csv)])
    out = capsys.readouterr().out
    assert "TIN ending 6789" in out and "NoForm" in out and "(no W-9)" in out
    assert "threshold $2000.00" in out and "1099-K" in out
    rows = list(csv.DictReader(out_csv.open()))
    assert [(r["payee"], r["reportable"], r["w9_missing"], r["tin_last4"]) for r in rows] == [
        ("Solo", "yes", "no", "6789"), ("NoForm", "no", "yes", "")]

    stored = b"".join(p.read_bytes() for p in tmp_path.glob("l.db*"))
    texts = [out, out_csv.read_text(), json.dumps(ledger.audit_log(1000)), stored.decode("latin-1")]
    for text in texts:
        assert "123456789" not in text and "123-45-6789" not in text
