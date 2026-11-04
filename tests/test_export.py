import csv
import json
from io import StringIO

import pytest

from quarter_ledger import connect
from quarter_ledger.connect import Observed
from quarter_ledger.export import (
    DIMENSIONS,
    FOCUS_COLUMNS,
    centre_headers,
    focus,
    focus_rows,
    journal,
    unbilled,
)
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger, month_range


def test_journal_balances_after_rounding(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    # Three charges of $0.004 each round to $0.00 alone but must still balance as a set.
    for customer, vendor, amount in [
        ("acme", "openai", 1_234_567),
        ("acme", "serpapi", 5_005),
        ("globex", "openai", 2_999_999),
    ]:
        _, call = ledger.hold([], {"agent": "a", "customer": customer}, vendor, "m", amount)
        ledger.settle(call, amount)

    month = ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]
    rows = list(csv.DictReader(StringIO(journal(ledger, month))))
    debits = sum(round(float(r["Debit"]) * 100) for r in rows if r["Debit"])
    credits = sum(round(float(r["Credit"]) * 100) for r in rows if r["Credit"])
    assert debits == credits == 123 + 1 + 300
    assert {r["Customer"] for r in rows if r["Debit"]} == {"acme", "globex"}


def test_empty_month_has_header_only(tmp_path):
    assert journal(Ledger(str(tmp_path / "l.db")), "2020-01").strip().count("\n") == 0


def test_month_range_wraps_december():
    start, end = month_range("2026-12")
    assert (start.month, end.year, end.month) == (12, 2027, 1)


def test_customer_spend_is_cost_of_revenue(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for customer in ("acme", None):
        _, call = ledger.hold([], {"agent": "a", "customer": customer}, "openai", "m", 1_000_000)
        ledger.settle(call, 1_000_000)
    month = ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]
    rows = list(csv.DictReader(StringIO(journal(ledger, month))))
    accounts = {r["Customer"]: r["Account"] for r in rows if r["Debit"]}
    assert accounts == {"acme": "Cost of revenue: AI", "": "AI vendor costs"}


def spent_month(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    for customer, amount in [("acme", 1_234_567), (None, 2_000_000)]:
        _, call = ledger.hold([], {"agent": "a", "customer": customer}, "openai", "m", amount)
        ledger.settle(call, amount)
    month = ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]
    return ledger, month


def test_quickbooks_layout_balances_and_keeps_names_out_unless_asked(tmp_path):
    ledger, month = spent_month(tmp_path)
    rows = list(csv.DictReader(StringIO(journal(ledger, month, fmt="quickbooks"))))
    assert list(rows[0]) == [
        "Journal No.",
        "Journal Date",
        "Account Name",
        "Debits",
        "Credits",
        "Description",
        "Name",
    ]
    debits = sum(round(float(r["Debits"]) * 100) for r in rows if r["Debits"])
    credits = sum(round(float(r["Credits"]) * 100) for r in rows if r["Credits"])
    assert debits == credits == 123 + 200
    assert {r["Name"] for r in rows} == {""} and "openai usage for acme" in {
        r["Description"] for r in rows
    }
    assert rows[0]["Journal Date"].count("/") == 2  # MM/DD/YYYY

    named = list(
        csv.DictReader(StringIO(journal(ledger, month, fmt="quickbooks", customer_names=True)))
    )
    assert "acme" in {r["Name"] for r in named}


def test_xero_one_signed_amount_column_sums_to_zero(tmp_path):
    ledger, month = spent_month(tmp_path)
    text = journal(ledger, month, "6100", "2100", "5100", fmt="xero", tax_rate="No VAT")
    rows = list(csv.DictReader(StringIO(text)))
    assert list(rows[0]) == [
        "*Narration",
        "*Date",
        "Description",
        "*AccountCode",
        "*TaxRate",
        "*Amount",
    ]
    assert sum(round(float(r["*Amount"]) * 100) for r in rows) == 0
    assert {r["*AccountCode"] for r in rows} == {"6100", "2100", "5100"}
    assert {r["*TaxRate"] for r in rows} == {"No VAT"} and len({r["*Narration"] for r in rows}) == 1


