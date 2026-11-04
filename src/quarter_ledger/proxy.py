"""The gateway agents call instead of the vendor, plus the finance dashboard.

Agents: GET, POST, PUT, PATCH or DELETE /{vendor}/{path} goes to that vendor's base URL.
A call to an API that is not a model (no price, or PUT, PATCH or DELETE with no model) is
not held or charged; it is audited as an upstream_call. Rules (rules.py) may deny any call
or send it to a person first, and may hold a model reply, streamed or not, whose tool call
looks destructive until a person decides (streamed: see streamed.py). Headers:
  x-quarter-key       an agent key (required when agent keys are configured)
  x-quarter-agent     optional: else the name given to the agent key, else "default". A
                      named key is that agent only: a different name is refused (403)
  x-quarter-task, x-quarter-customer  optional; Claude Code's session id is the default task
  x-quarter-approval  an approved approval id, to retry a call that needed approval
  x-quarter-wait      seconds (at most 300, or QUARTER_MAX_WAIT) to hold a call, or a
                      POST /authorize, that needs approval open until someone decides; approved, it goes on
An agent key can instead arrive where the vendor key goes (Authorization: Bearer, or
x-api-key). It is then swapped for the vendor key from the gateway's environment. Otherwise
the vendor's own key travels with the request. Vendor keys are never stored.

A model with no price is still forwarded: it is held and charged at the vendor's highest
known prices, and audited once a day as unknown_price.

Before forwarding, the worst-case cost is held: every request byte counted as an input
token (tokens are at least one byte) plus max_tokens of output, or the flat per-request
price. Over a cap: 402. Over an approval limit: 403 with an approval id. Neither call
reaches the vendor.
Past QUARTER_FREE_DECISIONS in a month, calls are decided as ever and each reply carries
x-quarter-free-limit: exceeded.

Streamed responses are passed through as they arrive, and usage is read from the stream
(OpenAI-style chat completions are asked to include it). When a vendor reports no usage,
the hold is charged in full, so spend is never undercounted.

Finance: /dashboard (HTTP basic auth, password = a person's key; ?month=YYYY-MM), /approvals (JSON),
POST /approvals/{id}/approve or /deny, GET /forecast, POST /costs to record what is
spent on agents (see store.add_cost), and /audit (JSON) for every finance action and failed sign-in.
The finance role downloads a month's files, as the CLI writes them: GET /export?month=&format=
(csv, quickbooks, xero, netsuite or focus), /audit-pack?month= and /approvals/export?month=.
POST /attribution saves the cost centre map the journals split by (export.read_centres),
each change in the audit log.
GET /tasks/{id}/why says where a task's money went (see forensics.py). POST /slack/actions takes
Approve and Deny clicks from Slack, signed with the Slack app's signing secret. POST
/approvals/stripe takes Stripe's signed approval request events into the approval record
(see approval_record.py). Ten wrong admin keys from
one address in five minutes lock it out for the rest of that window.

People (QUARTER_PEOPLE) sign in with their own key and act within their role: a viewer reads,
an approver also decides approvals and resumes agents, finance also records costs, an
admin does everything. QUARTER_ADMIN_KEY is an admin named "admin".
"""

import base64
import gzip
import hashlib
import hmac
import io
import json
import math
import os
import queue
import re
import secrets
import socket
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import (
    approval_record,
    bills,
    dashboard,
    forensics,
    issuing,
    learn,
    mandate,
    receipts,
    streamed,
    upstream,
)
from .evidence import audit_pack
from .export import FORMATS, centre_headers, focus, focus_rows, journal, read_centres
from .failopen import FailOpenLog
from .feed import payee_flags
from .insights import forecast
from .money import MAX_AMOUNT, to_micros, usd_display
from .payments import AUTH_TTL, RAILS, authorisation, payee_rule, x402_settlement
from .policy import Cap, Decision, Shadow, cap_label
from .pricing import Price, PriceBook, UnknownPrice
from .redact import StreamRestorer, restore_value
from .rules import Rules, fingerprint, graphql_operations, hide_secrets, without_tool_calls
from .runaway import RunawayRule, check
from .sanctions import Screen
from .slack import Slack, escape
from .store import Ledger, month_range

DEFAULT_MAX_TOKENS = 4096
UPSTREAMS = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "openrouter": "https://openrouter.ai",
    # Codex signed in with a ChatGPT plan, as OpenAI's gateway guide routes it: the plan pays.
    "chatgpt": "https://chatgpt.com",
}
# Vendors whose calls a plan pays for, priced at another vendor's list price for the record.
PRICED_AS = {"chatgpt": "openai"}
VENDOR_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
FORWARDED_HEADERS = ("authorization", "x-api-key", "content-type", "accept", "chatgpt-account-id")
# Forwarded as open families, as Anthropic asks of gateways: new capabilities arrive as new
# anthropic-* headers (and anthropic-beta carries the OAuth capability a claude.ai login needs).
FORWARDED_PREFIXES = ("anthropic-", "openai-")
# Never forwarded, even when an upstream lists them: the connection's own, and the agent's.
HOP_BY_HOP = {
    "host", "connection", "keep-alive", "transfer-encoding", "content-length", "te", "upgrade",
    "proxy-authorization", "cookie", "accept-encoding",
}
RESERVED = {  # first path segments the gateway answers itself, never a vendor's
    "dashboard", "approvals", "healthz", "costs", "audit",
    "forecast", "agents", "authorize", "settle", "slack", "tasks", "export", "audit-pack", "attribution",
    "standing", "bills", "issuing", "openapi.json",
}
# Vendor endpoints that cost nothing: forwarded without a price, a hold or a charge.
FREE_POSTS = ("v1/messages/count_tokens", "v1/responses/input_tokens")
FREE_GETS = ("v1/models", "backend-api/codex/models")
OUTPUT_LIMITS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
# Response headers the gateway sets itself; every other vendor header is relayed.
OWN_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "content-length", "content-type",
    "content-encoding", "date", "server",
}
MAX_BODY = 8 * 1024 * 1024
MAX_KEYLESS_BODY = 64 * 1024  # a caller with no valid key may not make the gateway read more
BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")  # the methods whose body is read and forwarded
CLIENT_TIMEOUT = 60  # seconds a client may stay silent mid-request before it is dropped
# A client sending a byte every few seconds is never silent, so the request line, headers and
# body must all arrive within this; and past MAX_CONNECTIONS a new one is closed at once. Before,
# 1,500 such sockets held 1,500 threads and 119 MB, over a hosted tenant's 96 MB limit.
REQUEST_DEADLINE = 60
MAX_CONNECTIONS = 256
BUDGET_CHECK_EVERY = 60  # seconds between forecast checks
LOGIN_FAILURES, LOGIN_WINDOW = 10, 300  # wrong admin keys allowed per client per 5 minutes
MAX_WAIT = 300  # seconds a call may wait for its approval, unless QUARTER_MAX_WAIT says less
MAX_TRACKED = 10_000  # tasks whose recent tool calls and cache hit rates are kept in memory
COLLAPSE_CALLS = 3  # calls in a row under half a task's usual cache hit rate
# Claude Code's attribution headers (x-claude-code-*), kept on each call's record
HINTS = ("agent-id", "parent-agent-id", "prompt-id", "request-class")
ROLES = ("viewer", "approver", "finance", "admin")  # each may do all that the ones before it may
LEGACY_ADMIN = "admin"  # the name QUARTER_ADMIN_KEY signs in as
UNCHECKED = object()
VIEWER_ROUTES = {"/forecast", "/audit", "/approvals", "/bills", "/approvals/suggestions", "/standing"}
FINANCE_DOWNLOADS = {"/export", "/audit-pack", "/approvals/export"}
OPENAPI = Path(__file__).with_name("openapi.json")  # this API, described
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
# A gateway call's own refusal is in its vendor's error shape, so the vendor's SDK raises a typed
# error with a readable message. Quarter's code and detail stay in the error object.
ERROR_TYPES = {
    400: "invalid_request_error", 401: "authentication_error", 402: "billing_error", 403: "permission_error",
    404: "not_found_error", 411: "invalid_request_error", 413: "request_too_large", 423: "permission_error",
    429: "rate_limit_error",
}
REFUSALS = {  # what happened and what to do, for each refusal an agent can get
    "cap_reached": "Quarter refused this call: {reason}. It did not reach the vendor. It can go once"
                   " spend is back under the cap, or the cap is raised.",
    "approval_required": "Quarter is holding this call for a person ({reason}). It did not reach the vendor."
                         " Once approval {approval_id} is approved, send the same request again with the header"
                         " x-quarter-approval: {approval_id}. Or send x-quarter-wait: <seconds> to wait for the decision.",
    "approval_denied": "{decided_by} denied approval {approval_id}, so Quarter did not send this call.",
    "agent_paused": "Quarter refused this call: {reason}. Its calls are refused until a person resumes the"
                    " agent, on the dashboard or with quarter-ledger resume.",
    "task_paused": "Quarter refused this call: {reason}. Its calls are refused until a person resumes the"
                   " task, on the dashboard or with quarter-ledger resume --task.",
    "rule_denied": "A Quarter rule refused this call ({rule}).",
    "ledger_unavailable": "Quarter could not record this call, so it did not send it. Try again shortly.",
}
APPROVAL_STATUSES = {  # GET /approvals?status=, and the approvals each one lists
    "pending": ("pending",), "approved": ("approved", "used"), "denied": ("denied",),
    "all": ("pending", "approved", "used", "denied"),
}
# Quarter's own routes and their methods, from the spec: a wrong method is answered 405.
ROUTES = [
    (re.compile(re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(path))), set(methods) & {"get", "post", "put", "patch", "delete"})
    for path, methods in json.loads(OPENAPI.read_text())["paths"].items() if not path.startswith("/{")
]
HTML_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


