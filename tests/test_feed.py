import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from test_egress import VOLUME_DELETE, FakeApi, as_agent
from test_proxy import ADMIN_KEY, AGENT_KEY, HEADERS, PRICES, call, start

from quarter_ledger.feed import Feed, checked, load_payees, payee_flags, sign_feed, verified
from quarter_ledger.money import to_micros
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.receipts import public_key
from quarter_ledger.rules import load_rules
from quarter_ledger.store import Ledger

KEY = "ed25519:" + "11" * 32
PUBLIC = public_key(KEY)
DROP = {"tool": "Bash", "command": "drop\\s+database", "action": "approve"}


def body(version=1, prices=None, rules=None):
    return {"version": version, "issued": "2026-11-02", "prices": prices or {}, "rules": rules or []}


def test_only_a_feed_signed_with_the_key_is_taken():
    envelope = sign_feed(body(rules=[DROP]), KEY)
    assert verified(envelope, PUBLIC) == envelope["feed"]
    assert verified({**envelope, "feed": body(version=2, rules=[DROP])}, PUBLIC) is None  # changed after signing
    assert verified(envelope, public_key("ed25519:" + "22" * 32)) is None
    assert verified({"feed": envelope["feed"]}, PUBLIC) is None


@pytest.mark.parametrize("bad, error", [
    (body(version=0), "version"),
    (body(rules=[{**DROP, "action": "deny"}]), "never deny"),
    (body(rules=[{"upstream": "github", "action": "allow"}]), "tool rules only"),
    (body(prices={"x": {"m": {"input_per_million": "lots"}}}), "feed prices"),
])
def test_a_feed_can_only_make_a_call_wait_never_stop_one(bad, error):
    with pytest.raises(ValueError, match=error):
        checked(bad)


def gateway(tmp_path, own_rules):
    ledger = Ledger(str(tmp_path / "l.db"))
    return Gateway(ledger, [], PRICES, {"fake": "https://fake.example"}, rules=own_rules)


def test_the_customer_wins_and_the_feed_fills_in(tmp_path):
    own = load_rules([{"tool": "Bash", "command": "rm\\s+-rf", "action": "deny"}])
    gw = gateway(tmp_path, own)
    feed = Feed(gw, "https://feed.example/feed.json", PUBLIC,
                {"fake": {"m": {"input_per_million": "3", "output_per_million": "4"}}}, own)
    prices = {"fake": {"m": {"input_per_million": "9", "output_per_million": "9"},
                       "new-model": {"input_per_million": "5", "output_per_million": "6"}}}
    raw = json.dumps(sign_feed(body(prices=prices, rules=[DROP]), KEY)).encode()
    assert feed.take(raw)
    assert gw.prices.price("fake", "m").input_per_million == to_micros(3)  # the customer's price stands
    assert gw.prices.price("fake", "new-model").input_per_million == to_micros(5)  # the feed adds a model
    assert gw.prices.price("anthropic", "claude-opus-5-5")  # built-in prices stay
    assert gw.rules.listed() == ["rule 1: deny tool Bash command rm\\s+-rf",
                                 "feed rule 1: approve tool Bash command drop\\s+database"]
    reply = {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "psql -c 'drop database prod'"}}]}
    assert str(gw.rules.held("a1", reply)[0][3]).startswith("feed rule 1")
    assert not feed.take(raw)  # the same version again changes nothing
    applied = gw.ledger.db.execute("select detail from audit where action = 'feed_applied'").fetchall()
    assert len(applied) == 1 and json.loads(applied[0][0])["version"] == 1
    assert (tmp_path / "l.feed.json").read_bytes() == raw  # kept for a restart offline


def test_a_bad_address_or_key_stops_the_gateway_from_starting(tmp_path):
    gw = gateway(tmp_path, load_rules([]))
    with pytest.raises(ValueError, match="https"):
        Feed(gw, "http://feed.example/feed.json", PUBLIC, None, gw.rules)
    with pytest.raises(ValueError, match="ed25519-pub"):
        Feed(gw, "https://feed.example/feed.json", "", None, gw.rules)


