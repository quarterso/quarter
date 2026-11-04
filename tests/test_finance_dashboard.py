import base64
import csv
import io
import json
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, FakeVendor, call, start
from test_roles import PEOPLE, as_

from quarter_ledger import connect
from quarter_ledger.connect import Observed
from quarter_ledger.dashboard import default_month, render
from quarter_ledger.export import journal
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

OCTOBER, SEPTEMBER = "2026-10", "2026-09"


@pytest.fixture
def books(tmp_path):
    vendor = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeVendor))
    ledger = Ledger(str(tmp_path / "ledger.db"))
    connect.book(ledger, "csv", [
        Observed("o1", "2026-10-13T08:00:00+00:00", "openai", 2_000_000, agent="claude-code", customer="acme"),
        Observed("s1", "2026-09-20T08:00:00+00:00", "anthropic", 7_000_000, agent="support-bot", customer="globex"),
    ])
    centres = tmp_path / "centres.json"
    gw = Gateway(ledger, [], PRICES, {"fake": f"http://127.0.0.1:{vendor.server_port}"}, {AGENT_KEY}, ADMIN_KEY,
                 people=PEOPLE, centres_path=str(centres))
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", ledger, centres
    proxy.shutdown()
    vendor.shutdown()


def get(url, name, headers=None):
    """A GET as a browser sends it from the dashboard: basic auth, same origin."""
    token = base64.b64encode(f"x:{PEOPLE[name]['key']}".encode()).decode()
    req = urllib.request.Request(url, headers={"authorization": f"Basic {token}", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def post_form(url, name, fields):
    data = urllib.parse.urlencode([("form", "1"), *fields]).encode()
    req = urllib.request.Request(url + "/attribution", data=data, headers={
        **as_(name), "content-type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def version(url, name="Fay", month=OCTOBER):
    page = get(f"{url}/dashboard?month={month}", name)[2].decode()
    return page.split("name=version value='")[1].split("'")[0]


@pytest.mark.parametrize(("today", "shown"), [
    ("2026-11-01", "2026-10"), ("2026-11-07", "2026-10"), ("2026-11-08", "2026-11"),
    ("2026-01-03", "2025-12"), ("2026-03-07", "2026-02"), ("2026-10-31", "2026-10"),
])
def test_the_close_week_opens_on_last_month(today, shown):
    assert default_month(datetime.fromisoformat(today).replace(hour=23, tzinfo=UTC)) == shown


def test_dashboard_opens_on_the_default_month(books):
    url, _, _ = books
    status, _, body = get(url + "/dashboard", "Vic")
    want = datetime.strptime(default_month(datetime.now(UTC)), "%Y-%m")
    assert status == 200 and f"{want:%B %Y}, " in body.decode()


def test_every_figure_follows_the_chosen_month(books):
    url, _, _ = books
    october = get(f"{url}/dashboard?month={OCTOBER}", "Vic")[2].decode()
    september = get(f"{url}/dashboard?month={SEPTEMBER}", "Vic")[2].decode()
    assert "October 2026, the whole month" in october and "$2.00" in october and "$7.00" not in october
    by_agent = october.split("<h2>By agent</h2>")[1].split("</table>")[0]  # the register lists every agent
    assert "claude-code" in by_agent and "support-bot" not in by_agent
    assert "$7.00" in september and "globex" in september and "acme" not in september
    assert "<option value='2026-09' selected>" in september and "<option value='2026-10'>" in september
    assert "Caps and the forecast are shown for this month only" in october


def test_bad_or_future_month_is_refused(books):
    url, _, _ = books
    for month in ("2026-13", "26-10", "2026-10-01", "<b>", "2999-01"):
        assert get(f"{url}/dashboard?month={urllib.parse.quote(month)}", "Vic")[0] == 400, month
        assert get(f"{url}/export?month={urllib.parse.quote(month)}", "Fay")[0] == 400, month


def test_only_finance_sees_the_files_and_the_form(books):
    url, _, _ = books
    for name in ("Vic", "Ann"):
        page = get(f"{url}/dashboard?month={OCTOBER}", name)[2].decode()
        assert "/export?" not in page and "/attribution" not in page, name
    for name in ("Fay", "Ada"):
        page = get(f"{url}/dashboard?month={OCTOBER}", name)[2].decode()
        assert "href='/export?format=quickbooks&amp;month=2026-10'" in page, name
        assert "href='/audit-pack?month=2026-10'" in page and "action='/attribution'" in page, name


def test_journal_downloads_as_the_cli_writes_them(books):
    url, ledger, _ = books
    for fmt in ("csv", "quickbooks", "xero", "netsuite"):
        status, headers, body = get(f"{url}/export?month={OCTOBER}&format={fmt}", "Fay")
        assert status == 200 and headers["content-type"] == "text/csv; charset=utf-8", fmt
        assert headers["content-disposition"] == f'attachment; filename="quarter-journal-{fmt}-2026-10.csv"'
        assert body.decode() == journal(ledger, OCTOBER, fmt=fmt)
    status, headers, body = get(f"{url}/export?month={OCTOBER}&format=focus", "Fay")
    assert status == 200 and 'filename="quarter-focus-2026-10.csv"' in headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(body.decode())))
    assert len(rows) == 1 and rows[0]["ServiceProviderName"].lower() == "openai"
    assert get(f"{url}/export?month={OCTOBER}&format=excel", "Fay")[0] == 400
    downloads = [r for r in ledger.audit_log() if r["action"] == "download"]
    assert len(downloads) == 5 and downloads[0]["client"] == "Fay@127.0.0.1"


def test_audit_pack_and_approvals_record_download(books):
    url, _, _ = books
    status, headers, body = get(f"{url}/audit-pack?month={OCTOBER}", "Ada")
    assert status == 200 and headers["content-type"] == "application/zip"
    assert headers["content-disposition"] == 'attachment; filename="quarter-audit-pack-2026-10.zip"'
    pack = zipfile.ZipFile(io.BytesIO(body))
    assert {"entries.csv", "journal.csv", "chain.json", "controls.md"} <= set(pack.namelist())
    assert json.loads(pack.read("chain.json"))["month"] == OCTOBER
    assert "finance-key" not in pack.read("controls.json").decode()  # roles go in, keys never
    status, headers, body = get(f"{url}/approvals/export?month={OCTOBER}", "Fay")
    assert status == 200 and headers["content-type"] == "application/zip"
    assert 'filename="quarter-approvals-2026-10.zip"' in headers["content-disposition"]
    assert {"approvals.csv", "chain.json"} <= set(zipfile.ZipFile(io.BytesIO(body)).namelist())


def test_downloads_refuse_everyone_but_finance(books):
    url, ledger, _ = books
    for path in ("/export?format=csv", "/audit-pack?", "/approvals/export?"):
        target = f"{url}{path}&month={OCTOBER}"
        assert call(target, headers=HEADERS)[0] == 401, path  # an agent key is not a person
        assert call(target, headers={})[0] == 401, path
        assert get(target, "Vic")[0] == 403 and get(target, "Ann")[0] == 403, path
        assert get(target, "Fay", {"origin": "https://evil.example"})[0] == 403, path
    assert sum(r["action"] == "role_refused" for r in ledger.audit_log()) == 6
    assert not any(r["action"] == "download" for r in ledger.audit_log())


def test_finance_edits_the_cost_centres_with_each_change_audited(books):
    url, ledger, centres = books
    fields = [("version", version(url)),
              ("agent", "claude-code"), ("department", "Engineering"), ("class", ""), ("location", ""),
              ("agent", "*"), ("department", "Other"), ("class", "G&A"), ("location", "")]
    assert post_form(url, "Fay", fields)[0] == 303
    assert json.loads(centres.read_text()) == {"department": {"claude-code": "Engineering", "*": "Other"},
                                               "class": {"*": "G&A"}}
    changes = [(r["client"], r["detail"]) for r in ledger.audit_log() if r["action"] == "cost_centre"]
    assert sorted(changes) == [("Fay@127.0.0.1", "class *: (none) -> G&A"),
                               ("Fay@127.0.0.1", "department *: (none) -> Other"),
                               ("Fay@127.0.0.1", "department claude-code: (none) -> Engineering")]
    page = get(f"{url}/dashboard?month={OCTOBER}", "Ada")[2].decode()
    assert "value='Engineering'" in page and "value='G&amp;A'" in page

    # The journal download splits by the saved map.
    body = get(f"{url}/export?month={OCTOBER}&format=csv", "Fay")[2].decode()
    assert {r["Department"] for r in csv.DictReader(io.StringIO(body)) if r["Debit"]} == {"Engineering"}

    fields = [("version", version(url, "Ada")),
              ("agent", "claude-code"), ("department", "R&D"), ("class", ""), ("location", ""),
              ("agent", "*"), ("department", "Other"), ("class", ""), ("location", "")]
    assert post_form(url, "Ada", fields)[0] == 303
    new = [r["detail"] for r in ledger.audit_log() if r["action"] == "cost_centre" and r["client"].startswith("Ada")]
    assert sorted(new) == ["class *: G&A -> (none)", "department claude-code: Engineering -> R&D"]
    assert ledger.verify_chain()["audit"]["intact"]


def test_quickbooks_download_says_why_a_department_is_refused(books):
    url, _, _ = books
    post_form(url, "Fay", [("version", version(url)), ("agent", "claude-code"), ("department", "Eng"),
                           ("class", ""), ("location", "")])
    status, _, body = get(f"{url}/export?month={OCTOBER}&format=quickbooks", "Fay")
    assert status == 400 and "use location" in json.loads(body)["error"]


def test_a_stale_form_is_refused_not_overwritten(books):
    url, _, centres = books
    stale = version(url)
    row = [("agent", "a1"), ("department", "Sales"), ("class", ""), ("location", "")]
    assert post_form(url, "Fay", [("version", stale), *row])[0] == 303
    assert post_form(url, "Ada", [("version", stale), ("agent", "a1"), ("department", "Ops"),
                                  ("class", ""), ("location", "")])[0] == 409
    assert json.loads(centres.read_text()) == {"department": {"a1": "Sales"}}


def test_cost_centre_form_refuses_bad_rows_and_other_roles(books):
    url, ledger, centres = books
    v = version(url)
    blank = ("class", ""), ("location", "")
    assert post_form(url, "Fay", [("version", v), ("agent", ""), ("department", "Eng"), *blank])[0] == 400
    twice = [("agent", "a1"), ("department", "A"), *blank]
    assert post_form(url, "Fay", [("version", v), *twice, *twice])[0] == 400
    assert post_form(url, "Fay", [("version", v), ("agent", "x" * 65), ("department", "A"), *blank])[0] == 400
    for name in ("Vic", "Ann"):
        assert post_form(url, name, [("version", v), ("agent", "a1"), ("department", "A"), *blank])[0] == 403
    assert call(url + "/attribution", {}, HEADERS)[0] == 401
    assert not centres.exists()
    assert not any(r["action"] == "cost_centre" for r in ledger.audit_log())


def test_render_escapes_centre_and_agent_names(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    page = render(ledger, [], "Fay (finance)", True, finance=True, month=OCTOBER,
                  centres={"department": {"<script>": "a'b\"c"}}, centres_version="v")
    assert "<script>" not in page and "&lt;script&gt;" in page and "a&#x27;b&quot;c" in page
