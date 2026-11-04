"""Connect mode against recorded responses (tests/fixtures/connect), written from each
vendor's published documentation. No test calls a real API."""

import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from test_proxy import start

from quarter_ledger import connect, runaway, sources, status
from quarter_ledger.cli import main
from quarter_ledger.connect import Observed, Spike, pick
from quarter_ledger.export import lines
from quarter_ledger.forensics import why
from quarter_ledger.margin import margins
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.slack import Slack
from quarter_ledger.store import Ledger

FIXTURES = Path(__file__).parent / "fixtures" / "connect"
KEY = "test-key-not-real"
OCT = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))


def fixture(name: str, **replace) -> bytes:
    text = (FIXTURES / name).read_text()
    for old, new in replace.items():
        text = text.replace(old, new)
    return text.encode()


class FakeSource(BaseHTTPRequestHandler):
    """Answers from `routes`: {path: callable(query) -> (status, body)}."""

    routes: ClassVar[dict] = {}
    requests: ClassVar[list] = []

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        FakeSource.requests.append((url.path, dict(self.headers), query))
        code, body = FakeSource.routes[url.path](query)
        self.send_response(code)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def url():
    FakeSource.routes, FakeSource.requests = {}, []
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeSource))
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


def gateway_spend(ledger, vendor, model, usd, at="2026-10-12T10:00:00+00:00", agent="a", customer=None):
    _, call = ledger.hold([], {"agent": agent, "customer": customer}, vendor, model, to_micros(usd))
    ledger.settle(call, to_micros(usd))
    ledger.db.execute("update entries set at = ? where call_id = ?", (at, call))


def charges(ledger, like="obs:%"):
    return ledger.db.execute(
        "select coalesce(sum(amount), 0) from entries where kind = 'charge' and call_id like ?", (like,)
    ).fetchone()[0]


def litellm_routes(fail_page2=False):
    def logs(query):
        if query["start_date"] != ["2026-10-12"]:
            return 200, b'{"data": [], "total": 0, "page": 1, "page_size": 2, "total_pages": 0}'
        if query["page"] == ["2"]:
            return (500, b"down") if fail_page2 else (200, fixture("litellm_spend_logs_page2.json"))
        return 200, fixture("litellm_spend_logs_page1.json")

    return {"/spend/logs/v2": logs}


# LiteLLM

def test_litellm_spend_logs_are_booked_once_with_agent_customer_and_task(url, ledger, monkeypatch):
    monkeypatch.setattr(sources, "LITELLM_PAGE", 2)
    FakeSource.routes = litellm_routes()
    env = {"QUARTER_LITELLM_URL": url, "QUARTER_LITELLM_KEY": KEY}
    day = (datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC))
    mapping = sources.load_map("litellm", None)
    pulled = sources.pull(ledger, "litellm", *day, env, mapping)
    assert (pulled.booked, pulled.amount) == (3, to_micros("0.7623"))
    path, headers, query = FakeSource.requests[0]
    assert path == "/spend/logs/v2" and headers["Authorization"] == f"Bearer {KEY}"
    assert query["page_size"] == ["2"] and FakeSource.requests[1][2]["page"] == ["2"]
    rows = ledger.db.execute(
        "select call_id, at, agent, task, customer, vendor, model, amount from entries order by id").fetchall()
    assert rows[0] == ("obs:litellm:chatcmpl-111", "2026-10-12T09:15:00+00:00", "support-bot", "ticket-9",
                       "acme", "openai", "gpt-4o", to_micros("0.0123"))
    # no key alias: the team alias names the agent; the model's prefix names the vendor
    assert rows[1][2:6] == ("Research", "sess-7", "globex", "anthropic")
    assert rows[2][1] == "2026-10-12T23:59:00+00:00"  # a time with no zone is UTC
    assert {r[0] for r in ledger.db.execute("select kind from entries")} == {"charge"}  # no hold behind it

    again = sources.pull(ledger, "litellm", *day, env, mapping)
    assert (again.booked, again.unchanged, again.amount) == (0, 3, 0)
    assert charges(ledger) == to_micros("0.7623")
    assert ledger.verify_chain()["entries"]["intact"]
    assert ledger.approvals("approved") == [] and ledger.approvals("used") == []