def test_netsuite_layout_and_custom_date(tmp_path):
    ledger, month = spent_month(tmp_path)
    text = journal(ledger, month, fmt="netsuite", date_format="%d.%m.%Y")
    rows = list(csv.DictReader(StringIO(text)))
    assert list(rows[0]) == ["External ID", "Date", "Account", "Debit", "Credit", "Memo", "Name"]
    assert {r["External ID"] for r in rows} == {f"AGENT-{month}"} and rows[0]["Date"].count(
        "."
    ) == 2


def test_cli_warns_past_an_importers_line_limit(tmp_path, capsys, monkeypatch):
    from quarter_ledger import cli

    _, month = spent_month(tmp_path)
    monkeypatch.setitem(cli.LINE_LIMITS, "xero", 2)
    cli.main(["--db", str(tmp_path / "l.db"), "export", "--month", month, "--format", "xero"])
    out = capsys.readouterr()
    assert "at most 2 lines" in out.err and out.out.startswith("*Narration")


def team_month(path):
    """October 2026: two agents and an unnamed one, two customers' worth, and a seat."""
    ledger = Ledger(path)
    for agent, customer, vendor, amount in [
        ("claude-code", "acme", "openai", 1_234_567),
        ("support-bot", "acme", "openai", 2_000_004),
        ("support-bot", None, "anthropic", 3_005_000),
        ("researcher", None, "anthropic", 1_004_999),
        ("researcher", None, "serpapi", 5_000),
    ]:
        _, call = ledger.hold([], {"agent": agent, "customer": customer}, vendor, "m", amount)
        ledger.settle(call, amount)
    ledger.db.execute("update entries set at = '2026-10-15T12:00:00+00:00'")
    ledger.add_cost("seat", "cursor", 40_000_000, "2026-10", agent="claude-code")
    return ledger


# Today's journal for team_month, byte for byte, before cost centres existed.
WITHOUT_CENTRES = {
    'csv': 'Journal No,Date,Account,Debit,Credit,Description,Customer\r\nAGENT-2026-10,2026-10-31,AI vendor costs,4.01,,anthropic usage,\r\nAGENT-2026-10,2026-10-31,AI vendor costs,0.01,,serpapi usage,\r\nAGENT-2026-10,2026-10-31,Cost of revenue: AI,3.23,,openai usage,acme\r\nAGENT-2026-10,2026-10-31,AI vendor costs,40.00,,seat: cursor (claude-code),\r\nAGENT-2026-10,2026-10-31,Accrued AI vendor costs,,47.25,Agent spend accrual,\r\n',
    'quickbooks': 'Journal No.,Journal Date,Account Name,Debits,Credits,Description,Name\r\nAGENT-2026-10,10/31/2026,AI vendor costs,4.01,,anthropic usage,\r\nAGENT-2026-10,10/31/2026,AI vendor costs,0.01,,serpapi usage,\r\nAGENT-2026-10,10/31/2026,Cost of revenue: AI,3.23,,openai usage for acme,\r\nAGENT-2026-10,10/31/2026,AI vendor costs,40.00,,seat: cursor (claude-code),\r\nAGENT-2026-10,10/31/2026,Accrued AI vendor costs,,47.25,Agent spend accrual,\r\n',
    'xero': '*Narration,*Date,Description,*AccountCode,*TaxRate,*Amount\r\nQuarter agent spend 2026-10,10/31/2026,anthropic usage,AI vendor costs,Tax Exempt,4.01\r\nQuarter agent spend 2026-10,10/31/2026,serpapi usage,AI vendor costs,Tax Exempt,0.01\r\nQuarter agent spend 2026-10,10/31/2026,openai usage for acme,Cost of revenue: AI,Tax Exempt,3.23\r\nQuarter agent spend 2026-10,10/31/2026,seat: cursor (claude-code),AI vendor costs,Tax Exempt,40.00\r\nQuarter agent spend 2026-10,10/31/2026,Agent spend accrual,Accrued AI vendor costs,Tax Exempt,-47.25\r\n',
    'netsuite': 'External ID,Date,Account,Debit,Credit,Memo,Name\r\nAGENT-2026-10,10/31/2026,AI vendor costs,4.01,,anthropic usage,\r\nAGENT-2026-10,10/31/2026,AI vendor costs,0.01,,serpapi usage,\r\nAGENT-2026-10,10/31/2026,Cost of revenue: AI,3.23,,openai usage for acme,\r\nAGENT-2026-10,10/31/2026,AI vendor costs,40.00,,seat: cursor (claude-code),\r\nAGENT-2026-10,10/31/2026,Accrued AI vendor costs,,47.25,Agent spend accrual,\r\n',
}
TEAMS = {"department": {"claude-code": "Engineering", "support-bot": "Support"}}