@dataclass
class Gateway:
    ledger: Ledger
    caps: list[Cap]
    prices: PriceBook
    upstreams: dict[str, str]  # in the config a value may also be {"url", "auth", "headers"}
    agent_keys: set[str] = field(default_factory=set, repr=False)
    admin_key: str | None = field(default=None, repr=False)
    approval_webhook: str | None = field(default=None, repr=False)  # a Slack incoming webhook, or anything taking {"text"}
    dashboard_url: str | None = None
    signing_key: str | None = field(default=None, repr=False)  # signs approval receipts
    stripe_secret: str | None = field(default=None, repr=False)  # Stripe Issuing endpoint's whsec_
    stripe_approvals_secret: str | None = field(default=None, repr=False)  # approval events endpoint's whsec_
    org: str | None = None  # the company's domain: named in spend mandates sellers check
    mercury: dict | None = field(default=None, repr=False)  # {token, account, url}: pays bills
    runaway: RunawayRule | None = field(default_factory=RunawayRule)
    fallbacks: dict[str, dict[str, str]] = field(default_factory=dict)  # vendor: {model: cheaper}
    auto_cache: bool = True  # mark Anthropic conversations that send no cache marker (see _cache)
    # rail: allowed payees. Others wait for a person, or with "unlisted": "deny" are refused.
    payees: dict = field(default_factory=dict)
    key_agents: dict[str, str] = field(default_factory=dict, repr=False)  # agent key: agent name
    vendor_keys: dict[str, str] = field(default_factory=dict, repr=False)  # vendor, or vendor:agent: its key
    slack: Slack | None = None  # approvals with Approve and Deny buttons
    people: dict[str, dict] = field(default_factory=dict, repr=False)  # name: {"key", "role"}
    rules: Rules = field(default_factory=Rules)  # what agents may do on each upstream
    redact_key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)  # keys placeholders
    _unpriced: set[str] = field(default_factory=set, repr=False)
    _failures: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _seen: dict[str, list[tuple[float, str]]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_budget_check: float = field(default=-1e9, repr=False)
    _budget_alerted: set[str] = field(default_factory=set, repr=False)
    _waiting: dict[str, threading.Event] = field(default_factory=dict, repr=False)
    _tool_calls: dict[str, list[tuple[float, str, str, str]]] = field(default_factory=dict, repr=False)
    _hits: dict[str, tuple[float, int, int, bool]] = field(default_factory=dict, repr=False)
    _shown: set[str] = field(default_factory=set, repr=False)  # approvals whose outcome Slack has
    _checks: queue.SimpleQueue | None = field(default=None, repr=False)  # agents to check after spend
    _to_check: set[str] = field(default_factory=set, repr=False)  # those queued and not yet checked
    # Set from upstreams given as {"url", "auth", "headers"}: the header an API takes its key
    # in (Railway's Project-Access-Token), and any extra headers it needs forwarded.
    upstream_auth: dict[str, str] = field(default_factory=dict, repr=False)
    upstream_headers: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    # QUARTER_MODE: "shadow" lets every call through that caps, payee lists, pauses and loop
    # stops would hold, and records what they would have done (see store.Ledger.hold).
    mode: str = "enforce"
    # QUARTER_MAX_WAIT: the longest x-quarter-wait, below a proxy's own timeout (Cloudflare's is 125 s)
    max_wait: float = MAX_WAIT
    # QUARTER_CLIENT_IP_HEADER: the header a reverse proxy puts the caller's address in. Behind
    # one, every caller has the proxy's address, so one stranger's wrong keys locked out everyone.
    client_ip_header: str | None = None
    _shadow_paused: set[tuple] = field(default_factory=set, repr=False)  # loop stops recorded
    vendors: upstream.Pool = field(default_factory=upstream.Pool, repr=False)  # keep-alive connections
    # QUARTER_FAIL_OPEN=model: plain model calls go on while the ledger cannot be written (failopen.py)
    fail_open: FailOpenLog | None = None
    # The cost centre map (export.read_centres) finance edits on the dashboard; its journals split by it.
    centres_path: str | None = None
    retention_days: int | None = None  # QUARTER_RETENTION_DAYS, for the audit pack
    rules_config: list | None = field(default=None, repr=False)  # the rules file as parsed, for the audit pack
    sanctions: Screen | None = None  # the stored OFAC lists every payee is screened against

    def centres(self) -> tuple[dict[str, dict[str, str]], str]:
        """The cost centre map ({} with none saved) and its version, a hash of the file. A
        ValueError when the file is not a map read_centres takes."""
        try:
            text = Path(self.centres_path).read_text() if self.centres_path else ""
        except FileNotFoundError:
            text = ""
        version = hashlib.sha256(text.encode()).hexdigest()[:16]
        return (read_centres(text) if text.strip() and json.loads(text) else {}), version

    def save_centres(self, version: str, centres: dict[str, dict[str, str]], actor: str) -> list[str] | None:
        """Write the map, one audit row per change, unless it changed since `version` was read
        (None). The audit rows go in before the file is swapped, so no change goes unrecorded."""
        with self._lock:
            old, now = self.centres()
            if now != version:
                return None
            changes = [
                f"{d} {agent}: {old.get(d, {}).get(agent, '(none)')} -> {centres.get(d, {}).get(agent, '(none)')}"
                for d in dashboard.DIMENSIONS
                for agent in sorted(old.get(d, {}).keys() | centres.get(d, {}).keys())
                if old.get(d, {}).get(agent) != centres.get(d, {}).get(agent)
            ]
            if changes:
                staged = f"{self.centres_path}.tmp"
                Path(staged).write_text(json.dumps(centres, indent=2) + "\n")
                for change in changes:
                    self.ledger.audit("cost_centre", actor, change)
                os.replace(staged, self.centres_path)
            return changes

    @property
    def shadowing(self) -> Shadow:
        """What holds let through and record, beyond caps in shadow themselves."""
        approvers = {name for name, p in self.people.items() if p["role"] != "viewer"}
        approvers |= set(self.slack.users.values()) if self.slack else set()
        approvers |= {LEGACY_ADMIN} if self.admin_key else set()
        return Shadow(self.mode == "shadow", self.payees.get("mode") == "shadow", tuple(sorted(approvers)))

    def in_shadow(self) -> list[str]:
        """Each rule that records instead of refusing, in words, for startup and the dashboard."""
        if self.mode == "shadow":
            also = " and the agent register" if self.ledger.register else ""
            return [f"everything (QUARTER_MODE=shadow): caps, payee lists, pauses and loop stops{also}"]
        out = [f"cap {cap_label(c, c.key)} ({c.window})" for c in self.caps if c.mode == "shadow"]
        out += ["payee lists"] if self.payees.get("mode") == "shadow" else []
        out += ["loop stops"] if self.runaway and self.runaway.shadow else []
        out += ["agent register"] if self.ledger.register and self.ledger.register.shadow else []
        return out

    def may_fail_open(self, ids: dict, held: int, approval_id: str | None) -> bool:
        """Whether a model call may go on unrecorded in the ledger: nothing could send it to
        a person. Its tool calls, an approval it carries, a cap's per-call approval line,
        or the register would."""
        agent = ids["agent"]
        return bool(
            self.fail_open and not approval_id
            and not self.rules.holds_tools(agent)
            and not (self.ledger.register and not self.ledger.register.shadow and self.mode != "shadow")
            and not any(c.approve_above is not None and held > c.approve_above and c.mode != "shadow"
                        and c.applies_to(ids) for c in self.caps)
        )

    def payee_rule(self, rail: str, payee: str) -> str | None:
        """None if the payee may be paid, "ask" if a person must agree, "deny" if refused."""
        return payee_rule(self.payees, rail, payee)

    def screen(self, rail: str, payee: str, approval_id: str | None = None) -> tuple[str | None, str | None]:
        """The sanctions screen's (refuse, ask) for a payment; (None, None) with no list stored.
        approval_id is the approval the payment carries: decided by a named person, it may
        clear the payee of a close match."""
        if not self.sanctions:
            return None, None
        approval = self.ledger.approval(approval_id) if approval_id else None
        if approval and approval["decided_by"] == LEGACY_ADMIN:  # the shared key names no one
            approval = None

        def known() -> bool:  # on the payee list, or paid before
            if payee.lower() in {p.lower() for p in self.payees.get(rail, [])}:
                return True
            return self.ledger.db.execute(
                "select 1 from entries where vendor = ? and kind = 'hold' limit 1", (f"{rail}:{payee}",)
            ).fetchone() is not None

        return self.sanctions.review(self.ledger, rail, payee, known, approval)

    def locked_out(self, client: str) -> bool:
        cutoff = time.monotonic() - LOGIN_WINDOW
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if t > cutoff]
            self._failures[client] = recent
            return len(recent) >= LOGIN_FAILURES

    def announce_approval(
        self, approval_id: str, ids: dict, vendor: str, model: str, amount: int, reason: str
    ) -> None:
        """Tell people an approval is waiting. Runs in the background; a failure is audited."""
        text = approval_text(ids["agent"], ids.get("customer"), vendor, model, amount, reason)
        if self.slack:

            def post():
                self.ledger.set_slack_message(approval_id, *self.slack.post_approval(text, approval_id))
                self._show_decision(approval_id)  # decided while posting

            self._in_background(post, approval_id)
            return
        if not self.approval_webhook:
            return
        if self.dashboard_url:
            text += f" Approve or deny: {self.dashboard_url.rstrip('/')}/dashboard ({approval_id})"
        else:
            text += f" Id: {approval_id}"
        self._notify(text, approval_id)

    def wait_for_decision(self, approval_id: str, seconds: float) -> dict:
        """The approval once decided, or as it stands when the wait ends.

        Only reads the database, so no write lock is held while waiting. A decision made
        in this process wakes the wait at once; one made elsewhere (the CLI) is seen
        within a second."""
        event = threading.Event()
        with self._lock:
            self._waiting[approval_id] = event
        deadline = time.monotonic() + seconds
        try:
            while True:
                approval = self.ledger.approval(approval_id)
                left = deadline - time.monotonic()
                if approval["status"] != "pending" or left <= 0:
                    return approval
                event.wait(min(left, 1.0))
        finally:
            with self._lock:
                self._waiting.pop(approval_id, None)

    def decided(self, approval_id: str) -> None:
        """Wake the call waiting for this approval, if one is, and show the outcome in Slack."""
        with self._lock:
            event = self._waiting.get(approval_id)
        if event:
            event.set()
        if self.slack:
            self._in_background(lambda: self._show_decision(approval_id), approval_id)

    def _show_decision(self, approval_id: str) -> None:
        """Show a decided approval's outcome in Slack once, though the click and the
        posting thread can both get here."""
        if not self.ledger.slack_message(approval_id):
            return  # not posted yet: the posting thread shows it
        if self.ledger.approval(approval_id)["status"] == "pending":
            return
        with self._lock:
            if approval_id in self._shown:
                return
            self._shown.add(approval_id)
        show_decision_in_slack(self.slack, self.ledger, approval_id)

    def check_budgets(self) -> None:
        """Warn once a day per cap when the forecast says a monthly cap will be reached."""
        if not self.approval_webhook:
            return
        with self._lock:
            if time.monotonic() - self._last_budget_check < BUDGET_CHECK_EVERY:
                return
            self._last_budget_check = time.monotonic()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for w in forecast(self.ledger, self.caps)["cap_warnings"]:
            key = f"{w['cap']} {today}"
            if key in self._budget_alerted:
                continue
            self._budget_alerted.add(key)
            when = (
                "has been reached"
                if w["reached"] == "already"
                else f"will be reached on {w['reached']}"
            )
            self.ledger.audit("budget_alert", None, key)
            self._notify(
                f"Budget: the ${usd_display(w['limit'])} monthly cap on {w['cap']} {when} "
                f"at the current rate (month end ${usd_display(w['projected'])}).",
                key,
            )

    def unpriced(self, vendor: str, model: str) -> Price | None:
        """The vendor's highest known prices for a model with none; None if nothing is known."""
        ceiling = self.prices.ceiling(vendor)
        if ceiling.is_flat:
            return None
        key = f"{vendor}/{model or '*'} {time.strftime('%Y-%m-%d', time.gmtime())}"
        with self._lock:
            first = key not in self._unpriced
            self._unpriced.add(key)
        if first:
            self.ledger.audit("unknown_price", None, f"{vendor}/{model or '*'}")
        return ceiling

    def check_repeats(self, agent: str, raw: bytes) -> None:
        """Pause an agent that keeps sending the same request: a retry loop."""
        if self.runaway is None:
            return
        now, digest = time.monotonic(), hashlib.sha256(raw).hexdigest()
        cutoff = now - self.runaway.window.total_seconds()
        with self._lock:
            seen = [s for s in self._seen.get(agent, []) if s[0] > cutoff] + [(now, digest)]
            self._seen[agent] = seen
            count = sum(1 for _, d in seen if d == digest)
        if count >= self.runaway.repeats:
            minutes = self.runaway.window.total_seconds() / 60
            self._pause(agent, f"sent the same request {count} times in {minutes:g} minutes")

    def check_tool_repeats(self, ids: dict, request: dict) -> None:
        """Pause a task whose model keeps making the same tool call and getting the same
        result back. The same call with changing results (tests rerun after each edit) trips
        only at three times the count. Each tool call id counts once, so a retried request or
        a side call carrying the same turn adds nothing."""
        calls = forensics.tool_calls(request) if self.runaway else []
        if not calls:
            return
        scope, now = ids.get("task") or ids["agent"], time.monotonic()
        cutoff, limit = now - self.runaway.window.total_seconds(), self.runaway.tool_repeats
        with self._lock:
            seen = [s for s in self._tool_calls.pop(scope, []) if s[0] > cutoff]
            known = {call_id for _, call_id, *_ in seen if call_id}
            seen += [(now, i, h, r) for i, h, r in calls if not i or i not in known]
            same = max(Counter((h, r) for _, _, h, r in seen).values())
            calls_alike = max(Counter(h for _, _, h, _ in seen).values())
            tripped = same >= limit or calls_alike >= 3 * limit
            if not tripped:  # once tripped, a resumed task starts afresh
                self._tool_calls[scope] = seen
                _forget_oldest(self._tool_calls)
        if tripped:
            minutes = self.runaway.window.total_seconds() / 60
            reason = (f"made the same tool call {same} times in {minutes:g} minutes, with the same result"
                      if same >= limit else f"made the same tool call {calls_alike} times in {minutes:g} minutes")
            self._pause(ids["agent"], reason, task=ids.get("task"))

    def record_call(self, call_id: str, call: dict, usage: dict, cost: int, held: int) -> None:
        """Keep a settled call's anatomy and watch its task's cache. A database failure is
        audited, never raised: the call has been paid for and its answer must reach the agent."""
        ids, hints, model, listed, vendor = (
            call["ids"], call["hints"], call["model"], call["listed"], call["vendor"]
        )
        list_cost = charge(vendor, listed, held, usage)
        lane = (ids.get("task"), hints["agent-id"], hints["request-class"])
        try:
            prev = self.ledger.last_call(lane, call["at"].isoformat()) if ids.get("task") else None
            needs_marker = call["path"].rstrip("/").endswith("messages") or "claude" in model
            parts = forensics.anatomy(
                call["request"], model, usage, call["at"], prev, needs_marker, listed, list_cost,
                lambda u: charge(vendor, listed, 0, u),
            )
            self.ledger.record_call({
                **ids, **parts, "call_id": call_id, "at": call["at"].isoformat(),
                "subagent": hints["agent-id"], "parent_agent": hints["parent-agent-id"],
                "prompt_id": hints["prompt-id"], "request_class": hints["request-class"],
                "vendor": vendor, "model": model, "route": call["route"], "upstream": call["upstream"],
                "redacted": call["redacted"],
                # What the cache Quarter asked for saved, at list price: the same tokens uncached,
                # less what they cost. Below zero on a last turn whose write nobody read.
                "saved": listed.cost(usage.get("input", 0), usage.get("output", 0)) - list_cost
                if call["cache_added"] and "input" in usage and not listed.is_flat else 0,
                "input": usage.get("input", 0), "output": usage.get("output", 0),
                "cache_read": usage.get("cache_read", 0),
                "cache_write": usage.get("cache_write_total", 0),
                "cost": cost, "list_cost": list_cost,
            })
        except sqlite3.Error as e:
            self.ledger.audit("forensics_failed", None, f"{call_id} {e}")
            return
        self.check_collapse(ids.get("task"), usage)

    def check_collapse(self, task: str | None, usage: dict) -> None:
        """Warn once per task when its cache hit rate stays under half its own running
        average for COLLAPSE_CALLS calls in a row."""
        tokens = usage.get("input", 0)
        if not task or tokens < forensics.MIN_CACHEABLE:
            return
        rate = usage.get("cache_read", 0) / tokens
        with self._lock:
            total, n, streak, warned = self._hits.pop(task, (0.0, 0, 0, False))
            usual = total / n if n else 0.0
            streak = streak + 1 if n >= COLLAPSE_CALLS and rate < usual / 2 else 0
            warn = streak >= COLLAPSE_CALLS and not warned
            self._hits[task] = (total + rate, n + 1, streak, warned or warn)
            _forget_oldest(self._hits)
        if warn:
            self.ledger.audit("cache_collapse", None, f"{task} {rate:.2f} against {usual:.2f}")
            where = f"quarter-ledger why {task}"
            if self.dashboard_url:
                where = f"{self.dashboard_url.rstrip('/')}/tasks/{quote(task, safe='')}/why"
            self._alert(
                f"Cache collapse: task {task} read {rate:.0%} of its prompt from the cache,"
                f" under half its usual {usual:.0%}, {COLLAPSE_CALLS} calls in a row. Why: {where}",
                task,
            )

    def check_runaway(self, agent: str) -> None:
        reason = check(self.ledger, agent, self.runaway) if self.runaway else None
        if reason:
            where = self.dashboard_url and f" Resume: {self.dashboard_url.rstrip('/')}/dashboard"
            self._pause(agent, reason, where or "")

    def _pause(self, agent: str, reason: str, where: str = "", task: str | None = None) -> None:
        if self.mode == "shadow" or (self.runaway and self.runaway.shadow):
            with self._lock:  # once per agent or task, as a pause lasts until someone resumes it
                first = (agent, task) not in self._shadow_paused
                self._shadow_paused.add((agent, task))
            if first:
                detail = {"agent": agent, "task": task, "outcome": "block", "reason": f"loop stop: {reason}", "asked": []}
                self.ledger.audit("would_hold", None, json.dumps(detail, sort_keys=True))
            return
        if self.ledger.pause(agent, reason, task):
            who = f"task {task} of {agent}" if task else agent
            self.ledger.audit("paused", None, f"{who}: {reason}")
            if self.approval_webhook:
                stays = "It stays paused until someone resumes it."
                self._notify(f"Paused {who}: it {reason}. {stays}{where}", task or agent)

    def after_spend(self, agent: str) -> None:
        """The budget and runaway checks, in the background so the reply is not held up. One
        thread runs them all: a thread per call opened a database connection per call. An
        agent already waiting for its check is not queued twice; the check reads the latest."""
        with self._lock:
            if self._checks is None:
                self._checks = queue.SimpleQueue()
                threading.Thread(target=self._run_checks, args=(self._checks,), daemon=True).start()
            if agent not in self._to_check:
                self._to_check.add(agent)
                self._checks.put(agent)

    def _run_checks(self, checks: queue.SimpleQueue) -> None:
        try:
            while True:
                agent = checks.get()
                with self._lock:
                    self._to_check.discard(agent)
                for run in (self.check_budgets, partial(self.check_runaway, agent)):
                    try:
                        run()
                    except sqlite3.Error:  # shown, as before; the agent's next spend checks again
                        traceback.print_exc()
        finally:  # anything else ends this thread, shown as before; the next spend starts another
            with self._lock:
                self._checks = None
                self._to_check.clear()

    def _alert(self, text: str, ref: str) -> None:
        """A warning nobody has to act on: to Slack when set up, else to the webhook."""
        if self.slack:
            slack, message = self.slack, {"channel": self.slack.channel, "text": escape(text)}
            self._in_background(lambda: slack.call("chat.postMessage", message), ref)
        elif self.approval_webhook:
            self._notify(text, ref)

    def _notify(self, text: str, ref: str) -> None:
        """Post to the webhook in the background; a failure is audited, never raised."""
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            self.approval_webhook, data=body, headers={"content-type": "application/json"}
        )
        self._in_background(lambda: urllib.request.urlopen(req, timeout=10).close(), ref)

    def _in_background(self, send, ref: str) -> None:
        """Run send() on its own thread; a failure is audited, never raised."""

        def run():
            try:
                send()
            except (OSError, ValueError) as e:  # URLError, TimeoutError and Slack's own errors
                self.ledger.audit("webhook_failed", None, f"{ref} {e}")

        threading.Thread(target=run, daemon=True).start()

    def person(self, key: str | None) -> tuple[str, str] | None:
        """The name and role of whoever holds this key. Every key is compared, in constant time."""
        found = (LEGACY_ADMIN, "admin") if _same(key, self.admin_key) else None
        for name, p in self.people.items():
            if _same(key, p["key"]):
                found = (name, p["role"])
        return found

    def failed_login(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(time.monotonic())

    def __post_init__(self):
        self.upstreams = dict(self.upstreams)  # the caller's config stays as written
        for name, up in list(self.upstreams.items()):
            if isinstance(up, dict):
                if set(up) - {"url", "auth", "headers"} or not isinstance(up.get("url"), str):
                    raise ValueError(f"upstream {name} takes url, and optionally auth and headers")
                self.upstreams[name] = up["url"]
                if up.get("auth"):
                    self.upstream_auth[name] = str(up["auth"]).lower()
                extra = tuple(str(h).lower() for h in up.get("headers", []))
                if any(h in HOP_BY_HOP or h.startswith("x-quarter") for h in (*extra, self.upstream_auth.get(name, ""))):
                    raise ValueError(f"upstream {name} cannot forward {extra}")
                self.upstream_headers[name] = extra
        unknown = {r.to for r in self.rules.routes} - self.upstreams.keys()
        if unknown:
            raise ValueError(f"routes go to upstreams that are not set: {sorted(unknown)}")
        clash = RESERVED & self.upstreams.keys()
        if clash:
            raise ValueError(f"vendor names reserved for the gateway: {sorted(clash)}")
        for name, p in self.people.items():
            if not isinstance(p, dict) or not p.get("key") or p.get("role") not in ROLES:
                raise ValueError(f"person {name} needs a key and a role: {', '.join(ROLES)}")
        keys = [p["key"] for p in self.people.values()] + [self.admin_key] * bool(self.admin_key)
        if len(set(keys)) != len(keys) or set(keys) & self.agent_keys:
            raise ValueError("each person needs a key of their own, apart from the agent keys")
        if self.mode not in ("enforce", "shadow") or self.payees.get("mode", "enforce") not in ("enforce", "shadow"):
            raise ValueError('QUARTER_MODE and the payee list\'s "mode" are "enforce" or "shadow"')
        if self.admin_key and LEGACY_ADMIN in self.people:
            raise ValueError(f"QUARTER_ADMIN_KEY signs in as {LEGACY_ADMIN}; rename that person")
        hook = urlsplit(self.approval_webhook or "https://x")
        if hook.scheme != "https":
            raise ValueError("the approval webhook must use https")
        for name, base in self.upstreams.items():
            parts = urlsplit(base)
            local = parts.hostname in ("localhost", "127.0.0.1", "::1")
            if parts.scheme != "https" and not (parts.scheme == "http" and local):
                raise ValueError(f"upstream {name} must use https: {base}")
        unknown = self.rules.upstreams() - self.upstreams.keys()
        if unknown:  # a misspelt upstream would leave the real one unguarded
            raise ValueError(f"rules name upstreams that are not set up: {sorted(unknown)}")


def _forget_oldest(tracked: dict) -> None:
    while len(tracked) > MAX_TRACKED:
        tracked.pop(next(iter(tracked)))


def show_decision_in_slack(slack: Slack, ledger: Ledger, approval_id: str) -> None:
    """Replace a decided approval's buttons in Slack with the outcome and who decided."""
    message = ledger.slack_message(approval_id)
    a = ledger.approval(approval_id)
    if not message or a["status"] == "pending":
        return
    text = approval_text(a["agent"], a["customer"], a["vendor"], a["model"], a["estimate"], a["reason"])
    verb = "Denied" if a["status"] == "denied" else "Approved"
    slack.show_decision(*message, text, f"{verb} by {a['decided_by']}.")


def approval_text(agent, customer, vendor, model, amount, reason) -> str:
    who = agent + (f" for {customer}" if customer else "")
    wants = f"${usd_display(amount)} on" if amount else "to call"  # 0: a rule asks, not a cap
    return f"Approval needed: {who} wants {wants} {vendor} {model}. {reason}."


def usage_from(obj: dict) -> dict[str, int]:
    """The tokens in a response or stream event, in OpenAI or Anthropic form: input (the whole
    prompt, cached tokens included), output, cache_read, and cache_write_total, the cache
    writes of which cache_write and cache_write_extended are those whose lifetime is known."""
    usage = (
        obj.get("usage")
        or (obj.get("message") or {}).get("usage")  # Anthropic message_start
        or (obj.get("response") or {}).get("usage")  # OpenAI Responses response.completed
        or {}
    )
    found = {}
    for name, keys in (("input", ("prompt_tokens", "input_tokens")),
                       ("output", ("completion_tokens", "output_tokens"))):
        value = next((usage[k] for k in keys if usage.get(k) is not None), None)
        if value is not None:
            found[name] = int(value)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(details, dict):  # OpenAI: cached tokens are already in the prompt count
        written = int(details.get("cache_write_tokens") or 0)
        lifetimes = details.get("cache_write_tokens_details") or {}
        short = int(lifetimes.get("ttl_30m_tokens") or 0)
        extended = int(lifetimes.get("ttl_12h_tokens") or 0)
        read = int(details.get("cached_tokens") or 0)
    else:  # Anthropic counts cached input apart from input_tokens
        written = int(usage.get("cache_creation_input_tokens") or 0)
        lifetimes = usage.get("cache_creation") or {}
        short = int(lifetimes.get("ephemeral_5m_input_tokens") or 0)
        extended = int(lifetimes.get("ephemeral_1h_input_tokens") or 0)
        read = int(usage.get("cache_read_input_tokens") or 0)
        if "input" in found:
            found["input"] += read + written
    if read or written:
        found.update(
            cache_read=read,
            cache_write=short,
            cache_write_extended=extended,
            cache_write_total=written,
        )
    return found


def output_limit(request: dict) -> int:
    """The most output tokens the call may produce, under whichever name the API uses."""
    return int(next((request[k] for k in OUTPUT_LIMITS if request.get(k)), DEFAULT_MAX_TOKENS))


def _same(a: str | None, b: str | None) -> bool:
    """Constant-time equality. As bytes: compare_digest refuses str outside ASCII."""
    return a is not None and b is not None and hmac.compare_digest(a.encode(), b.encode())


def estimate(price: Price, raw: bytes, request: dict) -> int:
    """The worst case: every request byte an input token, plus the most output allowed."""
    return price.cost() if price.is_flat else price.cost(len(raw), output_limit(request))


def charge(vendor: str, price: Price, held: int, usage: dict[str, int]) -> int:
    """The cost of a call that succeeded."""
    tokens = (usage.get("input"), usage.get("output"))
    if price.is_flat or tokens == (None, None):
        return held  # flat price, or no usage reported
    short, extended = usage.get("cache_write", 0), usage.get("cache_write_extended", 0)
    # A write of unknown lifetime pays the longer lifetime's price, except on
    # OpenAI, whose only such report is Chat Completions, which has no 12h writes.
    unsplit = max(0, usage.get("cache_write_total", 0) - short - extended)
    if vendor == "openai":
        short += unsplit
    else:
        extended += unsplit
    return price.cost(tokens[0] or 0, tokens[1] or 0, usage.get("cache_read", 0), short, extended)


class Handler(BaseHTTPRequestHandler):
    """serve() binds gw, the Gateway, to a subclass of this."""

    gw: Gateway
    protocol_version = "HTTP/1.1"
    _call: dict | None = None  # what record_call needs, set by _gateway for _forward_charged

    def parse_request(self):
        """Only content-length bodies are read. Any other body left on the connection
        would be read as the next request, one a proxy in front never saw."""
        if not super().parse_request():
            return False
        if self.command not in BODY_METHODS:
            self.server.deadline(self.connection, None)
        if "transfer-encoding" in self.headers:
            self._extra, self.close_connection = {}, True
            self._json(411, {"error": "send the body with a content-length"})
            return False
        if self.command not in BODY_METHODS and self.headers.get("content-length", "0").strip() != "0":
            self.close_connection = True
        return True

    def handle_one_request(self):
        self.server.deadline(self.connection, time.monotonic() + REQUEST_DEADLINE)
        try:
            super().handle_one_request()
        finally:
            self.server.deadline(self.connection, None)

    def do_GET(self):
        self._extra, self._who, self._vendor, self._redactor = {}, UNCHECKED, None, None
        route, gw = urlsplit(self.path).path, self.gw
        if route == "/healthz":
            return self._json(200, {"ok": True})
        if route == "/openapi.json":  # no secrets in it, so no key
            return self._relay(200, "application/json", OPENAPI.read_bytes())
        if gw.locked_out(self._client()):
            return self._json(429, {"error": "too many wrong keys, try again later"})
        if route == "/dashboard":
            return self._dashboard()
        if route in FINANCE_DOWNLOADS:
            return self._download(route)
        receipt = route.startswith("/approvals/") and route.endswith("/receipt") and route.count("/") == 3
        why = route.startswith("/tasks/") and route.endswith("/why") and route.count("/") == 3
        if not (receipt or why or route in VIEWER_ROUTES):
            return self._unrouted(b"")
        if self._refused("viewer"):
            return None
        if why:
            return self._json(200, forensics.why(gw.ledger, unquote(route.split("/")[2])))
        if receipt:
            return self._receipt(route.split("/")[2])
        if route == "/audit":
            return self._json(200, gw.ledger.audit_log())
        if route == "/approvals":
            return self._approvals()
        if route == "/approvals/suggestions":
            return self._json(200, learn.suggestions(gw.ledger))
        if route == "/standing":
            return self._json(200, gw.ledger.standing())
        if route == "/bills":
            return self._json(200, bills.listed(gw.ledger))
        return self._json(200, forecast(gw.ledger, gw.caps))

    def do_PUT(self):  # and PATCH and DELETE: only ever forwarded to an upstream
        self._extra, self._who, self._vendor, self._redactor = {}, UNCHECKED, None, None
        raw = self._body()
        if raw is not None:
            self._unrouted(raw)

    do_PATCH = do_DELETE = do_PUT

    def _body(self) -> bytes | None:
        """The request body, or None once refused for a bad or oversized length."""
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._json(413 if length > 0 else 400, {"error": "bad or oversized body"})
            return None
        if length > MAX_KEYLESS_BODY and not self._has_key():
            self.close_connection = True  # unread: 8 MB bodies on many sockets ran a tenant out of memory
            self._json(401, {"error": "a valid key is required to send a body this large"})
            return None
        raw = self.rfile.read(length)
        self.server.deadline(self.connection, None)
        return raw

    def _has_key(self) -> bool:
        """A valid agent or person key is on the request, or none is needed (no agent keys set,
        or a route checked by its signature instead)."""
        gw, route = self.gw, urlsplit(self.path).path.strip("/")
        if not gw.agent_keys or route in ("slack/actions", "issuing/stripe", "approvals/stripe") or self._agent_key():
            return True
        return bool(gw.person(self.headers.get("x-quarter-key")) or gw.person(self._basic_password()))

    def do_POST(self):
        self._extra, self._who, self._vendor, self._redactor = {}, UNCHECKED, None, None
        raw = self._body()
        if raw is None:
            return None
        parts = urlsplit(self.path).path.strip("/").split("/")
        if parts == ["approvals", "stripe"]:
            return self._stripe_approval(raw)
        if parts[0] == "approvals" and len(parts) == 3 and parts[2] in ("approve", "deny"):
            return self._decide(parts, raw)
        if parts == ["slack", "actions"]:
            if not self.gw.slack:
                return self._json(503, {"error": "Slack approvals are not set up: set QUARTER_SLACK_BOT_TOKEN,"
                                        " QUARTER_SLACK_SIGNING_SECRET and QUARTER_SLACK_CHANNEL"})
            return self._slack_action(raw)
        if parts[0] in ("agents", "tasks") and len(parts) == 3 and parts[2] == "resume":
            return self._resume(parts[0], unquote(parts[1]), raw)
        if parts[0] in ("authorize", "settle") and len(parts) == 1:
            return self._payment(parts[0], raw)
        if parts == ["issuing", "stripe"]:
            return self._card_event(raw)
        if parts == ["costs"]:
            return self._record(raw)
        if parts[0] == "standing":
            return self._standing(parts, raw)
        if parts == ["attribution"]:
            return self._attribution(raw)
        if parts == ["bills"]:
            return self._bill(raw)
        if parts[0] == "bills" and len(parts) == 3 and parts[2] == "pay":
            return self._pay_bill(parts[1], raw)
        self._unrouted(raw)

    def _unrouted(self, raw: bytes):
        """A path no Quarter route took. Under a Quarter route's first segment it is never a
        vendor's: 405 when the path is a route for other methods, else 404."""
        path = urlsplit(self.path).path
        if path.strip("/").split("/")[0] not in RESERVED:
            return self._gateway(raw)
        allowed = sorted({m.upper() for template, methods in ROUTES if template.fullmatch(path) for m in methods})
        if allowed:
            self._extra["allow"] = ", ".join(allowed)
            return self._json(405, {"error": f"{self.command} is not allowed here; use {' or '.join(allowed)}"})
        return self._json(404, {"error": f"no Quarter route {path[:200]}"})

    # Finance

    def _dashboard(self):
        if self._person() is None:
            body = json.dumps({"error": "a person's key or the admin key is required"}).encode()
            return self._relay(401, "application/json", body, {"www-authenticate": 'Basic realm="Quarter"'})
        name, role = self._person()
        month = self._month()
        if month is None:
            return
        can_decide = ROLES.index(role) >= ROLES.index("approver")
        finance = ROLES.index(role) >= ROLES.index("finance")
        centres, version = None, ""
        if finance and self.gw.centres_path:
            try:
                centres, version = self.gw.centres()
            except ValueError:  # a hand-edited file read_centres refuses: no form over it
                centres = None
        page = dashboard.render(self.gw.ledger, self.gw.caps, f"{name} ({role})", can_decide, self.gw.in_shadow(),
                                set(self.gw.key_agents.values()), month, finance, centres, version)
        self._relay(200, "text/html; charset=utf-8", page.encode(), HTML_HEADERS)

    def _month(self) -> str | None:
        """?month=YYYY-MM up to this one, else dashboard.default_month. None once answered 400."""
        given = parse_qs(urlsplit(self.path).query).get("month", [""])[0]
        now = datetime.now(UTC)
        if not given:
            return dashboard.default_month(now)
        if not MONTH.fullmatch(given) or given > f"{now:%Y-%m}":
            self._json(400, {"error": "month is YYYY-MM, and not later than this month"})
            return None
        return given

    def _download(self, route: str):
        """A month's journal, FOCUS rows, audit pack or approvals record, as the CLI writes them."""
        if self._cross_site():
            return self._json(403, {"error": "cross-site request refused"})
        if self._refused("finance"):
            return None
        month = self._month()
        if month is None:
            return None
        gw = self.gw
        if route == "/export":
            fmt = parse_qs(urlsplit(self.path).query).get("format", ["csv"])[0]
            if fmt == "focus":
                body, name = focus(focus_rows(gw.ledger, *month_range(month), {})), f"quarter-focus-{month}.csv"
            elif fmt in FORMATS:
                try:
                    centres = gw.centres()[0] if gw.centres_path else {}
                    centre_headers(fmt, list(centres))
                except ValueError as e:
                    return self._json(400, {"error": f"cost centres: {e}"})
                body = journal(gw.ledger, month, fmt=fmt, centres=centres or None)
                name = f"quarter-journal-{fmt}-{month}.csv"
            else:
                return self._json(400, {"error": f"format is one of {', '.join((*FORMATS, 'focus'))}"})
            data, ctype = body.encode(), "text/csv; charset=utf-8"
        else:
            buf = io.BytesIO()
            if route == "/audit-pack":
                people = {n: p["role"] for n, p in gw.people.items()} or None  # names and roles, never keys
                audit_pack(gw.ledger, month, buf, gw.caps, gw.signing_key, gw.retention_days, people,
                           gw.rules_config, gw.ledger.register)
                name = f"quarter-audit-pack-{month}.zip"
            else:
                approval_record.export(gw.ledger, buf, month)
                name = f"quarter-approvals-{month}.zip"
            data, ctype = buf.getvalue(), "application/zip"
        gw.ledger.audit("download", self._actor(), name)
        self._relay(200, ctype, data, {"content-disposition": f'attachment; filename="{name}"',
                                       "cache-control": "no-store"})

    def _attribution(self, raw: bytes):
        """The cost centre form: one row per agent, a centre per dimension, blank for none."""
        if not self._allowed("finance"):
            return None
        if not self.gw.centres_path:
            return self._json(409, {"error": "no cost centre file is set"})
        form = parse_qs(raw.decode(errors="replace"), keep_blank_values=True)
        new: dict[str, dict[str, str]] = {d: {} for d in dashboard.DIMENSIONS}
        seen: set[str] = set()
        for i, agent in enumerate(a.strip() for a in form.get("agent", [])):
            values = {d: (form.get(d, [])[i:i + 1] or [""])[0].strip() for d in dashboard.DIMENSIONS}
            if not agent:
                if any(values.values()):
                    return self._json(400, {"error": "a row has a centre but no agent"})
                continue
            if agent in seen:
                return self._json(400, {"error": f"{agent[:64]} is listed twice"})
            seen.add(agent)
            text = agent + "".join(values.values())
            if len(agent) > 64 or any(len(v) > 100 for v in values.values()) or not text.isprintable():
                return self._json(400, {"error": "agent names are at most 64 characters, centres 100"})
            for d, v in values.items():
                if v:
                    new[d][agent] = v
        changes = self.gw.save_centres(form.get("version", [""])[0], {d: m for d, m in new.items() if m},
                                       self._actor())
        if changes is None:
            return self._json(409, {"error": "the map changed since you opened it; reload and try again"})
        self._done(True, raw)

    def _approvals(self):
        """?status=pending (the default), approved (used ones too), denied or all."""
        wanted = parse_qs(urlsplit(self.path).query).get("status", ["pending"])[0]
        if wanted not in APPROVAL_STATUSES:
            return self._json(400, {"error": f"status is one of {', '.join(APPROVAL_STATUSES)}"})
        found = [a for status in APPROVAL_STATUSES[wanted] for a in self.gw.ledger.approvals(status)]
        return self._json(200, sorted(found, key=lambda a: a["created_at"]))

    def _receipt(self, approval_id: str):
        if not self.gw.signing_key:
            return self._json(503, {"error": "QUARTER_SIGNING_KEY is not set"})
        approval = self.gw.ledger.approval(approval_id)
        if not approval or approval["status"] not in ("approved", "used", "denied"):
            return self._json(404, {"error": "no decided approval with that id"})
        return self._json(200, {"receipt": receipts.sign(approval, self.gw.signing_key)})

    def _cross_site(self) -> bool:
        """A browser post from another site. Basic auth is sent automatically, so
        without this any page could approve a call from a signed-in finance tab."""
        origin = self.headers.get("origin") or self.headers.get("referer")
        if not origin:
            return self.headers.get("sec-fetch-site") not in (None, "same-origin", "none")
        return urlsplit(origin).netloc != self.headers.get("host")

    def _allowed(self, role: str) -> bool:
        """A person with this role or above, posting from this site. Otherwise answered."""
        if self._cross_site():
            self._json(403, {"error": "cross-site request refused"})
            return False
        return not self._refused(role)

    def _done(self, ok: bool, raw: bytes, why: str = ""):
        """The dashboard's buttons go back to the dashboard; an API call gets JSON, saying
        `why` when nothing changed."""
        if b"form=1" not in raw:
            return self._json(200, {"ok": True}) if ok else self._json(409, {"ok": False, "error": why})
        self.send_response(303)
        self.send_header("location", "/dashboard")
        self.send_header("content-length", "0")
        self.end_headers()

    def _resume(self, kind: str, name: str, raw: bytes):
        if not self._allowed("approver"):
            return
        ledger, by = self.gw.ledger, self._person()[0]
        ok = ledger.resume_task(name, by) if kind == "tasks" else ledger.resume(name, by)
        if ok:
            ledger.audit("resumed", self._actor(), f"task {name}" if kind == "tasks" else name)
        self._done(ok, raw, f"{kind[:-1]} {name[:64]} is not paused")

    def _decide(self, parts, raw):
        if not self._allowed("approver"):
            return
        by = self._person()[0]
        named = (self.headers.get("x-quarter-approver") or "").strip()[:64]
        if named and self._legacy_admin():
            by = named  # the shared admin key cannot say who is deciding; the header does
        elif named and named.lower() != by.lower():
            self.gw.ledger.audit("approver_mismatch", self._actor(), f"{parts[1]} {named}")
            return self._json(403, {"error": f"signed in as {by}, not {named}"})
        try:
            ok = self.gw.ledger.decide_approval(parts[1], parts[2] == "approve", by=by)
        except PermissionError as e:
            return self._json(403, {"error": str(e)})
        if ok:
            self.gw.ledger.audit(parts[2], self._actor(), parts[1])
            self.gw.decided(parts[1])
        approval = None if ok else self.gw.ledger.approval(parts[1])
        why = f"{parts[1][:64]} is already {approval['status']}" if approval else f"no approval {parts[1][:64]}"
        self._done(ok, raw, why)

    def _slack_action(self, raw: bytes):
        """An Approve or Deny click. Slack wants a 200 within 3 seconds, so its
        messages are sent in the background."""
        gw, slack = self.gw, self.gw.slack
        if not slack.verified(
            self.headers.get("x-slack-request-timestamp"),
            self.headers.get("x-slack-signature"),
            raw,
        ):
            return self._json(401, {"error": "bad slack signature"})
        try:
            payload = json.loads(parse_qs(raw.decode())["payload"][0])
            action = payload["actions"][0]
            user, channel = payload["user"]["id"], payload["channel"]["id"]
            ts, approval_id = payload["message"]["ts"], action["value"]
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return self._json(400, {"error": f"bad slack payload: {e}"})
        if action["action_id"] not in ("approve", "deny"):
            return self._json(400, {"error": "unknown slack action"})
        approve = action["action_id"] == "approve"
        gw.ledger.set_slack_message(approval_id, channel, ts)  # if posted before ts was kept
        person = slack.users.get(user)
        note = None
        if person is None:
            gw.ledger.audit("slack_refused", f"slack:{user}", approval_id)
            note = "You are not in QUARTER_SLACK_USERS, so you cannot decide approvals."
        else:
            try:
                ok = gw.ledger.decide_approval(approval_id, approve, by=person)
            except PermissionError as e:
                ok, note = False, f"{e}."
            if ok:
                gw.ledger.audit(action["action_id"], f"slack:{user}", approval_id)
                gw.decided(approval_id)
            elif note is None:
                note = "This approval was already decided."
        if note:
            gw._in_background(lambda: slack.tell(channel, user, note), approval_id)
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def _record(self, raw: bytes):
        if not self._allowed("finance"):
            return None
        ledger = self.gw.ledger
        try:
            body = json.loads(raw)
            amount = to_micros(body["amount_usd"])
            if amount <= 0:
                return self._json(400, {"error": "amount_usd must be positive"})
            if not MONTH.fullmatch(str(body["month"])):
                return self._json(400, {"error": "month is YYYY-MM"})
            cost_id = ledger.add_cost(
                category=body["category"],
                vendor=body["vendor"],
                amount=amount,
                month=body["month"],
                monthly=bool(body.get("monthly")),
                agent=body.get("agent"),
                customer=body.get("customer"),
                note=body.get("note", ""),
            )
            ledger.audit("cost", self._actor(), f"{cost_id} {amount}")
            return self._json(200, {"id": cost_id})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError) as e:
            return self._json(400, {"error": f"bad request: {e}"})

    def _standing(self, parts, raw: bytes):
        """Add a standing approval (POST /standing) or revoke one (POST /standing/{id}/revoke)."""
        if not self._allowed("approver"):
            return None
        by = self._person()[0]
        if len(parts) == 3 and parts[2] == "revoke":
            if self.gw.ledger.revoke_standing(parts[1], by):
                return self._json(200, {"id": parts[1]})
            return self._json(404, {"error": "no active standing approval with that id", "id": parts[1]})
        if len(parts) != 1:
            return self._json(404, {"error": "use /standing or /standing/{id}/revoke"})
        try:
            body = json.loads(raw)
            rule_id = self.gw.ledger.add_standing(
                str(body["agent"]), str(body["vendor"]), to_micros(body["up_to_usd"]),
                to_micros(body["monthly_usd"]), by, int(body.get("days", 90)),
            )
        except PermissionError as e:
            return self._json(403, {"error": str(e)})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError) as e:
            return self._json(400, {"error": f"bad request: {e}"})
        return self._json(200, {"id": rule_id})

    def _bill(self, raw: bytes):
        """An agent (or a person's tool) enters a bill to pay from the company's account."""
        gw = self.gw
        if not self._agent_ok():
            return self._json(401, {"error": "agent key required"})
        ids = self._ids()
        if ids is None:
            return None
        try:
            body = json.loads(raw)
            b = bills.add(gw.ledger, gw.caps, gw.payees, ids, str(body["payee"]), str(body["invoice"]),
                          to_micros(body["amount_usd"]), str(body["account"]), body.get("due"),
                          str(body.get("memo", "")), gw.sanctions)
        except bills.Duplicate as e:
            return self._json(409, {"error": "duplicate_invoice", "reason": str(e)})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError) as e:
            return self._json(400, {"error": f"bad request: {e}"})
        if b["status"] == "waiting":
            gw.announce_approval(b["approval_id"], ids, bills.vendor(b["payee"]), bills.RAIL, b["amount"], b["reason"])
        return self._json(200, b)

    def _pay_bill(self, bill_id: str, raw: bytes):
        if not self._allowed("finance"):
            return None
        if self._legacy_admin():  # it names no one, so it could also have approved the bill
            self.gw.ledger.audit("role_refused", self._actor(), f"shared admin key paying {bill_id}")
            return self._json(403, {"error": "pay with a person's own key, so the payment names who released it"})
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            return self._json(400, {"error": f"bad request: {e}"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "bad request: send a JSON object"})
        method, payment_method = body.get("method", "mercury"), body.get("payment_method", "ach")
        if method not in ("mercury", "manual"):
            return self._json(400, {"error": "method is mercury or manual"})
        if method == "manual" and not body.get("ref"):
            return self._json(400, {"error": "a manual payment needs the bank's reference"})
        if method == "mercury" and payment_method not in bills.MERCURY_METHODS:
            return self._json(400, {"error": f"payment_method is one of {', '.join(bills.MERCURY_METHODS)}"})
        try:
            b = bills.pay(self.gw.ledger, self.gw.caps, bill_id, self._person()[0], method,
                          body.get("ref"), self.gw.mercury, payment_method)
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'")})
        except PermissionError as e:
            return self._json(403, {"error": str(e)})
        except ValueError as e:
            return self._json(409, {"error": str(e)})
        return self._json(200, b)

    def _refused(self, need: str) -> bool:
        """Answers 401 or 403 and returns True, unless the person signed in has role `need`
        or one above it."""
        locked = self._who is UNCHECKED and self.gw.locked_out(self._client())
        if self._person() is None:
            if locked:
                self._json(429, {"error": "too many wrong keys, try again later"})
                return True
            self._json(401, {"error": "a person's key or the admin key is required"})
            return True
        name, role = self._person()
        if ROLES.index(role) >= ROLES.index(need):
            return False
        path = urlsplit(self.path).path
        self.gw.ledger.audit("role_refused", self._actor(), f"{role} {self.command} {path}")
        self._json(403, {"error": f"{name} is {role}; this needs {need} or above"})
        return True

    def _person(self) -> tuple[str, str] | None:
        """(name, role) of who signed in. Checked once per request, as failures count."""
        if self._who is UNCHECKED:
            self._who = self._sign_in()
        return self._who

    def _client(self) -> str:
        """The caller's address: the last one the proxy added, else the connection's."""
        sent = self.gw.client_ip_header and self.headers.get(self.gw.client_ip_header)
        return sent.split(",")[-1].strip()[:64] if sent else self.client_address[0]

    def _actor(self) -> str:
        """Who did it and from where, for the audit log."""
        return f"{self._person()[0]}@{self._client()}"

    def _legacy_admin(self) -> bool:
        return bool(self.gw.admin_key) and self._person() == (LEGACY_ADMIN, "admin")

    def _sign_in(self) -> tuple[str, str] | None:
        gw, client = self.gw, self._client()
        if gw.locked_out(client):
            return None
        sent = self.headers.get("x-quarter-key")
        who = gw.person(sent) or gw.person(self._basic_password())
        presented = self.headers.get("authorization") or (sent not in gw.agent_keys and sent)
        if who is None and presented:
            gw.failed_login(client)
            gw.ledger.audit("login_failed", client, urlsplit(self.path).path)
        return who

    def _basic_password(self) -> str | None:
        """The password of HTTP basic auth; any username works."""
        auth = self.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return None
        try:
            return base64.b64decode(auth[6:]).decode().partition(":")[2]
        except ValueError:
            return None

    # Agents: who is calling

    def _vendor_auth(self) -> dict[str, str | None]:
        bearer = self.headers.get("authorization", "")
        sent = {
            "authorization": bearer[7:].strip() if bearer[:7].lower() == "bearer " else None,
            "x-api-key": self.headers.get("x-api-key"),
        }
        for header in set(self.gw.upstream_auth.values()):
            sent[header] = self.headers.get(header)
        return sent

    def _subscription(self) -> bool:
        """A claude.ai login (Claude Code on Pro or Max) rather than an API key: those calls
        are covered by the subscription, not billed per token."""
        token = self._vendor_auth()["authorization"] or ""
        return token.startswith("sk-ant-oat")

    def _agent_key(self) -> str | None:
        sent = [self.headers.get("x-quarter-key"), *self._vendor_auth().values()]
        return next((k for v in sent for k in self.gw.agent_keys if _same(v, k)), None)

    def _agent_ok(self) -> bool:
        return not self.gw.agent_keys or self._agent_key() is not None

    def _ids(self) -> dict | None:
        """The call's agent, task and customer. None, answered with 403, when a named
        key speaks for another agent: that would dodge its pause and caps."""
        ids = {s: self.headers.get(f"x-quarter-{s}") for s in ("agent", "task", "customer")}
        named = self.gw.key_agents.get(self._agent_key() or "")
        ids["agent"] = ids["agent"] or named or "default"
        ids["task"] = ids["task"] or self.headers.get("x-claude-code-session-id")
        if named is not None and ids["agent"] != named:
            error = f"this agent key belongs to agent {named}"
            self._json(403, {"error": error, "agent": ids["agent"]})
            return None
        return ids

    def _swap_vendor_key(self, vendor: str) -> bool:
        """An agent key sent as the vendor key is replaced by the vendor's real key: a named
        key's own (vendor:agent) when set, else the vendor's. A routed call takes only the key
        set for the upstream it goes to, so no key reaches a host it was not made for. False
        when that is needed and the gateway has none."""
        sent = self._vendor_auth().values()
        if not any(_same(v, k) for v in sent for k in self.gw.agent_keys):
            return True
        named, keys, upstream = self.gw.key_agents.get(self._agent_key() or ""), self.gw.vendor_keys, self._vendor
        real = (named and keys.get(f"{upstream}:{named}")) or keys.get(upstream)
        if not real:
            return False
        for header in self._vendor_auth():
            del self.headers[header]
        if upstream in self.gw.upstream_auth:
            self.headers[self.gw.upstream_auth[upstream]] = real
        elif vendor == "anthropic":
            self.headers["x-api-key"] = real
        else:
            self.headers["authorization"] = f"Bearer {real}"
        return True

    # Answers to a call that never reaches the vendor

    def _refuse(self, reason: str):
        """The hold refused it: the agent or task is paused, or the call is over a cap."""
        error = "cap_reached"
        for paused in ("agent paused", "task paused"):
            if reason.startswith(paused):
                error = paused.replace(" ", "_")
        self._json(402 if error == "cap_reached" else 423, {"error": error, "reason": reason})

    def _approval_required(self, reason: str, approval_id: str):
        error = {"error": "approval_required", "reason": reason, "approval_id": approval_id}
        self._json(403, error)

    # Payments on any rail: authorise before, settle after. No money passes here.

    def _payment(self, step: str, raw: bytes):
        gw = self.gw
        if not self._agent_ok():
            return self._json(401, {"error": "agent key required"})
        if step == "authorize" and not gw.signing_key:  # settling signs nothing
            return self._json(503, {"error": "QUARTER_SIGNING_KEY is not set"})
        ids = self._ids()
        if ids is None:
            return None
        try:
            body = json.loads(raw)
            if "amount_micros" in body:
                amount = int(body["amount_micros"])
            else:
                amount = to_micros(body["amount_usd"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError) as e:
            return self._json(400, {"error": f"bad request: {e}"})
        if abs(amount) > MAX_AMOUNT:
            return self._json(400, {"error": "bad request: an amount over $1 billion"})
        if step == "settle":
            return self._settle_payment(ids, body, amount)

        rail, payee = str(body.get("rail", "")), str(body.get("payee", ""))
        if rail not in RAILS or not payee or amount <= 0:
            return self._json(
                400, {"error": f"need rail ({', '.join(sorted(RAILS))}), payee and an amount"}
            )
        rule, shadow = gw.payee_rule(rail, payee), gw.shadowing
        if rule == "deny" and not (shadow.payees or shadow.everything):
            return self._json(403, {"error": "payee_not_allowed", "payee": payee})
        refuse, screened = gw.screen(rail, payee, self.headers.get("x-quarter-approval"))
        if refuse:
            return self._json(403, {"error": "sanctions_match", "payee": payee, "reason": refuse})
        wait = self._wait()
        if wait is None:
            return None
        unlisted = f"{payee} is not on the {rail} payee list" if rule == "ask" else None
        network, resource = body.get("network"), str(body.get("resource") or "")
        flagged = payee_flags(gw.rules.feed_payees, rail, payee, network and str(network), resource, amount)
        ask = "; ".join(r for r in (unlisted, flagged) if r) or None
        deny = f"{payee} is not allowed on the {rail} payee list" if rule == "deny" else None
        used = self.headers.get("x-quarter-approval")
        args = (gw.caps, ids, f"{rail}:{payee}", rail, amount)
        decision, ref = gw.ledger.hold(*args, used, ask=ask, deny=deny, shadow=shadow, sanctions=screened)
        self._free_limit()
        if decision.outcome == "approve":
            gw.announce_approval(ref, ids, rail, payee, amount, decision.reason)
            approval = gw.wait_for_decision(ref, min(wait, gw.max_wait)) if wait > 0 else None
            if approval and approval["status"] == "denied":
                by = approval["decided_by"]
                return self._json(403, {"error": "approval_denied", "approval_id": ref, "decided_by": by})
            if not approval or approval["status"] != "approved":
                return self._approval_required(decision.reason, ref)
            # Approved while waiting: go on as if the agent retried with the id.
            used = ref
            if screened:  # as the retry would, so a person's clearing is remembered
                gw.screen(rail, payee, used)
            decision, ref = gw.ledger.hold(*args, used, ask=ask, deny=deny, shadow=shadow, sanctions=screened)
        if decision.outcome == "block":
            return self._refuse(decision.reason)
        if decision.outcome == "approve":  # the approval did not cover this payment
            gw.announce_approval(ref, ids, rail, payee, amount, decision.reason)
            return self._approval_required(decision.reason, ref)
        token = authorisation(ref, ids, rail, payee, amount, gw.signing_key)
        out = {"call_id": ref, "authorisation": token, "authorization": token, "expires_in": AUTH_TTL}
        self._extra["x-quarter-call"] = ref
        if decision.would_hold:  # authorised, but only because the rule is in shadow
            would = decision.would_hold
            out["shadow"] = {"would": would.outcome, "reason": would.reason}
            self._extra["x-quarter-shadow"] = f"would_{would.outcome}"
        if gw.org and gw.signing_key.startswith("ed25519:"):
            approval = used if used and gw.ledger.db.execute(
                "select 1 from approvals where id = ? and call_id = ?", (used, ref)).fetchone() else None
            out["mandate"] = mandate.mandate(
                gw.org, ref, rail, payee, amount, gw.ledger.policy_ref(),
                gw.ledger.entry_hash(ref), approval, gw.signing_key,
            )
        return self._json(200, out)

    def _settle_payment(self, ids: dict, body: dict, amount: int):
        gw = self.gw
        if amount < 0:
            return self._json(400, {"error": "the amount paid cannot be negative"})
        call_id = str(body.get("call_id", ""))
        row = gw.ledger.db.execute(
            "select agent, model, amount from entries where call_id = ? and kind = 'hold'",
            (call_id,),
        ).fetchone()
        bill = gw.ledger.db.execute("select 1 from bills where call_id = ?", (call_id,)).fetchone()
        if row is None or row[0] != ids["agent"] or row[1] not in RAILS or bill:  # bills settle when paid
            error = "no payment authorised for this agent with that call_id"
            return self._json(404, {"error": error})
        tx, network = str(body.get("tx", "")), ""
        if body.get("x402_response"):  # the PAYMENT-RESPONSE header the agent got back
            try:
                result = x402_settlement(str(body["x402_response"]))
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": f"bad x402_response: {e}"})
            tx, network = result["tx"], result["network"] or ""
            if not result["success"]:
                amount = 0  # the payment did not go through: nothing owed
        try:
            gw.ledger.settle(call_id, amount or None)
        except KeyError:
            return self._json(409, {"error": "already settled"})
        detail = f"{call_id} {row[1]} {amount} tx={tx}" + (f" network={network}" if network else "")
        gw.ledger.audit("payment_settled", self._client(), detail)
        if amount > row[2]:
            gw.ledger.audit("over_authorised", self._client(), detail)
        gw.after_spend(ids["agent"])
        return self._json(200, {"ok": True, "over_authorised": amount > row[2]})

    # Quarter cards: Stripe Issuing asks before each swipe and reports when it closes.

    def _stripe_approval(self, raw: bytes):
        """Record a Stripe approval request event. Read only: Stripe's own reviewers decide."""
        secret = self.gw.stripe_approvals_secret
        if not secret:
            return self._json(503, {"error": "QUARTER_STRIPE_APPROVALS_SECRET is not set"})
        if not issuing.verified(raw, self.headers.get("stripe-signature", ""), secret):
            return self._json(401, {"error": "bad Stripe signature"})
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return self._json(400, {"error": "not a Stripe event"})
        row = approval_record.from_stripe(event) if isinstance(event, dict) else None
        if row is None:  # another event type sent to this endpoint: nothing to record
            return self._json(200, {"received": True, "recorded": False})
        return self._json(200, {"received": True, "recorded": approval_record.record(self.gw.ledger, row)})

    def _card_event(self, raw: bytes):
        gw = self.gw
        if not gw.stripe_secret:
            return self._json(503, {"error": "QUARTER_STRIPE_SECRET is not set"})
        if not issuing.verified(raw, self.headers.get("stripe-signature", ""), gw.stripe_secret):
            return self._json(401, {"error": "bad Stripe signature"})
        try:
            event = json.loads(raw)
            auth = event["data"]["object"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._json(400, {"error": "not a Stripe event"})
        if event.get("type") == "issuing_authorization.request":
            return self._card_swipe(auth, str(event.get("api_version") or ""))
        call_id = (auth.get("metadata") or {}).get("quarter_call", "")
        closed = event.get("type") == "issuing_authorization.updated" and auth.get("status") in issuing.CLOSED
        hold = gw.ledger.db.execute(
            "select agent from entries where call_id = ? and kind = 'hold'", (call_id,)
        ).fetchone()
        if closed and hold:
            taken = issuing.captured_micros(auth)
            try:
                gw.ledger.settle(call_id, taken or None)
            except KeyError:
                pass  # Stripe sends an event more than once; the first one settled it
            else:
                gw.ledger.audit("card_settled", self._client(), f"{call_id} {auth.get('id')} {taken}")
                gw.after_spend(hold[0])
        return self._json(200, {"received": True})

    def _card_swipe(self, auth: dict, api_version: str):
        """Approve or decline within Stripe's 2 seconds, with the hold's id in the metadata."""
        gw, ask = self.gw, issuing.swipe(auth)
        ids, payee, amount = ask["ids"], ask["payee"], ask["micros"]

        def answer(approved: bool, **meta: str):
            body = {"approved": approved, "metadata": {f"quarter_{k}": v for k, v in meta.items()}}
            detail = f"{auth.get('id')} {ids['agent']} {payee} {amount} " + ("approved" if approved else "declined")
            gw.ledger.audit("card_swipe", self._client(), detail)
            self._relay(200, "application/json", json.dumps(body).encode(), {"stripe-version": api_version})

        if ask["currency"] != "usd" or amount <= 0:
            return answer(False, reason="only US dollar amounts are checked")
        rule, shadow = gw.payee_rule("card", payee), gw.shadowing
        if rule == "deny" and not (shadow.payees or shadow.everything):
            return answer(False, reason="payee_not_allowed")
        vendor = f"card:{payee}"
        approval = gw.ledger.approved_for(ids["agent"], vendor, amount)
        refuse, screened = gw.screen("card", payee, approval)
        if refuse:
            return answer(False, reason=refuse[:200])
        decision, ref = gw.ledger.hold(
            gw.caps, ids, vendor, "card", amount, approval,
            ask=f"{payee} is not on the card payee list" if rule == "ask" else None,
            deny=f"{payee} is not allowed on the card payee list" if rule == "deny" else None,
            shadow=shadow, sanctions=screened,
        )
        if decision.outcome == "allow":
            would = {"shadow": f"would_{decision.would_hold.outcome}"} if decision.would_hold else {}
            return answer(True, call=ref, **would)
        if decision.outcome == "approve":
            gw.announce_approval(ref, ids, "card", payee, amount, decision.reason)
            return answer(False, approval=ref, reason="waits for a person; retry once approved")
        return answer(False, reason=decision.reason[:200])

    # Gateway

    def _gateway(self, raw: bytes):
        gw, url = self.gw, urlsplit(self.path)
        vendor, _, rest = url.path.lstrip("/").partition("/")
        self._vendor = vendor  # so a refusal, even of an unknown vendor, is in a vendor's shape
        if not self._agent_ok():
            return self._json(401, {"error": "a Quarter agent key is required: send it as the API key, or in x-quarter-key"})
        plain = unquote(rest)  # a vendor decodes %2e%2e before resolving it
        if any(seg in ("..", ".") for seg in plain.split("/")) or "\\" in plain:
            return self._json(400, {"error": "bad path"})
        if vendor not in gw.upstreams:
            return self._json(404, {"error": f"unknown vendor {vendor}"})
        wait = self._wait()
        if wait is None:
            return None
        ids = self._ids()
        if ids is None:
            return None
        route = gw.rules.route(ids["agent"], ids["customer"], vendor)
        if route:  # e.g. this customer's data stays in the EU: the vendor's EU endpoint
            self._vendor = route.to
            self._extra["x-quarter-upstream"] = route.to
        if not self._swap_vendor_key(vendor):
            env = VENDOR_KEY_ENV.get(self._vendor, "QUARTER_VENDOR_KEYS")
            self._extra["x-should-retry"] = "false"  # the SDKs' own retries obey this
            return self._json(503, {"error": f"the gateway has no {self._vendor} key: set {env}"})
        if rest.startswith("v1/v1/"):  # a client base URL that already ends in /v1
            rest = rest[3:]
        target = gw.upstreams[self._vendor].rstrip("/") + "/" + rest
        target += f"?{url.query}" if url.query else ""
        if self._egress(vendor, "/" + unquote(rest), raw, target, ids):
            return None
        self._redactor = gw.rules.redactor(ids["agent"], ids["customer"], vendor, gw.redact_key)
        if self._redactor:  # e.g. card numbers may not reach a model: masked now, put back in the reply
            raw = self._redactor.body(raw)
            if self._redactor.found:
                self._extra["x-quarter-redacted"] = self._redactor.summary()
        if rest.rstrip("/") in (FREE_POSTS if self.command == "POST" else FREE_GETS):
            return self._forward(target, raw or None, lambda ok, usage: None)
        try:
            request = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return self._json(400, {"error": "body is not JSON"})
        request = request if isinstance(request, dict) else {}
        gw.check_repeats(ids["agent"], raw)
        gw.check_tool_repeats(ids, request)

        model = asked = str(request.get("model", ""))
        priced_as = PRICED_AS.get(vendor, vendor)
        try:
            price = gw.prices.price(priced_as, model)
        except UnknownPrice as e:
            price = gw.unpriced(priced_as, model)
            if price is None:
                return self._json(400, {"error": str(e)})
        listed = price  # what the call is worth at list price, whoever pays
        subscription = vendor in PRICED_AS or (vendor == "anthropic" and self._subscription())
        if subscription:
            price = Price()  # recorded at no cost: the subscription pays, not per token
            self._extra["x-quarter-billing"] = "subscription"
        if request.get("stream") and rest.endswith("chat/completions"):
            request.setdefault("stream_options", {})["include_usage"] = True
            raw = json.dumps(request).encode()
        cache_added = self._cache(vendor, rest, request)
        if cache_added:
            request = {**request, "cache_control": {"type": "ephemeral"}}
            raw = json.dumps(request).encode()
            self._extra["x-quarter-cache"] = "added"
        held = estimate(price, raw, request)
        fallback = self._fallback(vendor, model, request)
        decision, ref = self._hold(ids, vendor, model, held, fallback, min(max(wait, 0), gw.max_wait))
        if decision is None:
            return None
        if decision.fallback_to and fallback:
            self._extra["x-quarter-model"] = f"{model} -> {decision.fallback_to}"
            model, held = fallback
            held = 0 if subscription else held
        if model != asked:
            listed = gw.prices.price(PRICED_AS.get(vendor, vendor), model)
            price = Price() if subscription else listed
            raw = json.dumps({**request, "model": model}).encode()
        if decision.outcome == "approve":  # the approval did not cover this call
            gw.announce_approval(ref, ids, vendor, model, held, decision.reason)
            return self._approval_required(decision.reason, ref)
        self._call = {
            "ids": ids, "vendor": vendor, "model": model, "listed": listed, "request": request,
            "path": rest, "at": datetime.now(UTC),
            "route": "subscription" if subscription else "api", "upstream": self._vendor,
            "redacted": self._redactor.summary() if self._redactor else None,
            "cache_added": cache_added,
            "hints": {h: self.headers.get(f"x-claude-code-{h}", "")[:128] or None for h in HINTS},
        }
        self._forward_charged(target, raw, ref, vendor, price, held, ids["agent"])

    def _cache(self, vendor: str, path: str, request: dict) -> bool:
        """Whether to ask Anthropic to cache this call's prompt. A cached prefix is billed at a
        tenth of the input price on the next call, and only requests that ask get it. A
        conversation (two or more messages) with no marker of its own gets the top-level one,
        which caches up to its last block for 5 minutes. The write costs 1.25 times the input
        price, so it pays from the next turn; one-off calls are left alone. Anthropic's own
        API and its regional endpoints only: the old Bedrock integration refuses the field."""
        return (
            self.gw.auto_cache
            and vendor == "anthropic" and str(self._vendor).startswith("anthropic")
            and path.rstrip("/").endswith("messages")
            and len(request.get("messages") or []) >= 2
            and not any(forensics.marks(request))
            and self.headers.get("x-quarter-cache", "").lower() != "off"
        )

    def _fallback(self, vendor: str, model: str, request: dict) -> tuple[str, int] | None:
        """The cheaper model to move this call to near a cap, with its estimate."""
        cheaper = self.gw.fallbacks.get(vendor, {}).get(model)
        if not cheaper or self.headers.get("x-quarter-fallback", "").lower() == "off":
            return None
        try:
            price = self.gw.prices.price(PRICED_AS.get(vendor, vendor), cheaper)
        except UnknownPrice:
            return None
        swapped = {**request, "model": cheaper}
        return cheaper, estimate(price, json.dumps(swapped).encode(), swapped)

    def _wait(self) -> float | None:
        """The x-quarter-wait header in seconds, or None once a bad one is answered."""
        try:
            wait = float(self.headers.get("x-quarter-wait") or 0)
        except ValueError:
            wait = math.nan
        if not math.isfinite(wait):  # nan would never time out, and spin on the database
            self._json(400, {"error": "x-quarter-wait must be a number of seconds"})
            return None
        return wait

    def _hold(self, ids, vendor, model, held, fallback, wait):
        """Hold the estimate, waiting up to `wait` seconds for a person when the call needs
        an approval. (decision, call id or approval id), or (None, None) once answered."""
        gw = self.gw
        approval_id = self.headers.get("x-quarter-approval")
        try:
            decision, ref = gw.ledger.hold(gw.caps, ids, vendor, model, held, approval_id, fallback, shadow=gw.shadowing)
        except sqlite3.Error as e:
            return self._ledger_down(e, ids, vendor, model, held, approval_id)
        self._free_limit()
        if decision.outcome == "approve":
            gw.announce_approval(ref, ids, vendor, model, held, decision.reason)
            approval = gw.wait_for_decision(ref, wait) if wait else None
            if approval and approval["status"] == "denied":
                by = approval["decided_by"]
                self._json(403, {"error": "approval_denied", "approval_id": ref, "decided_by": by})
                return None, None
            if not approval or approval["status"] != "approved":
                self._approval_required(decision.reason, ref)
                return None, None
            # Approved while waiting: go on as if the agent retried with the id.
            decision, ref = gw.ledger.hold(gw.caps, ids, vendor, model, held, ref, shadow=gw.shadowing)
        if decision.outcome == "block":
            self._refuse(decision.reason)
            return None, None
        self._extra["x-quarter-call"] = ref
        if decision.would_hold:
            self._extra["x-quarter-shadow"] = f"would_{decision.would_hold.outcome}"
        return decision, ref

    def _free_limit(self) -> None:
        """Past the month's free decisions the call is decided as ever, and its reply says so."""
        if self.gw.ledger.over_free_limit():
            self._extra["x-quarter-free-limit"] = "exceeded"

    def _ledger_down(self, error, ids, vendor, model, held, approval_id):
        """The hold could not be written. Fail-open lets a plain model call through, written
        to the fallback file; anything else is refused."""
        gw = self.gw
        if gw.may_fail_open(ids, held, approval_id):
            try:
                ref = gw.fail_open.passed(ids, vendor, model, held, f"{type(error).__name__}: {error}")
            except OSError:
                ref = None
            if ref:
                self._extra["x-quarter-call"] = ref
                self._extra["x-quarter-fail-open"] = "model"
                return Decision("allow"), ref
        self._extra["retry-after"] = "1"
        self._json(503, {"error": "ledger_unavailable"})
        return None, None

    def _forward_charged(self, target, raw, ref, vendor, price, held, agent) -> None:
        """Forward a held call and settle the hold to what it really cost."""
        settled, call, self._call = False, self._call, None
        passed = self._extra.get("x-quarter-fail-open") is not None
        book = partial(self.gw.fail_open.settled, ref) if passed else partial(self.gw.ledger.settle, ref)

        def settle(ok: bool | None, usage: dict[str, int]) -> None:
            nonlocal settled
            if ok is None:  # sent and not answered: charged in full, as an interrupted call
                book(held)
                settled = True
                return
            cost = charge(vendor, price, held, usage) if ok else None
            book(0 if passed and cost is None else cost)  # the fallback file takes 0, not None
            settled = True  # only now: if recording failed, the hold is still open
            if ok and not passed:
                if call:
                    self.gw.record_call(ref, call, usage, cost, held)
                self.gw.after_spend(agent)

        try:
            self._forward(target, raw if self.command in BODY_METHODS else None, settle)
        except BaseException:
            if not settled:
                book(held)  # outcome unknown, e.g. the agent hung up
            raise

    # Egress: rules on what agents do, and calls to APIs that are not models

    def _egress(self, vendor: str, path: str, raw: bytes, target: str, ids: dict) -> bool:
        """Apply the rules, and forward a call that is an action rather than a model call,
        unpriced and audited. True once the call is answered."""
        gw = self.gw
        action = self._is_action(vendor, raw)
        if action:  # a model call meets the pause in its hold; an action meets it here
            paused = gw.ledger.pause_reason(ids)
            if paused is not None and gw.mode != "shadow":
                self._refuse(paused)
                return True
            if paused is not None:
                detail = {"agent": ids["agent"], "task": ids.get("task"), "vendor": vendor, "amount": 0,
                          "outcome": "block", "reason": paused, "asked": []}
                gw.ledger.audit("would_hold", None, json.dumps(detail, sort_keys=True))
        operations = None
        if gw.rules.reads_graphql(ids["agent"], vendor, self.command, path):
            query, ctype = urlsplit(self.path).query, self.headers.get("content-type")
            operations = graphql_operations(raw, query, ctype)
        decision, rule = gw.rules.decide(ids["agent"], vendor, self.command, path, operations)
        if rule is None and gw.rules.feed_calls:  # no rule of the customer's matched
            rule, operations = self._feed_hold(raw, target, operations)
            decision = "approve" if rule else decision
        by = None
        if decision == "deny":
            self._audit_call(ids, vendor, path, "denied", rule=rule)
            self._json(403, {"error": "rule_denied", "rule": rule})
            return True
        if decision == "approve":
            what = f"{self.command} {path} {','.join(operations or [])}".strip()[:500]
            approval = self._ask(ids, vendor, what, rule)
            by = approval["decided_by"]
            if approval["status"] == "denied":
                self._audit_call(ids, vendor, path, "denied", by, rule)
                error = {"error": "approval_denied", "approval_id": approval["id"], "decided_by": by}
                self._json(403, error)
                return True
            if approval["status"] != "used":
                self._audit_call(ids, vendor, path, "approval_required", rule=rule)
                self._approval_required(rule, approval["id"])
                return True
        if action or by:
            self._audit_call(ids, vendor, path, "approved" if by else "allowed", by, rule)
        if not action:
            return False
        self._forward(target, raw if self.command in BODY_METHODS else None, lambda ok, usage: None)
        return True

    def _feed_hold(self, raw: bytes, target: str, operations: list[str] | None):
        """Why a feed rule asks about this call, judged by where it really goes, and the
        GraphQL fields it runs (read here if no rule of the customer's needed them)."""
        rules, where = self.gw.rules, urlsplit(target)
        host, path = where.hostname or "", unquote(where.path)
        if operations is None and rules.feed_reads_graphql(self.command, host, path):
            operations = graphql_operations(raw, where.query, self.headers.get("content-type"))
        why = rules.feed_hold(self.command, host, path, where.query, raw.decode(errors="replace"), operations)
        return why, operations

    def _is_action(self, vendor: str, raw: bytes) -> bool:
        """A call to an API agents act on, not a model: it has no price. A body naming a
        model is a model call, priced as before."""
        if self.command in ("GET", "POST") and self.gw.prices.knows(PRICED_AS.get(vendor, vendor)):
            return False
        try:
            request = json.loads(raw) if raw else {}
        except ValueError:
            return True
        return not (isinstance(request, dict) and "model" in request)

    def _ask(self, ids: dict, upstream: str, action: str, reason: str) -> dict:
        """The approval a rule needs: the one in x-quarter-approval if it covers this action,
        else a new one, waited on for x-quarter-wait seconds. Marked used once approved."""
        gw, agent = self.gw, ids["agent"]
        given = self.headers.get("x-quarter-approval")
        if given and gw.ledger.use_action_approval(given, agent, upstream, action):
            return gw.ledger.approval(given)
        ref = gw.ledger.request_action_approval(ids, upstream, action, reason)
        gw.announce_approval(ref, ids, upstream, action, 0, reason)
        wait = min(max(float(self.headers.get("x-quarter-wait") or 0), 0), gw.max_wait)
        approval = gw.wait_for_decision(ref, wait) if wait else gw.ledger.approval(ref)
        if approval["status"] == "approved" and gw.ledger.use_action_approval(ref, agent, upstream, action):
            approval["status"] = "used"
        return approval

    def _audit_call(self, ids: dict, upstream: str, path: str, decision: str,
                    by: str | None = None, rule: str | None = None) -> None:
        """An action on an upstream, in the audit log. Never a body, a query or a key."""
        detail = {"agent": ids["agent"], "task": ids.get("task"), "upstream": upstream,
                  "method": self.command, "path": path[:500], "decision": decision}
        detail |= {"decided_by": by} if by else {}
        detail |= {"rule": rule} if rule else {}
        detail |= {"routed_to": self._vendor} if self._vendor not in (None, upstream) else {}
        self.gw.ledger.audit("upstream_call", self._client(), json.dumps(detail, sort_keys=True))

    def _hold_tool_calls(self, reply: dict, body: bytes) -> bytes:
        """A reply with a tool call a rule holds reaches the agent once a person approves it.
        Denied, or not decided in time, the agent gets the reply without those calls."""
        held = self.gw.rules.held(self._ids()["agent"], reply)
        note = self._held_note(held) if held else None
        return body if note is None else json.dumps(without_tool_calls(reply, held, note)).encode()

    def _held_note(self, held: list) -> str | None:
        """Asks a person about held tool calls, unless a rule denies them. None when they
        may reach the agent; else the note that takes their place."""
        gw, ids = self.gw, self._ids()
        vendor = urlsplit(self.path).path.lstrip("/").partition("/")[0]
        names = ", ".join(dict.fromkeys(name for _, name, _, _ in held))
        mask = self._redactor.text if self._redactor else str  # what a model may not see, nor may Slack
        reason = "; ".join(f"{rule} on {name}: {hide_secrets(mask(text))[:120]}" for _, name, text, rule in held)
        detail = {"agent": ids["agent"], "task": ids.get("task"), "upstream": vendor, "tools": names}
        denying = next((rule for *_, rule in held if rule.action == "deny"), None)
        if denying:
            detail |= {"decision": "denied", "rule": str(denying)}
            outcome = f"A rule denies it ({denying})."
        else:
            approval = self._ask(ids, vendor, f"tool {names} {fingerprint(held)}", reason)
            by, ref = approval["decided_by"], approval["id"]
            detail |= {"decision": approval["status"], "approval_id": ref}
            detail |= {"decided_by": by} if by else {}
            if approval["status"] == "used":
                detail["decision"] = "approved"
                gw.ledger.audit("tool_held", self._client(), json.dumps(detail, sort_keys=True))
                return None
            outcome = (
                f"{by} denied it." if approval["status"] == "denied" else
                f"No one approved it in time. Once approval {ref} is approved, send the same"
                f" request again with the header x-quarter-approval: {ref}."
            )
        gw.ledger.audit("tool_held", self._client(), json.dumps(detail, sort_keys=True))
        return f"Quarter held the tool call {names} for a person, so it was not run. {outcome}"

    def _forward(self, url: str, data: bytes | None, settle) -> None:
        """Send to the vendor and relay the reply.

        settle(succeeded, usage) runs before the reply's last byte, so the
        charge is recorded by the time the agent sees the answer. succeeded is None when
        the request was sent and no reply came back: the vendor may have done the work.
        """
        own = (self.gw.upstream_auth.get(self._vendor, ""), *self.gw.upstream_headers.get(self._vendor, ()))
        headers = {
            k.lower(): v for k, v in self.headers.items()
            if k.lower() in FORWARDED_HEADERS or k.lower().startswith(FORWARDED_PREFIXES)
            or k.lower() in own
        }
        headers["accept-encoding"] = "identity"  # usage must be readable
        try:
            with self.gw.vendors.open(self.command, url, data, headers) as resp:
                self._reply(resp, settle)
        except upstream.Unsent as e:
            settle(False, {})
            return self._json(502, {"error": f"vendor unreachable: {e}"})
        except upstream.Sent as e:  # the vendor may have done the work: charged, never resent
            settle(None, {})
            self._extra["x-should-retry"] = "false"  # nor by the SDK, which would pay twice
            return self._json(502, {"error": f"vendor connection lost after the request was sent,"
                                             f" so it was not retried: {e}"})

    def _reply(self, resp, settle) -> None:
        ctype = resp.headers.get("content-type", "")
        if not 200 <= resp.status < 300:  # redirects too: followed, they would carry the vendor key
            body = resp.read()
            settle(False, {})
            return self._relay(resp.status, resp.headers.get("content-type"), *self._decoded(resp, body))
        if "text/event-stream" in ctype:
            return self._relay_stream(resp, ctype, settle)
        body, extra = self._decoded(resp, resp.read())
        try:
            parsed = json.loads(body)
        except ValueError:  # not JSON, or not text at all
            parsed = None
        settle(True, usage_from(parsed) if isinstance(parsed, dict) else {})
        if isinstance(parsed, dict) and self._redactor and self._redactor.found:
            parsed = restore_value(parsed, self._redactor.found)
            body = json.dumps(parsed).encode()
        if isinstance(parsed, dict) and self.gw.rules.tools:
            body = self._hold_tool_calls(parsed, body)
        self._relay(resp.status, ctype, body, extra)

    @staticmethod
    def _decoded(resp, body: bytes) -> tuple[bytes, dict]:
        """The body with a gzip encoding removed, and the vendor headers to relay."""
        extra = {k: v for k, v in resp.headers.items() if k.lower() not in OWN_HEADERS}
        encoding = resp.headers.get("content-encoding", "").lower()
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding not in ("", "identity"):
            extra["content-encoding"] = encoding  # relayed as it came
        return body, extra

    def _relay_stream(self, resp, ctype: str, settle) -> None:
        self.send_response(resp.status)
        self.send_header("content-type", ctype)
        self.send_header("transfer-encoding", "chunked")
        for k, v in self._extra.items():
            self.send_header(k, v)
        for k, v in resp.headers.items():  # a stream is never decoded here
            if k.lower() not in OWN_HEADERS or k.lower() == "content-encoding":
                self.send_header(k, v)
        self.end_headers()
        seen, message, kept = {}, b"", None  # kept: the events held back once a tool call starts
        checking = bool(self.gw.rules.tools)
        found = self._redactor.found if self._redactor else None
        restorer = StreamRestorer(found) if found else None
        for line in resp:
            message += line
            if line.strip():
                continue  # an event ends at a blank line
            for out in restorer.feed(message, streamed.data_of(message)) if restorer else [message]:
                kept = self._stream_event(out, seen, kept, checking)
            message = b""
        if message:
            for out in restorer.feed(message, streamed.data_of(message)) if restorer else [message]:
                kept = self._stream_event(out, seen, kept, checking)
        for out in restorer.flush() if restorer else []:
            kept = self._stream_event(out, seen, kept, checking)
        settle(True, seen)
        if kept:
            reply, origin = streamed.reply_of(kept)
            held = self.gw.rules.held(self._ids()["agent"], reply)
            note = self._held_note(held) if held else None
            for chunk in [raw for raw, _ in kept] if note is None else streamed.without(kept, held, origin, note):
                self._chunk(chunk)
        self.wfile.write(b"0\r\n\r\n")

    def _stream_event(self, message: bytes, seen: dict, kept: list | None, checking: bool) -> list | None:
        event = streamed.data_of(message)
        if event:  # counts only grow, so the largest seen is final
            for k, v in usage_from(event).items():
                seen[k] = max(seen.get(k, 0), v)
        if kept is None and checking and streamed.starts_tool(event):
            kept = []
        if kept is None:
            self._chunk(message)
        else:
            kept.append((message, event))
        return kept

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _relay(self, status: int, ctype: str | None, body: bytes, extra=None) -> None:
        self.send_response(status)
        self.send_header("content-type", ctype or "application/json")
        self.send_header("x-content-type-options", "nosniff")
        for k, v in [*self._extra.items(), *(extra or {}).items()]:
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, body) -> None:
        if status >= 400 and isinstance(body, dict) and isinstance(body.get("error"), str):
            body = self._vendor_error(status, body)
        if status == 429:  # only a lockout: it lasts the rest of the window
            self._extra |= {"retry-after": str(LOGIN_WINDOW), "x-should-retry": "false"}
        self._relay(status, "application/json", json.dumps(body).encode())

    def _vendor_error(self, status: int, body: dict) -> dict:
        """A gateway call's refusal in its vendor's error shape: OpenAI's, or Anthropic's for
        /anthropic. Quarter's code and detail go in the error object, where each SDK keeps it, and
        the detail stays beside it too (approval_id, reason). Other routes are as given."""
        first = urlsplit(self.path).path.lstrip("/").partition("/")[0]
        if getattr(self, "_vendor", None) is None and first not in self.gw.upstreams:
            return body
        error, detail = body["error"], {k: v for k, v in body.items() if k != "error"}
        code = error if re.fullmatch(r"[a-z_]+", error) else None
        said = "Quarter refused this call" if status < 500 else "Quarter could not complete this call"
        message = REFUSALS[code].format_map(detail) if code in REFUSALS else f"{said}: {error}"
        shaped = {"type": ERROR_TYPES.get(status, "api_error"), "message": message, "code": code, **detail}
        if first.startswith("anthropic"):
            return {"type": "error", "error": shaped, **detail}
        return {"error": shaped, **detail}

    def log_message(self, *args):  # keep request logs out of stderr
        pass


class Server(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS at once, and each request read within REQUEST_DEADLINE."""

    request_queue_size = 128  # the default of 5 reset connections when agents opened many at once

    def __init__(self, *args):
        super().__init__(*args)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._deadlines: dict[socket.socket, float] = {}
        self._deadlines_lock = threading.Lock()
        threading.Thread(target=self._reap, daemon=True).start()

    def deadline(self, conn: socket.socket, at: float | None) -> None:
        with self._deadlines_lock:
            if at is None:
                self._deadlines.pop(conn, None)
            else:
                self._deadlines[conn] = at

    def _reap(self):
        while True:
            time.sleep(1)
            now = time.monotonic()
            with self._deadlines_lock:
                late = [c for c, at in self._deadlines.items() if at < now]
                for conn in late:
                    del self._deadlines[conn]
            for conn in late:
                try:
                    conn.shutdown(socket.SHUT_RDWR)  # the handler's read returns, and it closes
                except OSError:
                    pass

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(gw: Gateway, host: str = "127.0.0.1", port: int = 8780) -> ThreadingHTTPServer:
    # timeout: else a client that stops sending holds a thread for good
    handler = type("Handler", (Handler,), {"gw": gw, "timeout": CLIENT_TIMEOUT})
    return Server((host, port), handler)