def test_litellm_pull_cut_short_resumes_without_double_counting(url, ledger, monkeypatch):
    monkeypatch.setattr(sources, "LITELLM_PAGE", 2)
    FakeSource.routes = litellm_routes(fail_page2=True)
    env = {"QUARTER_LITELLM_URL": url, "QUARTER_LITELLM_KEY": KEY}
    span = (datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC))
    mapping = sources.load_map("litellm", None)
    with pytest.raises(ValueError, match="answered 500"):
        sources.pull(ledger, "litellm", *span, env, mapping)
    assert charges(ledger) == to_micros("0.5123")  # page 1 kept
    assert connect.cursor(ledger, "litellm") == "2026-10-12T00:00:00+00:00"  # the 11th is done
    FakeSource.routes = litellm_routes()
    start_, end = connect.period(ledger, "litellm", None, None, "2026-10-13")
    assert start_ == datetime(2026, 10, 11, tzinfo=UTC)  # a day of overlap for late rows
    sources.pull(ledger, "litellm", start_, end, env, mapping)
    assert charges(ledger) == to_micros("0.7623")


def test_litellm_csv_export(tmp_path, ledger):
    path = tmp_path / "logs.csv"
    path.write_text("request_id,startTime,model,spend,end_user,metadata,request_tags\n"
                    'r1,2026-10-12T09:00:00Z,openai/gpt-4o,1.25,acme,"{""user_api_key_alias"": ""bot""}",'
                    '"[""task:t1""]"\n')
    mapping = sources.load_map("litellm", None)
    sources.pull(ledger, "litellm", *OCT, {}, mapping, file=str(path))
    assert ledger.db.execute("select agent, task, customer, vendor, amount from entries").fetchone() == (
        "bot", "t1", "acme", "openai", to_micros("1.25"))


# The vendor cost APIs

def test_openai_costs_book_the_remainder_by_project(url, ledger):
    body = {"value": fixture("openai_costs.json")}
    FakeSource.routes = {"/v1/organization/costs": lambda q: (200, body["value"])}
    gateway_spend(ledger, "openai", "gpt-4o", "1.00")  # the gateway already saw $1 of the 12th
    env = {"OPENAI_ADMIN_KEY": KEY, "QUARTER_OPENAI_ADMIN_URL": url}
    mapping = {"projects": {"proj_support": "support-bot", "proj_research": "researcher"}}
    sources.pull(ledger, "openai", *OCT, env, mapping)
    query = FakeSource.requests[0][2]
    assert query["group_by"] == ["line_item", "project_id"]
    by_agent = dict(ledger.db.execute(
        "select coalesce(agent, '-'), sum(amount) from entries where call_id like 'obs:%' group by agent"))
    # $5 billed for gpt-4o on the 12th, $1 of it through the gateway: $4 split 4:1 by project
    assert by_agent == {"support-bot": to_micros("3.2"), "researcher": to_micros("0.8"), "-": to_micros("0.25")}
    assert sum(ledger.spend_by("vendor", *OCT).values()) == to_micros("5.25")  # equals the bill

    again = sources.pull(ledger, "openai", *OCT, env, mapping)
    assert (again.booked, again.revised, again.amount) == (0, 0, 0)
    body["value"] = fixture("openai_costs.json").replace(b'"value": 0.25', b'"value": 0.75')
    revised = sources.pull(ledger, "openai", *OCT, env, mapping)
    assert (revised.revised, revised.amount) == (1, to_micros("0.5"))  # the vendor changed the 13th

    # a detailed source books $2 of the 12th later: re-reading the totals takes it back off
    connect.book(ledger, "litellm", [Observed("x", "2026-10-12T12:00:00+00:00", "openai", to_micros(2),
                                              model="gpt-4o-2024-08-06", agent="support-bot")])
    sources.pull(ledger, "openai", *OCT, env, mapping)
    assert sum(ledger.spend_by("vendor", *OCT).values()) == to_micros("5.75")
    # (no chain check here: gateway_spend redates an entry after it was hashed)


def test_anthropic_cost_report_by_workspace_and_finished_days_only(url, ledger):
    FakeSource.routes = {"/v1/organizations/cost_report": lambda q: (200, fixture("anthropic_cost_report.json"))}
    env = {"ANTHROPIC_ADMIN_KEY": KEY, "QUARTER_ANTHROPIC_ADMIN_URL": url}
    pulled = sources.pull(ledger, "anthropic", *OCT, env, {"workspaces": {"wrkspc_eng": "claude-code"}})
    assert pulled.amount == to_micros(4)  # cents: "300" + "100"
    headers, query = FakeSource.requests[0][1:]
    assert headers["X-Api-Key"] == KEY and query["group_by[]"] == ["description", "workspace_id"]
    assert dict(ledger.db.execute("select coalesce(agent, '-'), amount from entries")) == {
        "claude-code": to_micros(3), "-": to_micros(1)}

    FakeSource.requests.clear()
    today = datetime(2026, 10, 12, 15, tzinfo=UTC)
    sources.pull(ledger, "anthropic", datetime(2026, 10, 12, tzinfo=UTC), today, env, {}, at=today)
    assert FakeSource.requests == []  # the 12th has not finished: nothing to read