def test_without_cost_centres_every_format_is_unchanged(tmp_path):
    ledger = team_month(str(tmp_path / "l.db"))
    for fmt, expected in WITHOUT_CENTRES.items():
        assert journal(ledger, "2026-10", fmt=fmt) == expected


def debit_lines(text: str) -> list[tuple]:
    rows = list(csv.DictReader(StringIO(text)))
    keys = ("Description", "Customer", "Department", "Debit")
    return [tuple(r[k] for k in keys) for r in rows if r["Debit"]]


def test_cost_centres_split_expense_lines_and_still_balance(tmp_path):
    ledger = team_month(str(tmp_path / "l.db"))
    text = journal(ledger, "2026-10", centres=TEAMS)
    assert debit_lines(text) == [
        ("anthropic usage", "", "", "1.00"),  # researcher is not mapped: unassigned
        ("anthropic usage", "", "Support", "3.01"),
        ("serpapi usage", "", "", "0.01"),
        ("openai usage", "acme", "Engineering", "1.23"),
        ("openai usage", "acme", "Support", "2.00"),
        ("seat: cursor (claude-code)", "", "Engineering", "40.00"),
    ]
    rows = list(csv.DictReader(StringIO(text)))
    assert rows[-1]["Credit"] == "47.25" and rows[-1]["Department"] == ""


