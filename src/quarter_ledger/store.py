"""The ledger: an append-only record of every hold, charge and release, plus approvals
and the costs people pay for their agents.

Before a call, its worst-case cost is held. After the call the hold is replaced by the
real charge. Checking caps and placing the hold happen in one write transaction, so two
calls running at once can never both fit under a cap that only has room for one.
"""

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from . import register as agent_register
from .policy import Cap, Decision, Shadow, decide

SCHEMA = """
create table if not exists entries (
  id integer primary key,
  call_id text not null,
  kind text not null check (kind in ('hold', 'charge', 'release')),
  at text not null,
  agent text, task text, customer text,
  vendor text not null, model text,
  amount integer not null
);
create index if not exists entries_call on entries (call_id);
create index if not exists entries_agent on entries (agent, at);
create index if not exists entries_customer on entries (customer, at);
create index if not exists entries_task on entries (task, at);

create table if not exists approvals (
  id text primary key,
  created_at text not null,
  agent text not null, task text, customer text,
  vendor text not null, model text,
  estimate integer not null,
  reason text not null,
  status text not null check (status in ('pending', 'approved', 'denied', 'used')),
  decided_at text, decided_by text
);

-- What people spend on agents: seats, subscriptions, compute, people. A monthly cost
-- counts in every month from start_month to end_month (open-ended while null).
create table if not exists costs (
  id integer primary key,
  at text not null,
  start_month text not null,
  end_month text,
  monthly integer not null check (monthly in (0, 1)),
  agent text, customer text,
  category text not null,
  vendor text not null,
  amount integer not null,
  note text
);

-- The person responsible for each agent. They may not approve that agent's spending.
create table if not exists owners (
  agent text primary key,
  owner text not null
);

-- Agents stopped because they spent far faster than usual, until a person resumes them.
create table if not exists pauses (
  id integer primary key,
  agent text not null,
  at text not null,
  reason text not null,
  resumed_at text, resumed_by text
);
create index if not exists pauses_open on pauses (agent, resumed_at);

-- One row per priced call: what it was made of and why it cost that. Hashes and byte sizes
-- of the prompt only, never its text. Not chained: entries hold the money.
create table if not exists calls (
  call_id text primary key,
  at text not null,
  agent text, task text,
  subagent text, parent_agent text, prompt_id text, request_class text,
  vendor text not null, model text,
  route text not null check (route in ('api', 'subscription')),
  input integer not null, output integer not null,
  cache_read integer not null, cache_write integer not null,
  cost integer not null, list_cost integer not null,
  tools_cost integer not null, system_cost integer not null,
  history_cost integer not null, output_cost integer not null,
  fingerprint text not null,
  cache_break text, break_cost integer not null,
  upstream text,  -- where the call went: a route may send it to an upstream other than vendor
  redacted text,  -- what was masked before the call left, by kind and count ("card:1,email:2"), never the values
  saved integer not null default 0  -- micros the cache marker Quarter added saved, at list price
);
create index if not exists calls_task on calls (task, at);

-- Who did what on the finance side, including failed sign-ins. Never edited.
create table if not exists bills (
  id text primary key,
  created_at text not null,
  agent text not null, task text, customer text,
  payee text not null,
  invoice text not null,
  amount integer not null,
  due text,
  account text not null,
  account_end text not null,
  memo text,
  status text not null,
  reason text,
  approval_id text, call_id text,
  paid_at text, paid_by text, method text, ref text
);
create unique index if not exists bills_invoice on bills (lower(payee), invoice)
  where status not in ('refused', 'denied');
create table if not exists payee_accounts (
  payee text primary key,
  account text not null,
  account_end text not null,  -- both '' for a payee with tax details and no payment yet
  at text not null,
  w9_at text, entity text, tin text, tin_end text, pays_for text  -- see tax.py
);
create table if not exists standing (
  id text primary key,
  created_at text not null,
  created_by text not null,
  agent text not null,
  vendor text not null,
  up_to integer not null,
  monthly integer not null,
  ends text not null,
  revoked_at text, revoked_by text
);
-- Quarterly owner reviews (register.py): each agent kept or retired, by whom. Never edited.
create table if not exists reviews (
  id integer primary key,
  at text not null,
  quarter text not null,
  agent text not null,
  owner text,
  reviewer text not null,
  result text not null check (result in ('keep', 'retire')),
  note text
);
create index if not exists reviews_agent on reviews (agent, quarter);
create table if not exists audit (
  id integer primary key,
  at text not null,
  action text not null,
  client text,
  detail text
);
"""