class Publisher(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    served: ClassVar[bytes] = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-length", str(len(Publisher.served)))
        self.end_headers()
        self.wfile.write(Publisher.served)

    def log_message(self, *args):
        pass


def test_fetched_feeds_are_checked_and_a_restart_uses_the_kept_one(tmp_path):
    server = start(ThreadingHTTPServer(("127.0.0.1", 0), Publisher))
    url = f"http://127.0.0.1:{server.server_port}/feed.json"
    try:
        gw = gateway(tmp_path, load_rules([]))
        good = sign_feed(body(rules=[DROP]), KEY)
        Publisher.served = json.dumps({**good, "feed": body(version=5, rules=[DROP])}).encode()
        assert not Feed(gw, url, PUBLIC, None, gw.rules).fetch()
        refused = gw.ledger.db.execute("select detail from audit where action = 'feed_refused'").fetchone()
        assert "signature does not verify" in refused[0]
        Publisher.served = json.dumps(good).encode()
        assert Feed(gw, url, PUBLIC, None, load_rules([])).fetch()

        restarted = gateway(tmp_path, load_rules([]))
        Publisher.served = b"down"
        feed = Feed(restarted, url, PUBLIC, None, load_rules([]))
        feed.start()
        feed.stop()
        assert restarted.rules.listed() == ["feed rule 1: approve tool Bash command drop\\s+database"]
    finally:
        server.shutdown()


def test_a_malformed_feed_is_refused_not_raised(tmp_path):
    gw = gateway(tmp_path, load_rules([]))
    feed = Feed(gw, "https://feed.example/feed.json", PUBLIC, None, gw.rules)
    for bad in (body(rules="Bash"), body(prices={"x": "y"}), body(prices={"x": {"m": {"input_per_million": "lots"}}})):
        assert not feed.take(json.dumps(unchecked_envelope(bad)).encode())
    assert not feed.take(b"not json")
    assert gw.ledger.db.execute("select count(*) from audit where action = 'feed_refused'").fetchone()[0] == 4


def unchecked_envelope(feed_body):
    """Signed without the publishing check, as a broken or hostile publisher might."""
    from quarter_ledger import ed25519
    from quarter_ledger.feed import canonical

    return {"feed": feed_body, "signature": ed25519.sign(bytes.fromhex(KEY[8:]), canonical(feed_body)).hex()}


def test_a_rule_says_in_words_why_it_held_a_call():
    rules = load_rules([{**DROP, "why": "drops a database"}])
    assert rules.listed() == ["rule 1: approve, drops a database"]
    reply = {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "drop database prod"}}]}
    assert str(rules.held("a1", reply)[0][3]) == "rule 1: approve, drops a database"
    with pytest.raises(ValueError, match="unknown"):
        load_rules([{**DROP, "reason": "x"}])


# Version 2: HTTP rules, for agents that act on APIs through the gateway

VOLUME = {"id": "railway.volume", "service": "Railway", "host": ["backboard.railway.*", "127.0.0.1"],
          "method": ["POST"], "path": "^/graphql/v2$", "graphql": ["volumeDelete", "volumeInstanceBackupDelete"],
          "action": "approve", "why": "deletes a Railway volume or its backups", "source": "https://docs.railway.com"}
REPO = {"id": "github.repo", "service": "GitHub", "host": ["api.github.com", "127.0.0.1"], "method": ["DELETE"],
        "path": "^/repos/[^/]+/[^/]+$", "action": "approve", "why": "deletes a GitHub repository",
        "source": "https://docs.github.com/rest"}


@pytest.mark.parametrize("bad, error", [
    ({**REPO, "action": "deny"}, "action approve"),
    ({**REPO, "path": "("}, "bad path"),
    ({**REPO, "method": "DELETE"}, "lists of strings"),
    ({k: v for k, v in REPO.items() if k != "path"}, "a path"),
])
def test_a_feed_http_rule_can_only_ask(bad, error):
    with pytest.raises(ValueError, match=error):
        checked({**body(), "http": [bad]})


def test_an_http_rule_with_a_field_this_version_does_not_know_is_skipped():
    _, rules = checked({**body(rules=[DROP]), "http": [REPO, {**VOLUME, "header": "x-amz-target: *Delete*"}]})
    assert [r.id for r in rules.feed_calls] == ["github.repo"]


