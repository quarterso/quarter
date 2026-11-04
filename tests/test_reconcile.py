import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from test_proxy import start

from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.reconcile import compare, fetch, model_of, read_costs
from quarter_ledger.store import Ledger

KEY = "sk-admin-test-not-real"


def spend(ledger, vendor, model, usd, at="2026-10-12T10:00:00+00:00"):
    _, call = ledger.hold([], {"agent": "a"}, vendor, model, to_micros(usd))
    ledger.settle(call, to_micros(usd))
    ledger.db.execute("update entries set at = ? where call_id = ?", (at, call))


class FakeVendor(BaseHTTPRequestHandler):
    """The two cost APIs as their documentation shows them, two pages each."""

    requests: ClassVar[list] = []
    answer: ClassVar[tuple | None] = None  # (status, body) to answer instead

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        FakeVendor.requests.append((url.path, dict(self.headers), query))
        if FakeVendor.answer:
            code, data = FakeVendor.answer
        elif url.path == "/v1/organization/costs":
            code, data = 200, json.dumps(self.openai(query.get("page"))).encode()
        else:
            code, data = 200, json.dumps(self.anthropic(query.get("page"))).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def openai(page):
        def bucket(day, results):
            return {"object": "bucket", "start_time": day, "end_time": day + 86400, "results": [
                {"object": "organization.costs.result", "amount": {"value": v, "currency": "usd"},
                 "line_item": item, "project_id": None} for item, v in results]}

        if page is None:
            return {"object": "page", "has_more": True, "next_page": "p2", "data": [
                bucket(1790812800, [("gpt-4o-2024-08-06, input", 1.5), ("gpt-4o-2024-08-06, output", 2.5)]),
                bucket(1790726400, [("gpt-4o-2024-08-06, input", 99)]),  # 30 September: not October
            ]}
        return {"object": "page", "has_more": False, "next_page": None, "data": [
            bucket(1793404800, [("o3, input", 0.06), ("assistants api | file search", 3)]),
            bucket(1793491200, [("o3, input", 50)]),  # 1 November: the day past the month
        ]}

    @staticmethod
    def anthropic(page):
        def bucket(day, results):
            return {"starting_at": f"{day}T00:00:00Z", "ending_at": "", "results": [
                {"amount": cents, "currency": "USD", "description": desc, "model": model,
                 "cost_type": "tokens" if model else "web_search"} for desc, model, cents in results]}

        if page is None:
            return {"has_more": True, "next_page": "page_2", "data": [
                bucket("2026-10-01", [("Claude Opus 5.5 Usage - Input Tokens", "claude-opus-5-5", "1000.5"),
                                      ("Claude Opus 5.5 Usage - Output Tokens", "claude-opus-5-5", "999.5")]),
            ]}
        return {"has_more": False, "next_page": None, "data": [
            bucket("2026-10-31", [("Web Search", None, "250")]),
        ]}

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor_url():
    FakeVendor.requests, FakeVendor.answer = [], None
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_openai_costs_by_model_across_pages(vendor_url):
    costs = fetch("openai", KEY, "2026-10", vendor_url)
    assert costs == {"gpt-4o-2024-08-06": to_micros(4), "o3": to_micros("0.06"),
                     "assistants api | file search": to_micros(3)}
    (path, headers, first), (_, _, second) = FakeVendor.requests
    assert path == "/v1/organization/costs" and headers["Authorization"] == f"Bearer {KEY}"
    assert first["start_time"] == ["1790812800"] and first["group_by"] == ["line_item"]
    assert first["end_time"] == ["1793577600"] and second["page"] == ["p2"]


def test_anthropic_cost_report_in_cents(vendor_url):
    costs = fetch("anthropic", KEY, "2026-10", vendor_url)
    assert costs == {"claude-opus-5-5": to_micros(20), "Web Search": to_micros("2.50")}
    (path, headers, first), (_, _, second) = FakeVendor.requests
    assert path == "/v1/organizations/cost_report"
    assert headers["X-Api-Key"] == KEY and headers["Anthropic-Version"] == "2023-06-01"
    assert "Authorization" not in headers
    assert first["starting_at"] == ["2026-10-01T00:00:00Z"] and first["group_by[]"] == ["description"]
    assert second["page"] == ["page_2"]


def test_http_errors_never_show_the_key(vendor_url):
    FakeVendor.answer = (401, f'{{"error": "bad key {KEY}"}}'.encode())
    with pytest.raises(ValueError, match="answered 401") as e:
        fetch("openai", KEY, "2026-10", vendor_url)
    assert KEY not in str(e.value)
    FakeVendor.answer = (302, b"")  # a redirect is not followed with the key
    with pytest.raises(ValueError, match="answered 302"):
        fetch("anthropic", KEY, "2026-10", vendor_url)
    FakeVendor.answer = (200, b"<html>")
    with pytest.raises(ValueError, match="could not be read"):
        fetch("anthropic", KEY, "2026-10", vendor_url)
    FakeVendor.answer = (200, json.dumps({"data": [{"start_time": 1790812800, "results": [
        {"amount": {"value": 1, "currency": "eur"}}]}]}).encode())
    with pytest.raises(ValueError, match="only USD"):
        fetch("openai", KEY, "2026-10", vendor_url)