# OpenRouter

def test_openrouter_activity_by_key(url, ledger):
    at = datetime.now(UTC)
    day = (at - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = f"{day:%Y-%m-%d}"

    def activity(query):
        if query["date"] != [stamp]:
            return 200, b'{"data": []}'
        name = "openrouter_activity_key.json" if "api_key_hash" in query else "openrouter_activity.json"
        return 200, fixture(name, DAY=stamp)

    FakeSource.routes = {"/api/v1/activity": activity}
    env = {"QUARTER_OPENROUTER_URL": url, "QUARTER_OPENROUTER_MANAGEMENT_KEY": KEY}
    sources.pull(ledger, "openrouter", day - timedelta(days=40), at, env, {"keys": {"abc123": "researcher"}}, at=at)
    assert {tuple(r[2]["date"]) for r in FakeSource.requests} == {(stamp,)} | {
        (f"{d:%Y-%m-%d}",) for d in (day - timedelta(days=n) for n in range(-1, 29))}  # 30 days, not 40
    assert dict(ledger.db.execute("select coalesce(agent, '-'), sum(amount) from entries group by agent")) == {
        "researcher": to_micros(6), "-": to_micros(4)}
    assert ledger.db.execute("select distinct vendor, model from entries").fetchall() == [("openrouter", "openai/gpt-4.1")]


def test_openrouter_generations_from_logged_ids(url, ledger, tmp_path):
    FakeSource.routes = {"/api/v1/generation": lambda q: (200, fixture("openrouter_generation.json"))}
    ids = tmp_path / "ids.csv"
    ids.write_text("id,agent\ngen-abc123,writer\n")
    env = {"QUARTER_OPENROUTER_URL": url, "OPENROUTER_API_KEY": KEY}
    mapping = sources.load_map("openrouter-gen", None)
    sources.pull(ledger, "openrouter-gen", *OCT, env, mapping, file=str(ids))
    assert ledger.db.execute("select agent, task, customer, amount, at from entries").fetchone() == (
        "writer", "sess-or-1", "initech", to_micros("0.0042"), "2026-10-12T08:00:00+00:00")
    sources.pull(ledger, "openrouter-gen", *OCT, env, mapping, file=str(ids))
    assert len(FakeSource.requests) == 1  # an id already booked is not asked for again


# Cloudflare AI Gateway

def test_cloudflare_logs_with_metadata(url, ledger, monkeypatch):
    monkeypatch.setattr(sources, "CLOUDFLARE_PAGE", 2)
    path = "/client/v4/accounts/acct1/ai-gateway/gateways/gw1/logs"

    def logs(query):
        if query["start_date"] != ["2026-10-12T00:00:00Z"]:
            return 200, b'{"success": true, "result": [], "result_info": {"total_count": 0}}'
        return 200, fixture(f"cloudflare_logs_page{query['page'][0]}.json")

    FakeSource.routes = {path: logs}
    env = {"QUARTER_CLOUDFLARE_URL": url, "QUARTER_CLOUDFLARE_TOKEN": KEY,
           "QUARTER_CLOUDFLARE_ACCOUNT": "acct1", "QUARTER_CLOUDFLARE_GATEWAY": "gw1"}
    span = (datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC))
    pulled = sources.pull(ledger, "cloudflare", *span, env, sources.load_map("cloudflare", None))
    assert (pulled.booked, pulled.amount) == (2, to_micros("0.012"))  # the cached call cost nothing
    assert FakeSource.requests[0][1]["Authorization"] == f"Bearer {KEY}"
    assert ledger.db.execute("select agent, customer, task, vendor from entries order by id").fetchall() == [
        ("sales-bot", "acme", "lead-77", "openai"), (None, None, None, "anthropic")]

    FakeSource.routes = {path: lambda q: (200, b'{"success": false, "errors": [{"code": 10000}], "result": []}')}
    with pytest.raises(ValueError, match="success false"):
        sources.pull(ledger, "cloudflare", *span, env, sources.load_map("cloudflare", None))


