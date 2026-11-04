"""Bill pay from the customer's own account.

A bill goes through the same caps, payee list and approvals as any agent spend, plus
three checks accounts payable relies on: an invoice already entered is refused, the same
amount to the same payee within 30 days waits for a person, and a first or changed bank
account for a payee waits for a person (changed bank details are how invoice fraud works).

Quarter never holds the money. It pays through the customer's own Mercury account with
the customer's own token, or records a payment a person made from any bank. Bank details
are kept only as a fingerprint and the last four characters; a Mercury recipient id is
kept as it is, since paying needs it and it reveals nothing.
"""

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import UTC, date, datetime, timedelta

from .money import MAX_AMOUNT, to_cents
from .payments import payee_rule
from .policy import Cap
from .store import Ledger, dicts, now
from .tax import warn_if_no_w9

RAIL = "invoice"
SIMILAR_DAYS = 30
MERCURY_URL = "https://api.mercury.com/api/v1"
MERCURY_METHODS = ("ach", "check", "domesticWire", "realTimePayment")
RECIPIENT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLOSED = ("refused", "denied")


class Duplicate(ValueError):
    pass


def _account(text: str) -> tuple[str, str]:
    """What is kept of a bank account: a Mercury recipient id as it is, anything else as
    a fingerprint; and its last four characters, for people to recognise it."""
    plain = re.sub(r"[\s-]", "", text).lower()
    if not plain:
        raise ValueError("the bill needs the account it is paid to")
    if RECIPIENT.match(text.strip().lower()):
        return text.strip().lower(), plain[-4:]
    return "sha256:" + hashlib.sha256(plain.encode()).hexdigest()[:24], plain[-4:]


def vendor(payee: str) -> str:
    return f"{RAIL}:{payee}"


def bill(ledger: Ledger, bill_id: str) -> dict | None:
    rows = dicts(ledger.db.execute("select * from bills where id = ?", (bill_id,)))
    return rows[0] if rows else None


def listed(ledger: Ledger, status: str | None = None) -> list[dict]:
    sql = (
        "select b.*, p.w9_at is null as no_w9 from bills b"
        " left join payee_accounts p on p.payee = lower(b.payee)"
    ) + (" where b.status = ?" if status else "")
    return dicts(
        ledger.db.execute(
            sql + " order by coalesce(b.due, '9999'), b.created_at", (status,) if status else ()
        )
    )


