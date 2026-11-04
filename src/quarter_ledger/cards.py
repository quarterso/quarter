"""The company's own agent cards on Mercury and Ramp, checked against the ledger.

Quarter does not decide these swipes live (it does for Stripe Issuing). An agent asks
POST /authorize with rail "card" first, then pays with its card. Reconcile reads the card
feed with the company's own read-only credentials and matches each charge to the card
authorisation that allowed it. A charge with none is spend outside Quarter.

Matching, per charge, oldest first. Candidates are the same agent's card authorisations
made from 72 hours before the charge to 5 minutes after it (clocks differ), not yet taken
by another charge. A charge at or under the authorised amount, or over it by at most 5% or
$1 (whichever is more: currency conversion, a tip), is matched; the closest amount wins,
then the latest authorisation. A charge over that with a candidate left is over-authorised
and takes the latest one. Anything else is outside. One authorisation covers one charge.

Refunds and credits are listed, never matched or flagged: a refund is money coming back.
Charges that never moved money (Mercury reversed, cancelled, failed or blocked; Ramp
declined or errored) are left out.

Mercury, https://docs.mercury.com/reference/listtransactions (read 2026-11-03):
GET https://api.mercury.com/api/v1/transactions with "Authorization: Bearer <token>";
start and end on createdAt (YYYY-MM-DD or ISO 8601), limit up to 1000, order asc, and
start_after the previous reply's page.nextPage. Each transaction has id, amount (dollars,
multipleOf 0.01, sign not documented, so its size is used), kind (debitCardTransaction,
creditCardTransaction; debitCardCredit and creditCardCredit for refunds), status, createdAt,
counterpartyName and cardId (any card payment or refund, debit or credit). A Read Only
token needs no IP allow list: https://docs.mercury.com/reference/getting-started-with-your-api

Ramp, https://docs.ramp.com/developer-api/v1/api/transactions and its OpenAPI document
https://docs.ramp.com/openapi/developer-api.json (read 2026-11-03): a token from POST
https://api.ramp.com/developer/v1/token, Basic auth with client id and secret, body
grant_type=client_credentials&scope=transactions:read. GET /developer/v1/transactions with
from_date and to_date on user_transaction_time and page_size up to 100; page.next is the
whole URL of the next page. Each transaction has id, card_id, limit_id (and fund_id),
card_holder.user_id, merchant_name, state, user_transaction_time, entity_amount {currency,
value in cents, negative for refunds and credits} and the deprecated amount in dollars, and
original_transaction_id on a refund.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .bills import MERCURY_URL, _NoRedirect
from .money import MICROS, to_micros
from .store import Ledger, month_range

SOURCES = ("mercury", "ramp")
RAMP_URL = "https://api.ramp.com"
WINDOW = timedelta(hours=72)  # a charge comes at most this long after its authorisation
SKEW = timedelta(minutes=5)  # and may carry a time this much earlier than Quarter's
OVER_PCT = 5  # a charge may be this much over the authorised amount, for FX or a tip
OVER_MIN = MICROS  # or this much, whichever is more
MAX_PAGES = 200
MERCURY_CHARGES = {"debitCardTransaction", "creditCardTransaction"}
MERCURY_REFUNDS = {"debitCardCredit", "creditCardCredit"}
MERCURY_DEAD = {"reversed", "cancelled", "failed", "blocked"}
RAMP_DEAD = {"DECLINED", "ERROR", "PENDING_INITIATION"}


def read_cards(raw) -> dict[str, str]:
    """agent-cards.json: {"card id": "agent name"}. On Ramp a fund id or the agent's user
    id also names an agent, since Ramp may issue a new card for each purchase."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError('cards file is {"card id": "agent name"}')
    return {str(k).strip().lower(): str(agent) for k, agent in raw.items()}


def _get(url: str, headers: dict, secrets: list[str], data: bytes | None = None) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers)

    def hide(text: str) -> str:
        for s in secrets:
            text = text.replace(s, "***")
        return text

    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise ValueError(f"answered {e.code}: {hide(e.read()[:300].decode(errors='replace'))}") from None
    except (OSError, ValueError) as e:  # URLError, timeouts, a reply that is not JSON
        raise ValueError(f"could not be read: {hide(str(e))}") from None