SCOPE_COLUMNS = {"agent", "task", "customer", "vendor"}
MAX_SUMS = 10_000  # cap windows whose running sums are kept (see Ledger.spent)
# Spend connect.py read from a source the gateway never saw: charges whose call_id starts
# "obs:". Reports count them; no gateway decision does (caps, loop stops, standing, register).
GATEWAY_ONLY = "substr(call_id, 1, 4) != 'obs:'"
CALL_COLUMNS = (
    "call_id", "at", "agent", "task", "subagent", "parent_agent", "prompt_id", "request_class",
    "vendor", "model", "route", "input", "output", "cache_read", "cache_write", "cost",
    "list_cost", "tools_cost", "system_cost", "history_cost", "output_cost", "fingerprint",
    "cache_break", "break_cost", "upstream", "redacted", "saved",
)
CHAINED = {
    "entries": ("call_id", "kind", "at", "agent", "task", "customer", "vendor", "model", "amount"),
    "audit": ("at", "action", "client", "detail"),
}


def chain_hash(prev: str, row: tuple) -> str:
    text = prev + "\x1f" + "\x1f".join("" if v is None else str(v) for v in row)
    return hashlib.sha256(text.encode()).hexdigest()


def month_range(month: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def now() -> str:
    return datetime.now(UTC).isoformat()


def dicts(cur: sqlite3.Cursor) -> list[dict]:
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


class Ledger:
    """One connection per thread: the gateway serves calls on many threads, and a
    connection shared between them would mix one call's transaction into another's."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":  # owner only; sqlite gives the -wal and -shm files the same mode
            os.close(os.open(path, os.O_CREAT | os.O_RDONLY, 0o600))
        self._local = threading.local()
        self._sums: dict[tuple, tuple[int, int]] = {}  # see spent
        self._sums_lock = threading.Lock()
        self.register: agent_register.RegisterRule | None = None  # QUARTER_REGISTER: see hold
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        with self._write():  # locked, so two processes opening at once cannot race
            for table, column, kind in (
                ("entries", "hash", "text"), ("audit", "hash", "text"), ("approvals", "call_id", "text"),
                ("approvals", "slack_channel", "text"), ("approvals", "slack_ts", "text"),
                ("pauses", "task", "text"), ("calls", "upstream", "text"), ("calls", "redacted", "text"),
                ("calls", "saved", "integer not null default 0"),
                *(("payee_accounts", c, "text") for c in ("w9_at", "entity", "tin", "tin_end", "pays_for")),
            ):
                have = {row[1] for row in self.db.execute(f"pragma table_info({table})")}
                if column not in have:
                    self.db.execute(f"alter table {table} add column {column} {kind}")

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.execute("pragma busy_timeout=5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self):
        self.db.execute("begin immediate")
        try:
            yield
            self.db.execute("commit")
        except BaseException:
            self.db.execute("rollback")
            with self._sums_lock:  # a sum may have counted entries that are now gone
                self._sums.clear()
            raise

    def spent(self, scope: str, value: str, since: datetime | None, observed: bool = False) -> int:
        """Charges plus holds that are still open. Observed spend (connect.py) only with
        observed set: the gateway decides on what passed through it.

        A cap's window starts at midnight or at no time at all, and every call asks for the
        same few windows, so their sums are kept with the last entry id they cover and only
        newer entries are added. Entries are never edited or deleted, so this equals summing
        the window afresh, in any process. Other windows are summed afresh."""
        if scope != "all" and scope not in SCOPE_COLUMNS:
            raise ValueError(scope)
        where = "1 = 1" if scope == "all" else f"{scope} = ?"
        args: list = [] if scope == "all" else [value]
        if not observed:
            where += f" and {GATEWAY_ONLY}"
        if since is not None:
            where += " and at >= ?"
            args.append(since.isoformat())
        signed = "case kind when 'release' then -amount else amount end"
        if since is not None and since != since.replace(hour=0, minute=0, second=0, microsecond=0):
            return self.db.execute(f"select coalesce(sum({signed}), 0) from entries where {where}", args).fetchone()[0]
        key = (scope, value, args[-1] if since is not None else None, observed)
        with self._sums_lock:
            cached = self._sums.get(key)
        if cached is None:
            last = self.db.execute("select coalesce(max(id), 0) from entries").fetchone()[0]
            total = self.db.execute(
                f"select coalesce(sum({signed}), 0) from entries where {where} and id <= ?", [*args, last]
            ).fetchone()[0]
        else:  # by id alone, so only the entries after `last` are read
            last, total = cached
            newest, more = self.db.execute(
                f"select max(id), coalesce(sum(case when {where} then {signed} end), 0)"
                " from entries where id > ?", [*args, last]
            ).fetchone()
            last, total = newest or last, total + more
        with self._sums_lock:
            self._sums[key] = (last, total)
            while len(self._sums) > MAX_SUMS:
                self._sums.pop(next(iter(self._sums)))
        return total

    def hold(
        self,
        caps: list[Cap],
        ids: dict[str, str | None],
        vendor: str,
        model: str,
        estimate: int,
        approval_id: str | None = None,
        fallback: tuple[str, int] | None = None,
        ask: str | None = None,
        deny: str | None = None,
        shadow: Shadow | None = None,
        sanctions: str | None = None,
    ) -> tuple[Decision, str | None]:
        """Check caps and hold the estimate.

        ask is a reason this call needs a person even within its caps (a payee not on the
        list, say); the person sees it beside any reason the caps give. deny is a reason to
        refuse it outright; only a payee list in shadow passes one here. sanctions is a reason
        from the sanctions screen: it needs a person like ask, and no shadow mode passes it.

        fallback is (cheaper model, its estimate): tried in the same transaction when the
        call would otherwise be refused or sent for approval. Returns (decision, call_id
        or approval_id); decision.fallback_to names the model that was held.
        Paused agents are refused before anything else.

        Shadow: a cap whose mode is "shadow", the ask and deny reasons with shadow.payees,
        and everything (pauses too) with shadow.everything never refuse. A call they alone
        would hold is held and charged as if allowed, decision.would_hold says what enforcing
        would have done, and a would_hold audit row records it. Every other rule still decides.

        With self.register set, an agent with no owner, unused, or past its quarterly review
        is sent to a person (register.py); in shadow, recorded only. An approval lets one call
        through and is not a review."""
        shadow = shadow or Shadow()
        with self._write():
            paused = self.pause_reason(ids)
            if paused is not None and not shadow.everything:
                return Decision("block", paused), None
            approved = approval_id is not None and self._use_approval(approval_id, ids, vendor, estimate)
            enforced = [] if shadow.everything else [c for c in caps if c.mode != "shadow"]
            soft = shadow.everything or shadow.payees  # the ask and deny reasons are in shadow
            due = self._register_reason(ids) if self.register and not approved else None
            quiet = due and (shadow.everything or self.register.shadow)
            live_ask = "; ".join(r for r in (sanctions, None if quiet else due, None if soft else ask) if r) or None
            decision, standing = self.judge(
                enforced, ids, vendor, estimate, approved, live_ask, None if soft else deny
            )
            would = None
            if decision.outcome == "allow" and (paused or enforced != caps or (soft and (ask or deny)) or quiet):
                full_ask = "; ".join(r for r in (sanctions, due, ask) if r) or None
                full, _ = self.judge(caps, ids, vendor, estimate, approved, full_ask, deny)
                would = Decision("block", paused) if paused else full if full.outcome != "allow" else None
            if decision.outcome != "allow" and fallback is not None and not live_ask:
                cheaper, cheaper_estimate = fallback
                if decide(enforced, ids, cheaper_estimate, self.spent).outcome == "allow":
                    call_id = uuid.uuid4().hex
                    self._append(call_id, "hold", ids, vendor, cheaper, cheaper_estimate)
                    return Decision("allow", decision.reason, fallback_to=cheaper), call_id
            if decision.outcome == "allow":
                call_id = uuid.uuid4().hex
                self._append(call_id, "hold", ids, vendor, model, estimate)
                if approved:  # link the approval to the call it paid for
                    self.db.execute(
                        "update approvals set call_id = ? where id = ?", (call_id, approval_id)
                    )
                if standing:
                    self._audit_row("standing_used", None, f"{standing} {call_id} {vendor} {estimate}")
                if would:
                    self._would_hold(call_id, ids, vendor, model, estimate, would, shadow.approvers)
                    decision = Decision("allow", decision.reason, decision.fallback_to, would)
                return decision, call_id
            if decision.outcome == "approve":
                return decision, self._request_approval(ids, vendor, model, estimate, decision)
            if approved:  # nothing was spent, so the approval is still there for the retry
                self.db.execute("update approvals set status = 'approved' where id = ?", (approval_id,))
            self._audit_row("refused", None, json.dumps(
                {**self._call_detail(ids, vendor, model, estimate), "reason": decision.reason}, sort_keys=True
            ))
            return decision, None

    def judge(
        self,
        caps: list[Cap],
        ids: dict[str, str | None],
        vendor: str,
        estimate: int,
        approved: bool = False,
        ask: str | None = None,
        deny: str | None = None,
        at: datetime | None = None,
    ) -> tuple[Decision, str | None]:
        """The decision for one call from the ledger as it stands, and the standing approval
        that lets it skip the approval line. Reads only: hold acts on it, and replay asks it
        about calls already made, with `at` the moment each was made."""
        if deny:
            return Decision("block", deny), None
        decision = decide(caps, ids, estimate, self.spent, skip_approval=approved, now=at)
        if ask and not approved and decision.outcome != "block":
            reason = "; ".join(r for r in (ask, decision.reason) if r)
            decision = Decision("approve", reason)
        standing = None
        if decision.outcome == "approve" and not ask and not approved:
            standing = self._standing_for(ids.get("agent"), vendor, estimate, at)
            if standing:
                covered = decide(caps, ids, estimate, self.spent, skip_line=True, now=at)
                decision = covered if covered.outcome == "allow" else decision
                standing = standing if covered.outcome == "allow" else None
        return decision, standing

    def _register_reason(self, ids) -> str | None:
        """Why the register sends this agent's call to a person, inside hold's transaction:
        one statement of indexed lookups."""
        if not ids.get("agent"):
            return None
        found = agent_register.reasons(*agent_register.state(self, ids["agent"]), datetime.now(UTC), self.register)
        return "; ".join(reason for _, reason in found) or None

    def pause_reason(self, ids) -> str | None:
        reason = self.paused(ids["agent"]) if ids.get("agent") else None
        if reason is not None:
            return f"agent paused: {reason}"
        reason = self.task_paused(ids["task"]) if ids.get("task") else None
        return f"task paused: {reason}" if reason is not None else None

    @staticmethod
    def _call_detail(ids, vendor, model, amount) -> dict:
        return {"agent": ids.get("agent"), "task": ids.get("task"), "customer": ids.get("customer"),
                "vendor": vendor, "model": model, "amount": amount}

    def _would_hold(self, call_id, ids, vendor, model, amount, would: Decision, approvers) -> None:
        """The audit row for a call shadow let through: the rule, the amount, and who would
        have been asked (no one, for a refusal; never the agent's owner)."""
        asked = []
        if would.outcome == "approve":
            owner = self.db.execute("select owner from owners where agent = ?", (ids.get("agent"),)).fetchone()
            asked = [a for a in approvers if not (owner and a.lower() == owner[0].lower())]
        detail = {**self._call_detail(ids, vendor, model, amount), "call_id": call_id,
                  "outcome": would.outcome, "reason": would.reason, "asked": asked}
        self._audit_row("would_hold", None, json.dumps(detail, sort_keys=True))

    def settle(self, call_id: str, actual: int | None) -> None:
        """Release the hold and record the real cost (None: the call failed, nothing owed)."""
        if actual is not None and actual < 0:
            raise ValueError("a charge cannot be negative")
        with self._write():
            row = self.db.execute(
                "select agent, task, customer, vendor, model, amount from entries"
                " where call_id = ? and kind = 'hold' and not exists"
                " (select 1 from entries r where r.call_id = ? and r.kind = 'release')",
                (call_id, call_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"no open hold {call_id}")
            agent, task, customer, vendor, model, held = row
            ids = {"agent": agent, "task": task, "customer": customer}
            self._append(call_id, "release", ids, vendor, model, held)
            if actual:
                self._append(call_id, "charge", ids, vendor, model, actual)

    def book_fail_open(self, call: dict, charged: int) -> bool:
        """Book a call the gateway let through while the ledger was down (failopen.py), at the
        time it was made. False if that call id is already booked."""
        ids = {k: call.get(k) for k in ("agent", "task", "customer")}
        with self._write():
            if self.db.execute("select 1 from entries where call_id = ?", (call["id"],)).fetchone():
                return False
            for kind, amount in (("hold", call["held"]), ("release", call["held"]), ("charge", charged)):
                if kind != "charge" or charged:
                    self._append(call["id"], kind, ids, call["vendor"], call["model"], amount, call["at"])
            detail = {**ids, "id": call["id"], "at": call["at"], "vendor": call["vendor"],
                      "model": call["model"], "held": call["held"], "charged": charged,
                      "reason": call.get("reason")}
            self._audit_row("fail_open", None, json.dumps(detail, sort_keys=True))
        return True

    # Spend on agents

    def add_cost(
        self,
        category: str,
        vendor: str,
        amount: int,
        month: str,
        monthly: bool = False,
        agent: str | None = None,
        customer: str | None = None,
        note: str = "",
    ) -> int:
        with self._write():
            return self.db.execute(
                "insert into costs (at, start_month, monthly, agent, customer, category, vendor,"
                " amount, note) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now(), month, int(monthly), agent, customer, category, vendor, amount, note),
            ).lastrowid

    def end_cost(self, cost_id: int, last_month: str) -> bool:
        with self._write():
            return (
                self.db.execute(
                    "update costs set end_month = ? where id = ? and monthly = 1"
                    " and start_month <= ?",
                    (last_month, cost_id, last_month),
                ).rowcount
                == 1
            )

    def costs_in(self, month: str) -> list[dict]:
        """Every cost that counts in a month (YYYY-MM)."""
        cur = self.db.execute(
            "select id, agent, customer, category, vendor, amount, monthly, note from costs"
            " where (monthly = 0 and start_month = ?)"
            " or (monthly = 1 and start_month <= ? and (end_month is null or end_month >= ?))"
            " order by agent, category",
            (month, month, month),
        )
        return dicts(cur)

    # Approvals

    def _request_approval(self, ids, vendor, model, estimate, decision) -> str:
        approval_id = "apr_" + uuid.uuid4().hex[:16]
        self.db.execute(
            "insert into approvals (id, created_at, agent, task, customer, vendor, model,"
            " estimate, reason, status) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (approval_id, now(), ids["agent"], ids.get("task"), ids.get("customer"), vendor,
             model, estimate, decision.reason),
        )
        return approval_id

    def _use_approval(self, approval_id: str, ids, vendor: str, estimate: int) -> bool:
        """An approval covers one retry by the same agent to the same vendor (or payee), up
        to the approved amount."""
        updated = self.db.execute(
            "update approvals set status = 'used' where id = ? and status = 'approved'"
            " and agent = ? and vendor = ? and estimate >= ?",
            (approval_id, ids["agent"], vendor, estimate),
        ).rowcount
        return updated == 1

    def approved_for(self, agent: str, vendor: str, amount: int) -> str | None:
        """An approved, unused approval this payment fits, for retries that cannot carry
        its id (a card swipe)."""
        row = self.db.execute(
            "select id from approvals where status = 'approved' and agent = ? and vendor = ?"
            " and estimate >= ? order by created_at desc limit 1",
            (agent, vendor, amount),
        ).fetchone()
        return row[0] if row else None

    def request_action_approval(self, ids, upstream: str, action: str, reason: str) -> str:
        """An approval a rule asks for: for the action itself, not its cost, so estimate 0."""
        with self._write():
            return self._request_approval(ids, upstream, action, 0, Decision("approve", reason))

    def use_action_approval(self, approval_id: str, agent: str, upstream: str, action: str) -> bool:
        """An action approval covers one call by the same agent doing the same action."""
        with self._write():
            return self.db.execute(
                "update approvals set status = 'used' where id = ? and status = 'approved'"
                " and agent = ? and vendor = ? and model = ? and estimate = 0",
                (approval_id, agent, upstream, action),
            ).rowcount == 1

    def audit(self, action: str, client: str | None, detail: str = "") -> None:
        with self._write():
            self._audit_row(action, client, detail)

    def record_once(self, action: str, client: str | None, detail: str, key: tuple) -> bool:
        """An audit row whose detail is JSON, unless one with the same source and id is
        there already (vendors send an event more than once)."""
        with self._write():
            if self.db.execute(
                "select 1 from audit where action = ? and case when json_valid(detail) then"
                " json_extract(detail, '$.source') = ? and json_extract(detail, '$.id') = ? end"
                " limit 1",
                (action, *key),
            ).fetchone():
                return False
            self._audit_row(action, client, detail)
            return True

    def _audit_row(self, action: str, client: str | None, detail: str) -> None:
        """An audit row, inside a transaction already open."""
        row = (now(), action, client, detail)
        self.db.execute(
            "insert into audit (at, action, client, detail, hash) values (?, ?, ?, ?, ?)",
            (*row, self._next_hash("audit", row)),
        )

    def record_config(self, settings: dict) -> bool:
        """Audit the rules in force when they differ from the last recorded ones.

        Each section (caps, prices, ...) is hashed, so the audit log shows which changed
        and when; with the hash chain, a later edit to that history shows too."""
        hashes = {
            name: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]
            for name, value in sorted(settings.items())
        }
        last = self.db.execute(
            "select detail from audit where action = 'config' order by id desc limit 1"
        ).fetchone()
        previous = json.loads(last[0]) if last else {}
        if previous == hashes:
            return False
        changed = sorted(k for k in hashes.keys() | previous.keys() if hashes.get(k) != previous.get(k))
        self.audit("config", None, json.dumps(hashes, sort_keys=True))
        self.audit("config.changed", None, ", ".join(changed))
        return True

    def policy_ref(self) -> str | None:
        """The chain hash of the latest recorded config: which caps and rules were in force."""
        row = self.db.execute(
            "select hash from audit where action = 'config' order by id desc limit 1"
        ).fetchone()
        return row[0] if row else None

    def entry_hash(self, call_id: str) -> str | None:
        """The chain hash of a call's hold, so an auditor can find it in the ledger."""
        row = self.db.execute(
            "select hash from entries where call_id = ? and kind = 'hold'", (call_id,)
        ).fetchone()
        return row[0] if row else None

    def audit_log(self, limit: int = 200) -> list[dict]:
        return dicts(self.db.execute(
            "select at, action, client, detail from audit order by id desc limit ?", (limit,)
        ))

    def set_owner(self, agent: str, owner: str) -> None:
        with self._write():
            self.db.execute("insert or replace into owners (agent, owner) values (?, ?)", (agent, owner))
        self.audit("owner", None, f"{agent} -> {owner}")

    # Standing approvals: a person agrees ahead of time that an agent may spend up to an
    # amount per call with one vendor or payee, and up to a monthly total, without asking.
    # They cover only the approval line; caps still hold. Each ends, and each use is audited.

    def add_standing(self, agent: str, vendor: str, up_to: int, monthly: int, by: str, days: int = 90) -> str:
        if up_to <= 0 or monthly < up_to or not 1 <= days <= 366:
            raise ValueError("need 0 < up to <= monthly, and 1 to 366 days")
        owner = self.db.execute("select owner from owners where agent = ?", (agent,)).fetchone()
        if owner and owner[0].lower() == by.lower():
            self.audit("self_approval_refused", None, f"standing {agent} {vendor} {by}")
            raise PermissionError(f"{by} owns {agent} and cannot approve its spending ahead of time")
        rule_id = "std_" + uuid.uuid4().hex[:12]
        ends = (datetime.now(UTC) + timedelta(days=days)).isoformat()
        with self._write():
            self.db.execute(
                "insert into standing (id, created_at, created_by, agent, vendor, up_to, monthly, ends)"
                " values (?, ?, ?, ?, ?, ?, ?, ?)",
                (rule_id, now(), by, agent, vendor, up_to, monthly, ends),
            )
        self.audit("standing_added", by, f"{rule_id} {agent} {vendor} up to {up_to} monthly {monthly} until {ends[:10]}")
        return rule_id

    def standing(self, active: bool = True) -> list[dict]:
        sql = "select * from standing"
        if active:
            sql += " where revoked_at is null and ends > ?"
        return dicts(self.db.execute(sql + " order by created_at", (now(),) if active else ()))

    def revoke_standing(self, rule_id: str, by: str) -> bool:
        with self._write():
            ok = self.db.execute(
                "update standing set revoked_at = ?, revoked_by = ? where id = ? and revoked_at is null",
                (now(), by, rule_id),
            ).rowcount == 1
        if ok:
            self.audit("standing_revoked", by, rule_id)
        return ok

    def _standing_for(self, agent: str | None, vendor: str, estimate: int, at: datetime | None = None) -> str | None:
        """An active standing approval this call fits, inside hold's transaction (or as at
        `at`, for replay)."""
        if not agent:
            return None
        at = at or datetime.now(UTC)
        start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        for rule_id, monthly in self.db.execute(
            "select id, monthly from standing where (revoked_at is null or revoked_at > ?)"
            " and created_at <= ? and ends > ? and agent = ?"
            " and lower(vendor) = lower(?) and up_to >= ? order by up_to",
            (at.isoformat(), at.isoformat(), at.isoformat(), agent, vendor, estimate),
        ).fetchall():
            month = self.db.execute(
                "select coalesce(sum(case kind when 'release' then -amount else amount end), 0)"
                f" from entries where agent = ? and lower(vendor) = lower(?) and at >= ? and {GATEWAY_ONLY}",
                (agent, vendor, start),
            ).fetchone()[0]
            if month + estimate <= monthly:
                return rule_id
        return None

    def decide_approval(self, approval_id: str, approve: bool, by: str) -> bool:
        """Raises PermissionError when the approver owns the agent asking (approving only;
        an owner may always deny)."""
        by = by.strip()  # " Sam" is Sam: a space must not get round the owner rule
        if approve:
            row = self.db.execute(
                "select o.owner from approvals a join owners o on o.agent = a.agent where a.id = ?",
                (approval_id,),
            ).fetchone()
            if row and row[0].strip().lower() == by.lower():
                self.audit("self_approval_refused", None, f"{approval_id} {by}")
                raise PermissionError(f"{by} owns this agent and cannot approve its spending")
        with self._write():
            return (
                self.db.execute(
                    "update approvals set status = ?, decided_at = ?, decided_by = ?"
                    " where id = ? and status = 'pending'",
                    ("approved" if approve else "denied", now(), by, approval_id),
                ).rowcount
                == 1
            )

    # Pauses: an agent stopped for spending far faster than usual, or one task stopped
    # for looping. A row with a task pauses that task only.

    def paused(self, agent: str) -> str | None:
        row = self.db.execute(
            "select reason from pauses where agent = ? and task is null and resumed_at is null",
            (agent,),
        ).fetchone()
        return row[0] if row else None

    def task_paused(self, task: str) -> str | None:
        row = self.db.execute(
            "select reason from pauses where task = ? and resumed_at is null", (task,)
        ).fetchone()
        return row[0] if row else None

    def pause(self, agent: str, reason: str, task: str | None = None) -> bool:
        """False if the agent, or the task when one is named, was already paused."""
        with self._write():
            if (self.task_paused(task) if task else self.paused(agent)) is not None:
                return False
            self.db.execute(
                "insert into pauses (agent, task, at, reason) values (?, ?, ?, ?)",
                (agent, task, now(), reason),
            )
            return True

    def resume(self, agent: str, by: str) -> bool:
        return self._resume("agent = ? and task is null", agent, by)

    def resume_task(self, task: str, by: str) -> bool:
        return self._resume("task = ?", task, by)

    def _resume(self, which: str, value: str, by: str) -> bool:
        with self._write():
            return (
                self.db.execute(
                    f"update pauses set resumed_at = ?, resumed_by = ?"
                    f" where {which} and resumed_at is null",
                    (now(), by, value),
                ).rowcount
                > 0
            )

    def paused_agents(self) -> list[dict]:
        """Every open pause; task is set on those that pause one task only."""
        return dicts(self.db.execute(
            "select agent, task, at, reason from pauses where resumed_at is null order by at"
        ))

    def approval(self, approval_id: str) -> dict | None:
        rows = dicts(self.db.execute(
            "select id, created_at, agent, task, customer, vendor, model, estimate, reason, status,"
            " decided_at, decided_by from approvals where id = ?",
            (approval_id,),
        ))
        return rows[0] if rows else None

    def set_slack_message(self, approval_id: str, channel: str, ts: str) -> None:
        """The Slack message showing this approval, kept so any decision can update it."""
        with self._write():
            self.db.execute(
                "update approvals set slack_channel = ?, slack_ts = ? where id = ? and slack_ts is null",
                (channel, ts, approval_id),
            )

    def slack_message(self, approval_id: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "select slack_channel, slack_ts from approvals where id = ? and slack_ts is not null",
            (approval_id,),
        ).fetchone()
        return tuple(row) if row else None

    def approvals(self, status: str = "pending") -> list[dict]:
        return dicts(self.db.execute(
            "select id, created_at, agent, task, customer, vendor, model, estimate, reason, status"
            " from approvals where status = ? order by created_at",
            (status,),
        ))

    # Run forensics

    def record_call(self, call: dict) -> None:
        """One priced call's anatomy (see forensics.py); missing columns are null."""
        with self._write():
            self.db.execute(
                f"insert into calls ({', '.join(CALL_COLUMNS)})"
                f" values ({', '.join('?' * len(CALL_COLUMNS))})",
                tuple(call.get(c) for c in CALL_COLUMNS),
            )

    def saved_since(self, since: datetime) -> tuple[int, int]:
        """What the cache markers Quarter added saved since then, and on how many calls."""
        row = self.db.execute(
            "select coalesce(sum(saved), 0), count(*) from calls where saved != 0 and at >= ?",
            (since.isoformat(),),
        ).fetchone()
        return row[0], row[1]

    def last_call(self, lane: tuple, before: str) -> dict | None:
        """The latest call started before `before` in a lane: (task, subagent, request class)."""
        rows = dicts(self.db.execute(
            "select * from calls where task = ? and subagent is ? and request_class is ?"
            " and at < ? order by at desc, rowid desc limit 1",
            (*lane, before),
        ))
        return rows[0] if rows else None

    # Reports

    def totals(
        self, scope: str, since: datetime | None = None, until: datetime | None = None
    ) -> list[tuple[str | None, str, int]]:
        """Charges per scope id and vendor."""
        if scope not in SCOPE_COLUMNS:
            raise ValueError(scope)
        sql = f"select {scope}, vendor, sum(amount) from entries where kind = 'charge'"
        args: list = []
        if since is not None:
            sql += " and at >= ?"
            args.append(since.isoformat())
        if until is not None:
            sql += " and at < ?"
            args.append(until.isoformat())
        sql += f" group by {scope}, vendor order by {scope}, vendor"
        return self.db.execute(sql, args).fetchall()

    def spend_by(
        self, scope: str, since: datetime | None = None, until: datetime | None = None
    ) -> dict[str, int]:
        """Charges per scope id, summed over vendors; spend with no id is "(none)"."""
        out: dict[str, int] = {}
        for key, _vendor, amount in self.totals(scope, since, until):
            out[key or "(none)"] = out.get(key or "(none)", 0) + amount
        return out

    def _append(self, call_id, kind, ids, vendor, model, amount, at: str | None = None) -> None:
        row = (call_id, kind, at or now(), ids.get("agent"), ids.get("task"), ids.get("customer"),
               vendor, model, amount)
        self.db.execute(
            "insert into entries (call_id, kind, at, agent, task, customer, vendor, model,"
            " amount, hash) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*row, self._next_hash("entries", row)),
        )

    # The hash chain: every row carries a hash of itself and the row before it, so an
    # edited, inserted or deleted row breaks every hash after it.

    def _next_hash(self, table: str, row: tuple) -> str:
        last = self.db.execute(
            f"select hash from {table} where hash is not null order by id desc limit 1"
        ).fetchone()
        return chain_hash(last[0] if last else "", row)

    def verify_chain(self) -> dict:
        """Recompute both chains. Rows written before the chain existed are skipped."""
        result = {}
        for table, columns in CHAINED.items():
            prev, checked, broken = "", 0, None
            cur = self.db.execute(f"select id, {', '.join(columns)}, hash from {table} order by id")
            started = False
            for row_id, *values, stored in cur:
                if stored is None and not started:
                    continue
                started = True
                expected = chain_hash(prev, tuple(values))
                if stored != expected:
                    broken = row_id
                    break
                prev, checked = stored, checked + 1
            result[table] = {"checked": checked, "intact": broken is None, "first_bad_id": broken}
        return result


def agent_summary(ledger: Ledger, month: str) -> list[dict]:
    """Per agent for a month: spent by it (calls) and spent on it (costs)."""
    start, end = month_range(month)
    rows: dict[str, dict] = {}

    def row(agent):
        name = agent or "(company)"
        return rows.setdefault(name, {"agent": name, "by": 0, "on": 0})

    for agent, _vendor, amount in ledger.totals("agent", start, end):
        row(agent)["by"] += amount
    for cost in ledger.costs_in(month):
        row(cost["agent"])["on"] += cost["amount"]
    return sorted(rows.values(), key=lambda r: -(r["by"] + r["on"]))