def test_read_costs_from_a_console_export():
    text = ('﻿Date,Line Item,Cost\n2026-10-01,"gpt-4o, input","$1,000.25"\n'
            '2026-10-02,"gpt-4o, output",2\n2026-11-01,o3,7\n,,\n')
    assert read_costs(text, "2026-10") == {"gpt-4o": to_micros("1002.25")}
    assert read_costs("period,amount\n2026-10,5\n", "2026-10") == {"(no model)": to_micros(5)}
    with pytest.raises(ValueError, match="line 2"):
        read_costs("date,amount\n10/05/2026,5\n", "2026-10")
    with pytest.raises(ValueError, match="line 3"):
        read_costs("date,amount\n2026-10-01,5\n2026-10-02,lots\n", "2026-10")
    with pytest.raises(ValueError, match="date column"):
        read_costs("model,amount\no3,5\n", "2026-10")


def test_compare_flags_spend_outside_the_gateway_and_unbilled_spend():
    vendor = {"gpt-4o-2024-08-06": to_micros(100), "o3": to_micros(50), "o1": to_micros(10)}
    quarter = {"gpt-4o": to_micros("99.50"), "o3": to_micros(40), "gpt-5": to_micros(5)}
    out = compare(vendor, quarter, 2)
    rows = {r["model"]: r for r in out["rows"]}
    assert [r["model"] for r in out["rows"]] == ["o1", "o3", "gpt-5", "gpt-4o / gpt-4o-2024-08-06"]
    assert rows["o1"]["flag"] == "outside" and rows["gpt-5"]["flag"] == "unbilled"
    assert rows["o3"]["flag"] == "over" and rows["o3"]["difference_pct"] == 20
    assert rows["gpt-4o / gpt-4o-2024-08-06"]["flag"] == ""  # 0.5%, inside tolerance
    assert out["total"]["difference"] == to_micros("15.50") and out["total"]["flag"] == "over"
    assert model_of("evals | gpt-4o-mini, input") == "evals | gpt-4o-mini"


def test_reconcile_command_with_a_file(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    spend(ledger, "anthropic", "claude-opus-5-5", 20)
    spend(ledger, "anthropic", "claude-haiku-4-5", 3)
    spend(ledger, "openai", "gpt-4o", 9)  # another vendor
    spend(ledger, "anthropic", "claude-opus-5-5", 9, at="2026-11-01T00:00:00+00:00")  # next month
    ledger.db.execute(
        "insert into calls (call_id, at, vendor, model, route, input, output, cache_read, cache_write,"
        " cost, list_cost, tools_cost, system_cost, history_cost, output_cost, fingerprint, break_cost)"
        " values ('sub1', '2026-10-05T00:00:00+00:00', 'anthropic', 'claude-opus-5-5', 'subscription',"
        " 1, 1, 0, 0, 0, ?, 0, 0, 0, 0, '', 0)",
        (to_micros(4),),
    )
    costs = tmp_path / "costs.csv"
    costs.write_text("date,model,amount\n2026-10-03,claude-opus-5-5,20.10\n2026-10-04,claude-sonnet-4-5,8\n")
    run = ["--db", db, "reconcile", "--month", "2026-10", "--vendor", "anthropic", "--file", str(costs)]

    main(run)
    text = capsys.readouterr().out
    assert "vendor $28.10, Quarter $23.00, difference $5.10 18.1%" in text
    assert text.index("claude-sonnet-4-5") < text.index("claude-haiku-4-5") < text.index("claude-opus-5-5")
    assert "spend outside the gateway" in text and "did not bill it" in text
    assert "1 call(s) went on a subscription" in text and "$4.00 at list price" in text

    main([*run, "--csv"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "model,vendor_usd,quarter_usd,difference_usd,difference_pct,flag"
    assert lines[2] == "claude-haiku-4-5,0.00,3.00,-3.00,,unbilled"
    assert lines[-1] == "total,28.10,23.00,5.10,18.1,over"


def test_reconcile_command_from_the_api_and_clean_errors(tmp_path, capsys, monkeypatch, vendor_url):
    db = str(tmp_path / "l.db")
    spend(Ledger(db), "openai", "gpt-4o", 4)
    run = ["--db", db, "reconcile", "--month", "2026-10", "--vendor", "openai"]
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    with pytest.raises(SystemExit, match="set OPENAI_ADMIN_KEY"):
        main(run)

    monkeypatch.setenv("OPENAI_ADMIN_KEY", KEY)
    monkeypatch.setenv("QUARTER_OPENAI_ADMIN_URL", vendor_url)
    main(run)
    text = capsys.readouterr().out
    assert "gpt-4o / gpt-4o-2024-08-06" in text and KEY not in text

    FakeVendor.answer = (500, KEY.encode())
    with pytest.raises(SystemExit, match="reconcile: the openai cost API: answered 500") as e:
        main(run)
    assert KEY not in str(e.value)
    with pytest.raises(SystemExit, match="reconcile: .*missing.csv"):
        main([*run, "--file", str(tmp_path / "missing.csv")])
    with pytest.raises(SystemExit, match="reconcile: .*October"):
        main(["--db", db, "reconcile", "--month", "October", "--vendor", "openai"])