def test_split_lines_round_alone_and_the_accrual_matches_them(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    # $0.005 each: one cent together, but a cent apiece once split by team.
    for agent in ("claude-code", "support-bot"):
        _, call = ledger.hold([], {"agent": agent}, "openai", "m", 5_000)
        ledger.settle(call, 5_000)
    month = ledger.db.execute("select substr(at, 1, 7) from entries limit 1").fetchone()[0]
    rows = list(csv.DictReader(StringIO(journal(ledger, month, centres=TEAMS))))
    assert [r["Debit"] for r in rows if r["Debit"]] == ["0.01", "0.01"]
    assert rows[-1]["Credit"] == "0.02"


def test_star_takes_every_agent_not_named(tmp_path):
    ledger = team_month(str(tmp_path / "l.db"))
    centres = {"department": {**TEAMS["department"], "*": "Shared"}}
    assert [d[2] for d in debit_lines(journal(ledger, "2026-10", centres=centres))] == [
        "Shared",
        "Support",
        "Shared",
        "Engineering",
        "Support",
        "Engineering",
    ]


def test_each_format_names_cost_centres_in_its_own_columns(tmp_path):
    ledger = team_month(str(tmp_path / "l.db"))
    both = {"class": {"*": "AI"}, "location": {"support-bot": "Dublin"}}

    def read(fmt, centres=both):
        return list(csv.DictReader(StringIO(journal(ledger, "2026-10", fmt=fmt, centres=centres))))

    qb = read("quickbooks")
    assert list(qb[0])[-2:] == ["Class", "Location"]
    assert (qb[1]["Class"], qb[1]["Location"]) == ("AI", "Dublin")

    ns = read("netsuite", {**TEAMS, **both})
    assert list(ns[0])[-3:] == ["Department", "Class", "Location"]
    assert (ns[1]["Department"], ns[1]["Class"], ns[1]["Location"]) == ("Support", "AI", "Dublin")

    xero = read("xero")
    tracking = ["TrackingName1", "TrackingOption1", "TrackingName2", "TrackingOption2"]
    assert list(xero[0])[-4:] == tracking
    assert [xero[1][k] for k in tracking] == ["Class", "AI", "Location", "Dublin"]
    assert [xero[0][k] for k in tracking[2:]] == ["", ""]  # researcher: no location
    assert sum(round(float(r["*Amount"]) * 100) for r in xero) == 0


def test_importers_that_cannot_take_the_centres_say_so():
    with pytest.raises(ValueError, match="use location"):
        centre_headers("quickbooks", ["department"])
    with pytest.raises(ValueError, match="2 tracking categories"):
        centre_headers("xero", list(DIMENSIONS))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "Expecting property name"),
        ("[]", "must be an object"),
        ('{"team": {"a": "b"}}', "team: not a cost centre"),
        ('{"class": {"a": ""}}', "class: must map agents"),
        ('{"class": ["a"]}', "class: must map agents"),
    ],
)
def test_cli_bad_centres_file_exits_with_a_message(tmp_path, capsys, text, message):
    from quarter_ledger import cli

    team_month(str(tmp_path / "l.db"))
    path = tmp_path / "centres.json"
    path.write_text(text)
    with pytest.raises(SystemExit) as exit:
        cli.main(["--db", str(tmp_path / "l.db"), "export", "--month", "2026-10",
                  "--cost-centres", str(path)])
    assert str(exit.value).startswith(f"export: {path}: ") and message in str(exit.value)
    assert capsys.readouterr().out == ""


def test_cli_quickbooks_with_departments_and_a_missing_file_exit_cleanly(tmp_path):
    from quarter_ledger import cli

    team_month(str(tmp_path / "l.db"))
    path = tmp_path / "centres.json"
    path.write_text(json.dumps(TEAMS))
    base = ["--db", str(tmp_path / "l.db"), "export", "--month", "2026-10", "--cost-centres"]
    with pytest.raises(SystemExit, match="tracks departments as locations"):
        cli.main([*base, str(path), "--format", "quickbooks"])
    with pytest.raises(SystemExit, match="No such file"):
        cli.main([*base, str(tmp_path / "missing.json")])


def test_cli_splits_with_cost_centres(tmp_path, capsys):
    from quarter_ledger import cli

    team_month(str(tmp_path / "l.db"))
    path = tmp_path / "centres.json"
    path.write_text(json.dumps(TEAMS))
    cli.main(["--db", str(tmp_path / "l.db"), "export", "--month", "2026-10", "--format",
              "netsuite", "--cost-centres", str(path)])
    rows = list(csv.DictReader(StringIO(capsys.readouterr().out)))
    assert {r["Department"] for r in rows} == {"", "Engineering", "Support"}


def billing_month(path):
    """October 2026: $6 of openai for acme, $4 of openai and $2 of anthropic for no one, a seat."""
    ledger = Ledger(path)
    for customer, vendor, amount in [("acme", "openai", 6), (None, "openai", 4), (None, "anthropic", 2)]:
        _, call = ledger.hold([], {"agent": "a", "customer": customer}, vendor, "m", amount * 1_000_000)
        ledger.settle(call, amount * 1_000_000)
    ledger.db.execute("update entries set at = '2026-10-15T12:00:00+00:00'")
    ledger.add_cost("seat", "cursor", 40_000_000, "2026-10", agent="a")
    return ledger


def summed(rows):
    return sum(r.debit for r in rows), sum(r.credit for r in rows)