def _pages(first: str, headers: dict, secrets: list[str], next_of):
    """Each page's reply, following next_of(reply, url) until it gives None."""
    url, seen = first, set()
    for _ in range(MAX_PAGES):
        reply = _get(url, headers, secrets)
        if not isinstance(reply, dict):
            raise ValueError("answered something other than a JSON object")  # noqa: TRY004
        yield reply
        url = next_of(reply, url)
        if url is None:
            return
        if url in seen:
            raise ValueError("returned the same page twice")
        seen.add(url)
    raise ValueError(f"had more than {MAX_PAGES} pages")


def _when(text) -> datetime:
    at = datetime.fromisoformat(str(text))
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def mercury(token: str, start: datetime, end: datetime, base: str | None = None) -> list[dict]:
    """Card charges and refunds on any card from start to end, from Mercury."""
    base = (base or MERCURY_URL).rstrip("/")
    query = {"start": start.date().isoformat(), "end": (end + timedelta(days=1)).date().isoformat(),
             "limit": 1000, "order": "asc"}
    first = f"{base}/transactions?{urllib.parse.urlencode(query)}"

    def next_of(reply, _):
        after = (reply.get("page") or {}).get("nextPage")
        if not after or not reply.get("transactions"):
            return None
        return f"{first}&{urllib.parse.urlencode({'start_after': after})}"

    out = []
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    for reply in _pages(first, headers, [token], next_of):
        for t in reply.get("transactions") or []:
            kind, status = t.get("kind"), str(t.get("status", "")).lower()
            if kind not in MERCURY_CHARGES | MERCURY_REFUNDS or status in MERCURY_DEAD:
                continue
            details = t.get("details") or {}
            card = t.get("cardId") or next(
                (i.get("id") for i in (details.get("debitCardInfo"), details.get("creditCardInfo"))
                 if isinstance(i, dict) and i.get("id")), None)
            try:
                amount = abs(to_micros(Decimal(str(t["amount"]))))
                at = _when(t["createdAt"])
            except (KeyError, ArithmeticError, ValueError):
                raise ValueError(f"answered a transaction without an amount or time: {t.get('id')}") from None
            out.append({"id": str(t.get("id")), "at": at, "amount": amount,
                        "refund": kind in MERCURY_REFUNDS, "keys": [str(card or "").lower()],
                        "merchant": t.get("counterpartyName") or "", "pending": status == "pending",
                        "original": None})
    return out


def ramp_token(client_id: str, secret: str, base: str | None = None) -> str:
    base = (base or RAMP_URL).rstrip("/")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    reply = _get(f"{base}/developer/v1/token",
                 {"authorization": f"Basic {basic}",
                  "content-type": "application/x-www-form-urlencoded"},
                 [secret, basic],
                 urllib.parse.urlencode({"grant_type": "client_credentials",
                                         "scope": "transactions:read"}).encode())
    token = reply.get("access_token") if isinstance(reply, dict) else None
    if not token:
        raise ValueError("gave no access token")
    return token


def ramp(token: str, start: datetime, end: datetime, base: str | None = None) -> list[dict]:
    """Card charges and refunds on any card from start to end, from Ramp."""
    base = (base or RAMP_URL).rstrip("/")
    query = {"from_date": start.isoformat(), "to_date": end.isoformat(), "page_size": 100}
    host = urllib.parse.urlsplit(base).netloc

    def next_of(reply, _):
        url = (reply.get("page") or {}).get("next")
        if not url or not reply.get("data"):
            return None
        if urllib.parse.urlsplit(url).netloc != host:  # the token goes only where it was made for
            raise ValueError("gave a next page on another host")
        return url

    out = []
    first = f"{base}/developer/v1/transactions?{urllib.parse.urlencode(query)}"
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    for reply in _pages(first, headers, [token], next_of):
        for t in reply.get("data") or []:
            if str(t.get("state", "")).upper() in RAMP_DEAD:
                continue
            money = t.get("entity_amount") or {}
            try:
                if money:
                    if str(money.get("currency", "USD")).upper() != "USD":
                        raise ValueError(f"answered in {money.get('currency')}; only USD is compared")
                    signed = Decimal(int(money["value"])) / 100
                else:
                    signed = Decimal(str(t["amount"]))
                at = _when(t["user_transaction_time"])
            except (KeyError, TypeError, ArithmeticError) as e:
                raise ValueError(f"answered a transaction without an amount or time: {t.get('id')}") from e
            holder = t.get("card_holder") or {}
            keys = [str(k).lower() for k in (t.get("card_id"), t.get("fund_id") or t.get("limit_id"),
                                             holder.get("user_id")) if k]
            original = t.get("original_transaction_id")
            out.append({"id": str(t.get("id")), "at": at, "amount": abs(to_micros(signed)),
                        "refund": signed < 0 or bool(original), "keys": keys,
                        "merchant": t.get("merchant_name") or t.get("merchant_descriptor") or "",
                        "pending": str(t.get("state", "")).upper() == "PENDING", "original": original})
    return out