def test_feed_http_rules_hold_calls_the_customer_left_open(tmp_path):
    api = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
    FakeApi.calls, FakeApi.reply = [], {"ok": True}
    base = f"http://127.0.0.1:{api.server_port}"
    own = load_rules([{"upstream": "github", "method": ["DELETE"], "path": "/repos/acme/keep", "action": "allow"}])
    agent = "agent-key-0123456789"
    gw = Gateway(Ledger(str(tmp_path / "l.db")), [], PRICES, {"railway": base, "github": base}, {agent},
                 vendor_keys={"railway": "rw", "github": "gh"}, rules=own)
    feed = Feed(gw, "https://feed.example/feed.json", PUBLIC, None, own)
    assert feed.take(json.dumps(sign_feed({**body(version=2), "http": [VOLUME, REPO]}, KEY)).encode())
    assert gw.rules.listed()[-2:] == ["feed rule railway.volume: approve, deletes a Railway volume or its backups",
                                      "feed rule github.repo: approve, deletes a GitHub repository"]
    server = start(serve(gw, port=0))
    url, headers = f"http://127.0.0.1:{server.server_port}", as_agent(agent)
    try:
        status, reply = call(url + "/railway/graphql/v2", VOLUME_DELETE, headers)
        assert status == 403 and json.loads(reply)["reason"].startswith("feed rule railway.volume")
        aliased = {"query": "mutation { keep: volumeDelete(volumeId: 1) }"}
        assert call(url + "/railway/graphql/v2", aliased, headers)[0] == 403
        assert call(url + "/railway/graphql/v2", {"query": "query { me { name } }"}, headers)[0] == 200
        assert call(url + "/github/repos/acme/app", headers=headers, method="DELETE")[0] == 403
        assert call(url + "/github/repos/acme/keep", headers=headers, method="DELETE")[0] == 200  # theirs first
        assert call(url + "/github/repos/acme/app", headers=headers, method="GET")[0] == 200
        assert [(m, p) for m, p, *_ in FakeApi.calls] == [
            ("POST", "/graphql/v2"), ("DELETE", "/repos/acme/keep"), ("GET", "/repos/acme/app")]
        (asked,) = {a["model"] for a in gw.ledger.approvals() if "volumeDelete" in a["model"]}
        assert asked == "POST /graphql/v2 volumeDelete"  # the approval names the field, so it covers only that
    finally:
        server.shutdown()
        api.shutdown()


def test_a_version_3_feed_with_mcp_ratings_is_taken_and_the_ratings_ignored(tmp_path):
    """Feed version 3 adds `mcp`, Quarter's ratings of MCP server tools. The gateway does not act on
    them yet, so it takes the feed for its prices and rules and leaves the ratings alone."""
    mcp = [{"server": "github", "service": "GitHub", "package": ["github/github-mcp-server"], "names": ["*github*"],
            "source": "https://github.com/github/github-mcp-server/blob/main/README.md",
            "tools": [{"tool": "delete_repository", "rating": "ask", "why": "deletes a GitHub repository"}]}]
    v3 = {**body(version=3, rules=[DROP]), "http": [REPO], "mcp": mcp}
    _, rules = checked(v3)
    assert [r.id for r in rules.feed_calls] == ["github.repo"]
    gw = gateway(tmp_path, load_rules([]))
    feed = Feed(gw, "https://feed.example/feed.json", PUBLIC, None, gw.rules)
    assert feed.take(json.dumps(sign_feed(v3, KEY)).encode())
    assert feed.version == 3
    assert gw.rules.listed() == ["feed rule 1: approve tool Bash command drop\\s+database",
                                 "feed rule github.repo: approve, deletes a GitHub repository"]


PAY_TO = "0x6d6E695b09861467c7d462f5AAF31cF3540B9192"
SEARCH = "https://api.exa.ai/search"
PAYEES = [
    {"id": "x402.exa", "service": "Exa", "status": "known",
     "pay_to": [{"address": PAY_TO, "network": "eip155:8453"}, {"address": "12Ec2cJm", "network": "solana"}],
     "prices": [{"resource": SEARCH, "max_usd": "0.007"}],
     "why": "listed in the x402 Bazaar", "source": "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"},
    {"id": "x402.drainer", "service": "Drainer", "status": "caution", "hosts": ["*.drain.example"],
     "why": "a public report says it asked agents for their whole balance", "source": "https://report.example/1"},
]