def test_with_no_bill_the_whole_months_spend_is_accrued(tmp_path):
    rows = unbilled(billing_month(str(tmp_path / "l.db")), "2026-10", {})
    assert [(r.account, r.debit, r.credit, r.customer) for r in rows] == [
        ("AI vendor costs", 200, 0, ""),
        ("AI vendor costs", 400, 0, ""),
        ("Cost of revenue: AI", 600, 0, "acme"),
        ("Accrued liabilities", 0, 1200, ""),
    ]  # the seat was paid, so it is not accrued
    assert rows[-1].description == "Unbilled agent spend accrual"


def test_partial_bill_accrues_the_rest_split_by_customer(tmp_path):
    ledger = billing_month(str(tmp_path / "l.db"))
    rows = unbilled(ledger, "2026-10", {"openai": 5_000_000, "anthropic": 9_000_000},
                    accrued_account="2150", revenue_cost_account="5100")
    assert [(r.account, r.debit, r.credit, r.description, r.customer) for r in rows] == [
        ("AI vendor costs", 200, 0, "openai usage not yet billed", ""),
        ("5100", 300, 0, "openai usage not yet billed", "acme"),
        ("2150", 0, 500, "Unbilled agent spend accrual", ""),
    ]  # anthropic billed more than Quarter saw: nothing of it is unbilled


def test_split_shares_round_alone_and_the_accrual_matches_them(tmp_path):
    ledger = team_month(str(tmp_path / "l.db"))
    rows = unbilled(ledger, "2026-10", {"openai": 1_000_000}, centres=TEAMS)
    debits, credits = summed(rows)
    assert debits == credits == 223 + 401 + 1
    assert {(r.customer, r.centres) for r in rows if r.description.startswith("openai")} == {
        ("acme", ("Engineering",)),
        ("acme", ("Support",)),
    }