# Any CSV

def test_generic_csv_with_a_column_map(ledger):
    text = "When,Cost ($),Team,Client\n2026-10-03,$1.50,ops-bot,acme\n2026-10-03,$1.50,ops-bot,acme\n2026-10-04,2,,\n"
    mapping = {"columns": {"at": "When", "amount": "Cost ($)", "agent": "Team", "customer": "Client"}}
    records = sources.csv_records(text, mapping, "bedrock")
    assert len({r.id for r in records}) == 3  # two identical rows are two rows
    assert records[0].agent == "ops-bot" and records[0].vendor == "bedrock"
    assert sources.csv_records(text, mapping, "bedrock")[0].id == records[0].id  # stable
    connect.book(ledger, "csv", records)
    connect.book(ledger, "csv", sources.csv_records(text, mapping, "bedrock"))
    assert charges(ledger) == to_micros(5)
    with pytest.raises(ValueError, match="need a vendor"):
        sources.csv_records(text, mapping, None)
    with pytest.raises(ValueError, match="names column 'Nope'"):
        sources.csv_records(text, {"columns": {"amount": "Nope"}}, "x")
    with pytest.raises(ValueError, match="negative"):
        sources.csv_records("date,amount\n2026-10-01,-3\n", {}, "x")


def test_pick_paths_tags_and_json_text():
    row = {"metadata": '{"customer_id": "acme"}', "request_tags": ["env:prod", "customer:globex"], "user": ""}
    assert pick(row, ["metadata.customer_id"]) == "acme"
    assert pick(row, ["request_tags[customer:]"]) == "globex"
    assert pick(row, ["user", "metadata.missing", "request_tags[env:]"]) == "prod"
    assert pick(row, ["nothing.here"]) is None


# What observed spend may and may not do

def test_observed_spend_counts_in_reports_and_never_in_gateway_decisions(ledger):
    connect.book(ledger, "csv", [Observed(f"r{n}", "2026-10-12T09:00:00+00:00", "openai", to_micros(40),
                                          model="gpt-4o", agent="bot", customer="acme", task="t1", input=10,
                                          output=5) for n in range(3)])
    gateway_spend(ledger, "openai", "gpt-4o", "1", agent="bot", customer="acme")
    # reports and the journal see it
    assert ledger.spend_by("agent", *OCT)["bot"] == to_micros(121)
    rows = lines(ledger, "2026-10", centres={"department": {"bot": "Support"}})
    assert sum(r.debit for r in rows if r.customer == "acme") == 12100  # cents
    assert {r.centres for r in rows if r.debit} == {("Support",)}
    assert margins(ledger, "2026-10", {"acme": to_micros(100)})["rows"][0]["ai_cost"] == to_micros(121)
    assert why(ledger, "t1")["calls"] == 3
    # caps, standing approvals, loop stops and the register do not
    cap = Cap("agent", "bot", "total", to_micros(10))
    assert ledger.spent("agent", "bot", None) == to_micros(1)
    assert ledger.spent("agent", "bot", None, observed=True) == to_micros(121)
    decision, _ = ledger.hold([cap], {"agent": "bot"}, "openai", "gpt-4o", to_micros(5))
    assert decision.outcome == "allow"
    text = status.render(ledger, [Cap("agent", "bot", "month", to_micros(10))], now=datetime(2026, 10, 20, tzinfo=UTC))
    assert "$6.00 of $10.00" in text  # $1 charged, $5 held and "+ $120.00 observed, alert only" in text
    connect.book(ledger, "csv", [Observed("now", datetime.now(UTC).isoformat(), "openai", to_micros(500), agent="bot")])
    assert runaway.check(ledger, "bot", runaway.RunawayRule()) is None


def test_why_on_an_observed_task(ledger, tmp_path, capsys):
    db = str(tmp_path / "ledger.db")
    connect.book(ledger, "litellm", [Observed("r1", "2026-10-12T09:00:00+00:00", "openai", to_micros(2),
                                              task="t9", input=100, output=10)])
    main(["--db", db, "why", "t9"])
    assert "observed by connect" in capsys.readouterr().out