def flags(payee, amount_usd, network="eip155:8453", resource=SEARCH, rail="x402"):
    return payee_flags(load_payees(PAYEES), rail, payee, network, resource, to_micros(amount_usd))


def test_a_listed_payee_asks_only_when_cautioned_or_far_over_its_price():
    assert flags(PAY_TO.lower(), "0.07") is None  # ten times the listed price is still normal
    assert flags(PAY_TO, "0.0701") == "feed payee x402.exa: $0.0701 is over 10 times the $0.007 Exa lists for " + SEARCH
    assert flags(PAY_TO, "5", resource="https://api.exa.ai/answer") is None  # no price for that resource
    assert flags(PAY_TO, "5", network="eip155:137") is None  # the same address elsewhere is someone else
    assert flags(PAY_TO, "5", network=None)  # a payment that names no network matches the address
    assert flags("12Ec2cJm", "5", network="solana")  # x402 v1 network names are read as CAIP-2
    assert flags("12EC2CJM", "5", network="solana") is None  # Solana addresses are case-sensitive
    assert flags(PAY_TO, "5", rail="card") is None  # an address is an x402 payTo only
    assert flags("0xNobody", "500") is None  # a payee no one lists is not suspect
    why = "feed payee x402.drainer: a public report says it asked agents for their whole balance"
    assert flags("0xNobody", "0.01", resource="https://api.drain.example/x?y=1") == why
    assert flags("0xNobody", "0.01", resource="https://drain.example.org/") is None
    assert flags(PAY_TO, "5", resource="http://[nonsense") is None  # an unreadable resource has no price


def test_a_payee_this_version_cannot_read_is_skipped_and_a_broken_one_refused():
    newer = [{**PAYEES[1], "since": "2026"}, {**PAYEES[1], "status": "block"}]
    assert load_payees(newer) == []
    for bad in ({**PAYEES[1], "why": ""}, {**PAYEES[1], "hosts": []},
                {**PAYEES[0], "prices": [{"resource": SEARCH, "max_usd": "free"}]}):
        with pytest.raises(ValueError, match="payee"):
            checked({**body(version=4), "payees": [bad]})


def test_feed_payees_send_a_payment_to_a_person_with_the_reason(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    gw = Gateway(ledger, [], PRICES, {}, {AGENT_KEY}, ADMIN_KEY, signing_key="s" * 32)
    feed = Feed(gw, "https://feed.example/feed.json", PUBLIC, None, gw.rules)
    assert feed.take(json.dumps(sign_feed({**body(version=4), "payees": PAYEES}, KEY)).encode())
    applied = ledger.db.execute("select detail from audit where action = 'feed_applied'").fetchone()[0]
    assert json.loads(applied)["payees"] == 2
    server = start(serve(gw, port=0))
    try:
        url = f"http://127.0.0.1:{server.server_port}/authorize"
        pay = {"rail": "x402", "payee": PAY_TO, "network": "eip155:8453", "resource": SEARCH}
        assert call(url, {**pay, "amount_usd": "0.007"}, HEADERS)[0] == 200
        assert call(url, {**pay, "payee": "0xNobody", "amount_usd": "3"}, HEADERS)[0] == 200
        status, reply = call(url, {**pay, "amount_usd": "3"}, HEADERS)
        reply = json.loads(reply)
        assert status == 403 and reply["error"] == "approval_required"
        assert reply["reason"].startswith("feed payee x402.exa: $3 is over 10 times")
        status, reply = call(url, {**pay, "resource": "https://api.drain.example/pay", "amount_usd": "0.01"}, HEADERS)
        assert status == 403 and "asked agents for their whole balance" in json.loads(reply)["reason"]
        reasons = [a["reason"] for a in ledger.approvals()]
        assert len(reasons) == 2 and all(r.startswith("feed payee ") for r in reasons)
    finally:
        server.shutdown()