def add(
    ledger: Ledger,
    caps: list[Cap],
    payees: dict,
    ids: dict,
    payee: str,
    invoice: str,
    amount: int,
    account: str,
    due: str | None = None,
    memo: str = "",
    screen=None,
) -> dict:
    """Enter a bill. It is approved to pay, waiting for a person, or refused. screen is the
    sanctions screen (sanctions.Screen), if lists are stored."""
    payee, invoice = payee.strip(), invoice.strip()
    if not payee or not invoice:
        raise ValueError("the bill needs a payee and an invoice number")
    if amount <= 0 or amount % 10_000:
        raise ValueError("the amount must be positive whole cents")
    if amount > MAX_AMOUNT:
        raise ValueError("the amount is over $1 billion")
    if due is not None:
        date.fromisoformat(due)
    kept, end = _account(account)
    same = ledger.db.execute(
        "select id from bills where lower(payee) = lower(?) and invoice = ?"
        f" and status not in {CLOSED}",
        (payee, invoice),
    ).fetchone()
    if same:
        ledger.audit("duplicate_invoice", ids.get("agent"), f"{payee} {invoice} is {same[0]}")
        raise Duplicate(f"{payee} invoice {invoice} is already entered as {same[0]}")

    bill_id = "bill_" + uuid.uuid4().hex[:16]
    asks, rule = [], payee_rule(payees, RAIL, payee)
    if rule == "ask":
        asks.append(f"{payee} is not on the invoice payee list")
    known = ledger.db.execute(
        "select account, account_end from payee_accounts where payee = ?", (payee.lower(),)
    ).fetchone()
    if not (known and known[0]):  # a payee with only tax details has no account yet
        asks.append(f"first payment to {payee}'s account ending {end}")
    elif known[0] != kept:
        asks.append(
            f"{payee}'s bank account changed from ending {known[1]} to ending {end};"
            " confirm it with a contact you already know before paying"
        )
        ledger.audit(
            "payee_account_changed", ids.get("agent"), f"{bill_id} {payee} {known[1]} -> {end}"
        )
    since = (datetime.now(UTC) - timedelta(days=SIMILAR_DAYS)).isoformat()
    similar = ledger.db.execute(
        "select id, invoice from bills where lower(payee) = lower(?) and amount = ?"
        f" and created_at >= ? and status not in {CLOSED}",
        (payee, amount, since),
    ).fetchone()
    if similar:
        asks.append(f"possible duplicate of {similar[0]} (invoice {similar[1]}, same amount)")
    refuse = screened = None
    if screen and rule != "deny":  # known: Quarter has paid this account before
        refuse, screened = screen.review(ledger, RAIL, payee, lambda: bool(known and known[0]))

    if rule == "deny":
        status, reason, ref = "refused", f"{payee} is not on the invoice payee list", None
    elif refuse:
        status, reason, ref = "refused", refuse, None
    else:
        decision, ref = ledger.hold(
            caps, ids, vendor(payee), RAIL, amount, ask="; ".join(asks) or None, sanctions=screened
        )
        status = {"allow": "approved", "approve": "waiting", "block": "refused"}[decision.outcome]
        reason = decision.reason
    try:
        with ledger._write():
            ledger.db.execute(
                "insert into bills (id, created_at, agent, task, customer, payee, invoice, amount,"
                " due, account, account_end, memo, status, reason, approval_id, call_id)"
                " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bill_id,
                    now(),
                    ids["agent"],
                    ids.get("task"),
                    ids.get("customer"),
                    payee,
                    invoice,
                    amount,
                    due,
                    kept,
                    end,
                    memo,
                    status,
                    reason,
                    ref if status == "waiting" else None,
                    ref if status == "approved" else None,
                ),
            )
    except sqlite3.IntegrityError:  # the same invoice, entered at the same moment
        if status == "approved":
            ledger.settle(ref, None)
        elif status == "waiting":
            ledger.decide_approval(ref, False, by="quarter")
        raise Duplicate(f"{payee} invoice {invoice} is already entered") from None
    ledger.audit("bill", ids["agent"], f"{bill_id} {payee} {invoice} {amount} {status}")
    b = bill(ledger, bill_id)
    if status == "approved":
        warn_if_no_w9(ledger, b, "approved", ids["agent"])
    return b


def ready(ledger: Ledger, caps: list[Cap], bill_id: str) -> dict:
    """A waiting bill, once its approval is decided: approved (held) or denied."""
    b = bill(ledger, bill_id)
    if b is None or b["status"] != "waiting":
        return b
    approval = ledger.approval(b["approval_id"])
    if approval and approval["status"] == "denied":
        status, call = "denied", None
    elif approval and approval["status"] == "approved":
        ids = {"agent": b["agent"], "task": b["task"], "customer": b["customer"]}
        decision, call = ledger.hold(
            caps, ids, vendor(b["payee"]), RAIL, b["amount"], b["approval_id"]
        )
        if decision.outcome != "allow":
            return b | {"reason": decision.reason}
        status = "approved"
    else:
        return b
    with ledger._write():
        taken = ledger.db.execute(
            "update bills set status = ?, call_id = ? where id = ? and status = 'waiting'",
            (status, call, bill_id),
        ).rowcount
    if not taken and call:  # another payer readied it first, with its own hold
        ledger.settle(call, None)
    b = bill(ledger, bill_id)
    if taken and status == "approved":
        warn_if_no_w9(ledger, b, "approved", approval["decided_by"])
    return b


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect would carry the token elsewhere