def test_spike_and_budget_alerts_are_sent_once(ledger):
    at = datetime.now(UTC)
    today = at.replace(hour=0, minute=0, second=0, microsecond=0)
    usual = [Observed(f"d{n}", (today - timedelta(days=n)).isoformat(), "openai", to_micros(2), agent="bot")
             for n in range(1, 8)]
    connect.book(ledger, "csv", usual)
    jump = connect.book(ledger, "csv", [Observed("today", today.isoformat(), "openai", to_micros(50),
                                                 agent="bot", customer="acme")])
    sent = []
    caps = [Cap("customer", "acme", "month", to_micros(40))]
    texts = connect.alert(ledger, caps, jump.days, Spike(), lambda text, ref: sent.append(text), at)
    assert any("agent bot spent $50.00" in t and "usual $2.00" in t for t in sent)
    assert any("customer acme spent $50.00" in t and "no spend in the 7 days" in t for t in sent)
    assert any(t.startswith("Budget: the $40.00 monthly cap on customer acme has been reached") for t in sent)
    assert texts == sent
    assert connect.alert(ledger, caps, jump.days, Spike(), lambda text, ref: sent.append(text), at) == []
    old = connect.book(ledger, "csv", [Observed("old", (today - timedelta(days=20)).isoformat(), "openai",
                                                to_micros(500), agent="bot")])
    assert connect.spikes(ledger, old.days, Spike(), at) == []  # backfilled history is not news
    assert len(ledger.db.execute("select * from audit where action = 'spike_alert'").fetchall()) == 2


def test_alerts_reach_slack(ledger, monkeypatch):
    posted = []

    class FakeSlack(BaseHTTPRequestHandler):
        def do_POST(self):
            posted.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    slack = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeSlack))
    client = Slack("xoxb-test", "s", "C1", {}, api=f"http://127.0.0.1:{slack.server_port}")
    monkeypatch.setattr(Slack, "from_env", classmethod(lambda cls, env: client))
    try:
        sources.notifier(ledger, {})("Spend jump: <!channel>", "ref")
    finally:
        slack.shutdown()
    assert posted == [{"channel": "C1", "text": "Spend jump: &lt;!channel&gt;"}]
    monkeypatch.setattr(Slack, "from_env", classmethod(lambda cls, env: None))
    with pytest.raises(ValueError, match="https"):
        sources.notifier(ledger, {"QUARTER_APPROVAL_WEBHOOK": "http://example.com"})


def test_connect_command(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "ledger.db")
    file = tmp_path / "spend.csv"
    file.write_text("id,date,amount_usd,model,agent,customer\na1,2026-10-05,3.10,gpt-4o,bot,acme\n")
    for var in ("QUARTER_SLACK_BOT_TOKEN", "QUARTER_APPROVAL_WEBHOOK"):
        monkeypatch.delenv(var, raising=False)
    main(["--db", db, "connect", "csv", "--file", str(file), "--vendor", "openai"])
    assert "1 new, 0 revised, 0 already booked; $3.10 added" in capsys.readouterr().out
    main(["--db", db, "connect", "csv", "--file", str(file), "--vendor", "openai"])
    assert "0 new, 0 revised, 1 already booked" in capsys.readouterr().out
    main(["--db", db, "report", "--by", "agent"])
    assert "bot" in capsys.readouterr().out
    main(["--db", db, "report", "--by", "agent", "--month", "2026-10"])
    assert "bot" in capsys.readouterr().out
    main(["--db", db, "report", "--by", "agent", "--month", "2026-09"])
    assert "bot" not in capsys.readouterr().out
    main(["--db", db, "agents", "list"])
    assert "bot" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="set QUARTER_LITELLM_URL"):
        main(["--db", db, "connect", "litellm", "--since", "2026-10-01"])
    audit = Ledger(db).db.execute("select count(*) from audit where action = 'observed_pull'").fetchone()[0]
    assert audit == 2


def test_reconcile_still_shows_spend_outside_the_gateway(url, ledger):
    from quarter_ledger.reconcile import quarter_side

    FakeSource.routes = {"/v1/organization/costs": lambda q: (200, fixture("openai_costs.json"))}
    gateway_spend(ledger, "openai", "gpt-4o", "1.00")
    sources.pull(ledger, "openai", *OCT, {"OPENAI_ADMIN_KEY": KEY, "QUARTER_OPENAI_ADMIN_URL": url}, {})
    connect.book(ledger, "litellm", [Observed("y", "2026-10-13T01:00:00+00:00", "openai", to_micros(1), model="o3")])
    seen, _ = quarter_side(ledger, "openai", "2026-10")
    assert seen == {"gpt-4o": to_micros(1), "o3": to_micros(1)}  # the gateway and LiteLLM, not the bill itself