def test_empty_month_accrues_nothing(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    assert unbilled(ledger, "2020-01", {"openai": 1_000_000}) == []
    assert journal(ledger, "2020-01", rows=[], reversal="R").strip().count("\n") == 0


def test_reversal_is_the_accrual_negated_on_the_first_of_next_month(tmp_path):
    ledger = billing_month(str(tmp_path / "l.db"))
    rows = unbilled(ledger, "2026-10", {})
    text = journal(ledger, "2026-10", rows=rows, number="ACCR", reversal="REV")
    out = list(csv.DictReader(StringIO(text)))
    accrual = [r for r in out if r["Journal No"] == "ACCR"]
    reversed_ = [r for r in out if r["Journal No"] == "REV"]
    assert {r["Date"] for r in accrual} == {"2026-10-31"}
    assert {r["Date"] for r in reversed_} == {"2026-11-01"}
    swapped = [{**r, "Journal No": "ACCR", "Date": "2026-10-31", "Debit": r["Credit"],
                "Credit": r["Debit"]} for r in reversed_]
    assert swapped == accrual and len(accrual) == 4


def test_december_reverses_in_january(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    _, call = ledger.hold([], {"agent": "a"}, "openai", "m", 1_000_000)
    ledger.settle(call, 1_000_000)
    ledger.db.execute("update entries set at = '2026-12-31T23:00:00+00:00'")
    text = journal(ledger, "2026-12", rows=unbilled(ledger, "2026-12", {}), reversal="REV",
                   fmt="quickbooks")
    assert {r["Journal Date"] for r in csv.DictReader(StringIO(text))} == {"12/31/2026", "01/01/2027"}


def cli_unbilled(tmp_path, capsys, *extra):
    from quarter_ledger import cli

    billing_month(str(tmp_path / "l.db"))
    bill = tmp_path / "openai.csv"
    bill.write_text("date,amount\n2026-10-20,5.00\n2026-11-02,3.00\n")
    cli.main(["--db", str(tmp_path / "l.db"), "export", "--month", "2026-10", "--unbilled",
              "--billed", f"openai={bill}", *extra])
    out = capsys.readouterr()
    return list(csv.DictReader(StringIO(out.out))), out.err


@pytest.mark.parametrize(
    ("fmt", "number", "date", "debit", "credit"),
    [
        ("csv", "Journal No", "Date", "Debit", "Credit"),
        ("quickbooks", "Journal No.", "Journal Date", "Debits", "Credits"),
        ("netsuite", "External ID", "Date", "Debit", "Credit"),
    ],
)
def test_cli_accrual_and_reversal_balance_in_each_format(tmp_path, capsys, fmt, number, date,
                                                         debit, credit):
    rows, _ = cli_unbilled(tmp_path, capsys, "--format", fmt)
    for no, on in [("AGENT-ACCR-2026-10", "2026-10-31"), ("AGENT-REV-2026-10", "2026-11-01")]:
        entry = [r for r in rows if r[number] == no]
        on = on if fmt == "csv" else f"{on[5:7]}/{on[8:]}/{on[:4]}"
        assert {r[date] for r in entry} == {on}
        debits = sum(round(float(r[debit]) * 100) for r in entry if r[debit])
        credits = sum(round(float(r[credit]) * 100) for r in entry if r[credit])
        assert debits == credits == 200 + 300 + 200
    assert len(rows) == 8


def test_cli_xero_reversal_sums_to_zero_and_negates(tmp_path, capsys):
    rows, _ = cli_unbilled(tmp_path, capsys, "--format", "xero", "--accrued-account", "2150")
    accrual = [r for r in rows if r["*Date"] == "10/31/2026"]
    reversed_ = [r for r in rows if r["*Date"] == "11/01/2026"]
    assert {r["*Narration"] for r in accrual} == {"Quarter unbilled agent spend 2026-10"}
    assert {r["*Narration"] for r in reversed_} == {"Quarter unbilled agent spend 2026-10 reversed"}
    assert sum(float(r["*Amount"]) for r in accrual) == 0
    assert [-float(r["*Amount"]) for r in reversed_] == [float(r["*Amount"]) for r in accrual]
    assert ("2150", "-7.00") in {(r["*AccountCode"], r["*Amount"]) for r in accrual}


def test_cli_unbilled_errors_and_a_vendor_quarter_never_saw(tmp_path, capsys):
    from quarter_ledger import cli

    base = ["--db", str(tmp_path / "l.db"), "export", "--month", "2026-10"]
    with pytest.raises(SystemExit, match="--billed needs --unbilled"):
        cli.main([*base, "--billed", "openai=bill.csv"])
    with pytest.raises(SystemExit, match="give it as VENDOR=FILE"):
        cli.main([*base, "--unbilled", "--billed", "bill.csv"])
    with pytest.raises(SystemExit, match="No such file"):
        cli.main([*base, "--unbilled", "--billed", f"openai={tmp_path / 'missing.csv'}"])
    rows, err = cli_unbilled(tmp_path, capsys, "--billed", f"open-ai={tmp_path / 'openai.csv'}")
    assert len(rows) == 8 and "Quarter recorded no open-ai spend in 2026-10" in err


# FOCUS 1.4: the Cost and Usage columns whose feature level is Mandatory, in the spec's order
# (specification/datasets/cost_and_usage/columns/columns.mdpp at tag v1.4).
SPEC_1_4_MANDATORY = [
    "BilledCost", "BillingAccountId", "BillingAccountName", "BillingCurrency", "BillingPeriodEnd",
    "BillingPeriodStart", "ChargeCategory", "ChargeClass", "ChargeDescription", "ChargePeriodEnd",
    "ChargePeriodStart", "ContractedCost", "EffectiveCost", "HostProviderName", "InvoiceIssuerName",
    "ListCost", "PricingQuantity", "PricingUnit", "ServiceProviderName", "ServiceCategory",
    "ServiceName",
]


def focus_ledger(tmp_path):
    """A governed call with tokens, an observed call with tokens, and a vendor's day total."""
    ledger = Ledger(str(tmp_path / "l.db"))
    _, call = ledger.hold([], {"agent": "support-bot", "task": "t-1", "customer": "acme"},
                          "anthropic", "claude-sonnet-4-5", 50_000)
    ledger.settle(call, 12_345)
    ledger.db.execute("update entries set at = '2026-10-12T10:30:00+00:00' where call_id = ?", (call,))
    ledger.record_call({
        "call_id": call, "at": "2026-10-12T10:30:00+00:00", "agent": "support-bot", "task": "t-1",
        "vendor": "anthropic", "model": "claude-sonnet-4-5", "route": "api", "input": 1000,
        "output": 200, "cache_read": 3000, "cache_write": 0, "cost": 12_345, "list_cost": 15_000,
        "tools_cost": 0, "system_cost": 0, "history_cost": 0, "output_cost": 0, "fingerprint": "{}",
        "break_cost": 0, "saved": 0,
    })
    connect.book(ledger, "litellm", [Observed("r1", "2026-10-13T08:00:00+00:00", "openai", 2_000_000,
                                              model="gpt-5", agent="coder", task="t-2", input=50, output=7)])
    connect.book(ledger, "openai_costs", [Observed("2026-10-14|gpt-5|", "2026-10-14T00:00:00+00:00",
                                                   "openai", 3_000_000, model="gpt-5", bucket=True)])
    return ledger, call


def test_focus_columns_are_the_1_4_mandatory_set_first_then_extensions(tmp_path):
    header = next(csv.reader(StringIO(focus(focus_rows(Ledger(str(tmp_path / "l.db")))))))
    assert header == list(FOCUS_COLUMNS)
    assert header[:len(SPEC_1_4_MANDATORY)] == SPEC_1_4_MANDATORY
    rest = header[len(SPEC_1_4_MANDATORY):]
    assert not set(rest) & set(SPEC_1_4_MANDATORY) and len(set(header)) == len(header)
    assert [c for c in rest if c.startswith("x_")] == rest[rest.index("x_CallId"):]


def test_focus_gateway_charge(tmp_path):
    ledger, call = focus_ledger(tmp_path)
    row = focus_rows(ledger, accounts={"anthropic": "org-123"})[0]
    assert row == row | {
        "BilledCost": "0.012345", "EffectiveCost": "0.012345", "ContractedCost": "0.012345",
        "ListCost": "0.015000", "BillingCurrency": "USD", "BillingAccountId": "org-123",
        "BillingPeriodStart": "2026-10-01T00:00:00Z", "BillingPeriodEnd": "2026-11-01T00:00:00Z",
        "ChargePeriodStart": "2026-10-12T00:00:00Z", "ChargePeriodEnd": "2026-10-13T00:00:00Z",
        "ChargeCategory": "Usage", "ChargeClass": None, "ServiceProviderName": "anthropic",
        "HostProviderName": "anthropic", "InvoiceIssuerName": "anthropic", "ServiceName": "anthropic",
        "ServiceCategory": "AI and Machine Learning", "ServiceSubcategory": "Generative AI",
        "SkuId": "claude-sonnet-4-5", "SkuPriceId": "claude-sonnet-4-5",
        "PricingQuantity": 4200, "PricingUnit": "Tokens", "ConsumedQuantity": 4200,
        "ConsumedUnit": "Tokens", "x_CallId": call, "x_Agent": "support-bot", "x_Task": "t-1",
        "x_Customer": "acme", "x_Governed": "true", "x_InputTokens": 1000, "x_OutputTokens": 200,
        "x_CacheReadTokens": 3000, "x_CacheWriteTokens": 0,
        "Tags": '{"agent": "support-bot", "customer": "acme", "task": "t-1"}',
    }


def test_focus_observed_charges(tmp_path):
    ledger, _ = focus_ledger(tmp_path)
    _, observed, total = focus_rows(ledger)
    assert observed == observed | {
        "x_CallId": "obs:litellm:r1", "x_Governed": "false", "BilledCost": "2.000000",
        "ListCost": "2.000000", "BillingAccountId": "openai", "PricingQuantity": 57, "x_Task": "t-2",
        "ChargePeriodStart": "2026-10-13T00:00:00Z",
    }
    # A vendor's day total has no tokens: the quantities are null, never a made-up 0.
    assert total == total | {
        "x_Governed": "false", "BilledCost": "3.000000", "PricingQuantity": None, "PricingUnit": None,
        "ConsumedQuantity": None, "ConsumedUnit": None, "x_InputTokens": None, "Tags": None,
        "ChargePeriodStart": "2026-10-14T00:00:00Z", "ChargePeriodEnd": "2026-10-15T00:00:00Z",
    }


def test_focus_lowered_vendor_total_is_a_negative_usage_row(tmp_path):
    ledger, _ = focus_ledger(tmp_path)
    connect.book(ledger, "openai_costs", [Observed("2026-10-14|gpt-5|", "2026-10-14T00:00:00+00:00",
                                                   "openai", 2_500_000, model="gpt-5", bucket=True)])
    revised = focus_rows(ledger)[-1]
    assert (revised["BilledCost"], revised["EffectiveCost"], revised["ListCost"]) == ("-0.500000",) * 3
    assert (revised["ChargeCategory"], revised["ChargeClass"]) == ("Usage", None)


def test_focus_counts_governed_and_observed_spend_once(tmp_path):
    ledger, _ = focus_ledger(tmp_path)
    # The vendor's cost API reports $5 for 2026-10-13, of which the observed LiteLLM call holds $2.
    groups = {("2026-10-13", "gpt-5"): {None: 5_000_000}}
    connect.book(ledger, "openai_costs", connect.remainder(ledger, "openai_costs", "openai", groups))
    rows = focus_rows(ledger)
    charged = ledger.db.execute("select count(*), sum(amount) from entries where kind = 'charge'").fetchone()
    assert len(rows) == charged[0] == 4  # holds and releases are not rows
    assert sum(to_micros(r["BilledCost"]) for r in rows) == charged[1] == 12_345 + 8_000_000
    on_13th = {r["x_CallId"]: r["BilledCost"] for r in rows if r["ChargePeriodStart"].startswith("2026-10-13")}
    assert on_13th == {"obs:litellm:r1": "2.000000", "obs:openai_costs:2026-10-13|gpt-5|": "3.000000"}


def test_focus_csv_quotes_and_nulls(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    _, call = ledger.hold([], {"agent": "a", "customer": 'Acme, "Inc"'}, "serpapi", None, 1_000)
    ledger.settle(call, 1_000)
    text = focus(focus_rows(ledger))
    assert '"Acme, ""Inc"""' in text
    row = next(csv.DictReader(StringIO(text)))
    assert row["x_Customer"] == 'Acme, "Inc"'
    assert json.loads(row["Tags"]) == {"agent": "a", "customer": 'Acme, "Inc"'}
    assert (row["ServiceCategory"], row["ServiceSubcategory"], row["SkuPriceId"]) == ("Other", "Other (Other)", "")
    assert row["ChargeClass"] == row["PricingQuantity"] == ""


def test_cli_focus_dates_file_and_errors(tmp_path, capsys):
    from quarter_ledger import cli

    focus_ledger(tmp_path)
    base = ["--db", str(tmp_path / "l.db"), "export", "--format", "focus"]
    out = tmp_path / "focus.csv"
    cli.main([*base, "--from", "2026-10-13", "--to", "2026-10-14", "--csv", str(out)])
    assert capsys.readouterr().out == ""
    assert [r["x_CallId"] for r in csv.DictReader(out.open())] == ["obs:litellm:r1"]
    cli.main([*base, "--month", "2026-10", "--account", "openai=org-9"])
    rows = list(csv.DictReader(StringIO(capsys.readouterr().out)))
    assert [r["BillingAccountId"] for r in rows] == ["anthropic", "org-9", "org-9"]
    with pytest.raises(SystemExit, match="not both"):
        cli.main([*base, "--month", "2026-10", "--from", "2026-10-01"])
    with pytest.raises(SystemExit, match="give it as VENDOR=ID"):
        cli.main([*base, "--account", "org-9"])
    with pytest.raises(SystemExit, match="needs --month"):
        cli.main(["--db", str(tmp_path / "l.db"), "export"])