def authorisations(ledger: Ledger, start: datetime, end: datetime) -> list[dict]:
    """Card authorisations from POST /authorize from start to end, with who approved each.
    Stripe Issuing swipes Quarter already settled are left out: those are its own cards."""
    issuing = {d.split(" ", 1)[0] for (d,) in ledger.db.execute(
        "select detail from audit where action = 'card_settled'")}
    standing = {}
    for (d,) in ledger.db.execute("select detail from audit where action = 'standing_used'"):
        rule, call_id, *_ = d.split(" ")
        standing[call_id] = rule
    rows = ledger.db.execute(
        "select h.call_id, h.at, h.agent, h.task, h.customer, h.vendor, h.amount,"
        " (select amount from entries c where c.call_id = h.call_id and c.kind = 'charge'),"
        " exists (select 1 from entries r where r.call_id = h.call_id and r.kind = 'release'),"
        " (select decided_by from approvals a where a.call_id = h.call_id)"
        " from entries h where h.kind = 'hold' and h.model = 'card' and h.at >= ? and h.at < ?"
        " order by h.at",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out = []
    for call_id, at, agent, task, customer, vendor, held, charged, closed, approver in rows:
        if call_id in issuing:
            continue
        if approver is None and call_id in standing:
            by = ledger.db.execute("select created_by from standing where id = ?",
                                   (standing[call_id],)).fetchone()
            approver = f"{by[0]} (standing {standing[call_id]})" if by else None
        out.append({"call_id": call_id, "at": datetime.fromisoformat(at), "agent": agent,
                    "task": task, "customer": customer, "payee": vendor.split(":", 1)[1],
                    "amount": held, "open": not closed, "charged": charged, "approver": approver})
    return out


def allowed(held: int) -> int:
    """The most a charge may be and still match an authorisation of `held`."""
    return held + max(OVER_MIN, held * OVER_PCT // 100)


def reconcile(ledger: Ledger, cards: dict[str, str], month: str, fetch) -> dict:
    """fetch(start, end) gives the source's card transactions. Charges from WINDOW before
    the month are matched too, so authorisations they took are not offered again."""
    start, end = month_range(month)
    feed = fetch(start - WINDOW, end)
    auths = authorisations(ledger, start - 2 * WINDOW, end + SKEW)
    unclaimed = list(auths)
    matched, over, outside, refunds = [], [], [], []
    for c in sorted(feed, key=lambda c: c["at"]):
        agent = next((cards[k] for k in c["keys"] if k in cards), None)
        if agent is None:
            continue
        c = c | {"agent": agent}
        here = start <= c["at"] < end
        if c["refund"]:
            if here:
                refunds.append(c)
            continue
        near = [a for a in unclaimed if a["agent"] == agent
                and c["at"] - WINDOW <= a["at"] <= c["at"] + SKEW]
        fits = [a for a in near if c["amount"] <= allowed(a["amount"])]
        if fits:
            a = min(fits, key=lambda a: (abs(a["amount"] - c["amount"]), c["at"] - a["at"]))
            unclaimed.remove(a)
            if here:
                matched.append((c, a))
        elif near:
            a = max(near, key=lambda a: a["at"])
            unclaimed.remove(a)
            if here:
                over.append((c, a))
        elif here:
            outside.append(c)
    never = [a for a in unclaimed if start <= a["at"] < end and a["agent"] in set(cards.values())]
    charges = [c for c in feed if start <= c["at"] < end and not c["refund"]
               and any(k in cards for k in c["keys"])]
    return {"month": month, "charges": len(charges), "matched": matched, "over": over,
            "outside": outside, "refunds": refunds, "never_charged": never}


def settle(ledger: Ledger, matched: list, source: str) -> int:
    """Record each matched charge whose authorisation is still open as its settlement, at
    the charged amount, in the audit chain as /settle would. Returns how many."""
    done = 0
    for c, a in matched:
        if not a["open"] or c["pending"]:
            continue
        try:
            ledger.settle(a["call_id"], c["amount"])
        except KeyError:
            continue  # settled since it was read
        detail = f"{a['call_id']} card {c['amount']} tx={source}:{c['id']}"
        ledger.audit("payment_settled", "cards reconcile", detail)
        if c["amount"] > a["amount"]:
            ledger.audit("over_authorised", "cards reconcile", detail)
        done += 1
    return done