def mercury_send(b: dict, config: dict, method: str = "ach") -> str:
    """Send the payment from the customer's Mercury account. Returns Mercury's transaction
    id. The bill id is the idempotency key, so a retry never pays twice."""
    if method not in MERCURY_METHODS:
        raise ValueError(f"payment method must be one of {', '.join(MERCURY_METHODS)}")
    if not RECIPIENT.match(b["account"]):
        raise ValueError("paying by Mercury needs the bill's account to be a Mercury recipient id")
    body = {
        "recipientId": b["account"],
        "amount": to_cents(b["amount"]) / 100,
        "paymentMethod": method,
        "idempotencyKey": b["id"],
        "note": f"{b['payee']} invoice {b['invoice']} ({b['id']})",
        "externalMemo": f"Invoice {b['invoice']}",
    }
    if method == "domesticWire":
        body["purpose"] = {
            "simple": {"category": "vendor", "additionalInfo": f"Invoice {b['invoice']}"}
        }
    url = f"{config.get('url') or MERCURY_URL}/account/{config['account']}/transactions"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"authorization": f"Bearer {config['token']}", "content-type": "application/json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409: already sent with this key, and the reply is that payment
            raise ValueError(
                f"Mercury answered {e.code}: {e.read()[:300].decode(errors='replace')}"
            ) from None
        reply = json.loads(e.read())
    if reply.get("status") not in ("pending", "sent"):
        raise ValueError(f"Mercury did not send it: status {reply.get('status')}")
    return str(reply["id"])


def pay(
    ledger: Ledger,
    caps: list[Cap],
    bill_id: str,
    by: str,
    method: str,
    ref: str | None = None,
    mercury: dict | None = None,
    payment_method: str = "ach",
) -> dict:
    """Pay an approved bill: through Mercury, or record that a person paid it ("manual",
    with the bank's reference). The person paying cannot be the one who approved it."""
    by = by.strip()  # " Sam" is Sam: a space must not get round the two-person rule
    b = ready(ledger, caps, bill_id)
    if b is None:
        raise KeyError(f"no bill {bill_id}")
    if b["status"] != "approved":
        raise ValueError(
            f"{bill_id} is {b['status']}" + (f": {b['reason']}" if b.get("reason") else "")
        )
    if b["approval_id"]:
        approval = ledger.approval(b["approval_id"])
        if approval and (approval["decided_by"] or "").strip().lower() == by.lower():
            raise PermissionError(f"{by} approved this bill, so someone else must pay it")
    if method == "manual" and not ref:
        raise ValueError("a manual payment needs the bank's reference")
    if method not in ("manual", "mercury"):
        raise ValueError("method is mercury or manual")
    if method == "mercury" and not (mercury and mercury.get("token") and mercury.get("account")):
        raise ValueError("set QUARTER_MERCURY_TOKEN and QUARTER_MERCURY_ACCOUNT to pay by Mercury")
    with ledger._write():  # one payer at a time
        taken = ledger.db.execute(
            "update bills set status = 'paying' where id = ? and status = 'approved'", (bill_id,)
        ).rowcount
    if not taken:
        raise ValueError(f"{bill_id} is already being paid")
    try:
        if method == "mercury":
            ref = mercury_send(b, mercury, payment_method)
    except (ValueError, OSError) as e:
        with ledger._write():
            ledger.db.execute("update bills set status = 'approved' where id = ?", (bill_id,))
        ledger.audit("bill_payment_failed", by, f"{bill_id} {e}")
        raise ValueError(str(e)) from None
    ledger.settle(b["call_id"], b["amount"])
    with ledger._write():
        ledger.db.execute(
            "update bills set status = 'paid', paid_at = ?, paid_by = ?, method = ?, ref = ?"
            " where id = ?",
            (now(), by, method, ref, bill_id),
        )
        ledger.db.execute(
            "insert into payee_accounts (payee, account, account_end, at) values (?, ?, ?, ?)"
            " on conflict (payee) do update set account = excluded.account,"
            " account_end = excluded.account_end, at = excluded.at",
            (b["payee"].lower(), b["account"], b["account_end"], now()),
        )
    ledger.audit(
        "bill_paid", by, f"{bill_id} {b['payee']} {b['invoice']} {b['amount']} {method} {ref}"
    )
    warn_if_no_w9(ledger, b, "paid", by)
    return bill(ledger, bill_id)


def due_by(ledger: Ledger, day: str) -> list[str]:
    """Approved or waiting bills due on or before a day: the pay run."""
    date.fromisoformat(day)
    rows = ledger.db.execute(
        "select id from bills where status in ('approved', 'waiting') and due is not null"
        " and due <= ? order by due",
        (day,),
    )
    return [r[0] for r in rows]
