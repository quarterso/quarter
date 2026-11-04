"""Year-end 1099 payee tracking for bill pay.

A payer sends Form 1099-NEC or 1099-MISC to each US payee it paid enough in a calendar
year, and needs the payee's Form W-9 (a foreign payee gives a W-8) before paying, or the
payment may be subject to backup withholding. Quarter keeps, on the payee's record, when
the form arrived, the entity type on it, and the TIN only as a keyed fingerprint and its
last four digits. The full TIN is never stored, logged or printed. The fingerprint is
keyed because a nine-digit number with its last four known is only 100,000 guesses.

Thresholds, checked on irs.gov on 2026-11-03:
- Payments made in 2025 and earlier: $600 for nonemployee compensation, rents, other
  income and medical payments.
- Payments made after 31 December 2025: $2,000 (One Big Beautiful Bill Act section 70433),
  indexed for inflation from calendar year 2027. Publication 1099 (2026), "For tax years
  beginning after 2025": https://www.irs.gov/publications/p1099 and Rev. Proc. 2025-32,
  https://www.irs.gov/pub/irs-drop/rp-25-32.pdf
- The 2027 indexed figure was not published when this was written. Indexing only raises
  it, so $2,000 is used for 2027 on: it reports at least everyone who must be reported.
- Corporations are generally exempt, except attorneys' fees and medical and health care
  payments. Gross proceeds paid to an attorney keep a $600 threshold. Payments by card or
  a third party network are reported by the network on Form 1099-K, not by the payer.
  Instructions for Forms 1099-MISC and 1099-NEC: https://www.irs.gov/instructions/i1099mec

Bills are paid by bank (ACH, check, wire, real-time payment) through Mercury, or recorded
as a manual bank payment, so no bill is a card payment. Spend on Quarter cards is not a
bill and is not in this report.
"""

import hashlib
import hmac
import re
from datetime import date

from .store import Ledger, now

ENTITIES = {
    "individual": "individual or sole proprietor",
    "partnership": "partnership",
    "llc-partnership": "LLC taxed as a partnership",
    "c-corp": "C corporation",
    "s-corp": "S corporation",
    "other": "other",
    "foreign": "foreign, with a W-8",
}
CORPORATIONS = ("c-corp", "s-corp")
PAYS_FOR = ("services", "goods", "attorney", "medical")
ATTORNEY_PROCEEDS = 600_000_000  # micros: gross proceeds to an attorney, 1099-MISC box 10
NOTE = (
    "Paid bills only, by payment date (UTC). Card payments are reported by the card network"
    " on Form 1099-K: no bill is paid by card, and Quarter card spend is not in this report."
)


def threshold(year: int) -> int:
    """The 1099-NEC and 1099-MISC threshold for payments made in a year, in micros."""
    return 600_000_000 if year <= 2025 else 2_000_000_000


def fingerprint(tin: str, key: bytes) -> tuple[str, str]:
    digits = re.sub(r"[\s-]", "", tin)
    if not re.fullmatch(r"\d{9}", digits):
        raise ValueError("a TIN is nine digits")
    mac = hmac.new(key, digits.encode(), hashlib.sha256).hexdigest()[:24]
    return "hmac-sha256:" + mac, digits[-4:]


def record(
    ledger: Ledger,
    payee: str,
    form_date: str,
    entity: str,
    key: bytes,
    tin: str | None = None,
    pays_for: str | None = None,
    by: str | None = None,
) -> dict:
    """Record a payee's W-9 (or W-8 for a foreign payee) on its payee record."""
    payee = payee.strip()
    if not payee:
        raise ValueError("name the payee")
    date.fromisoformat(form_date)
    if entity not in ENTITIES:
        raise ValueError(f"entity must be one of {', '.join(ENTITIES)}")
    if pays_for is not None and pays_for not in PAYS_FOR:
        raise ValueError(f"pays-for must be one of {', '.join(PAYS_FOR)}")
    if tin:
        kept, end = fingerprint(tin, key)
    elif entity == "foreign":
        kept, end = None, None
    else:
        raise ValueError("a W-9 has the payee's TIN")
    with ledger._write():
        ledger.db.execute(
            "insert into payee_accounts (payee, account, account_end, at, w9_at, entity, tin,"
            " tin_end, pays_for) values (?, '', '', ?, ?, ?, ?, ?, ?) on conflict (payee) do update"
            " set w9_at = excluded.w9_at, entity = excluded.entity, tin = excluded.tin,"
            " tin_end = excluded.tin_end, pays_for = excluded.pays_for",
            (payee.lower(), now(), form_date, entity, kept, end, pays_for),
        )
        ledger._audit_row(
            "payee_tax",
            by,
            f"{payee} {entity} form {form_date} tin {'on file' if kept else 'none'}"
            + (f" pays for {pays_for}" if pays_for else ""),
        )
    return details(ledger, payee)


def details(ledger: Ledger, payee: str) -> dict | None:
    row = ledger.db.execute(
        "select w9_at, entity, tin_end, pays_for from payee_accounts where payee = ?",
        (payee.lower(),),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return dict(zip(("w9_at", "entity", "tin_end", "pays_for"), row, strict=True))


def warn_if_no_w9(ledger: Ledger, b: dict, event: str, by: str | None) -> None:
    """Warn, never block: whether to pay without a W-9 is the customer's policy."""
    if details(ledger, b["payee"]) is None:
        ledger.audit(
            "w9_missing",
            by,
            f"{b['id']} {b['payee']} {event} with no W-9 on file; backup withholding may apply",
        )


def _reportable(total: int, year: int, entity: str | None, pays_for: str | None):
    if pays_for == "goods":
        return "no", "payments for goods are not reported"
    if total < threshold(year):
        if pays_for == "attorney" and total >= ATTORNEY_PROCEEDS:
            return "check", "gross proceeds of $600 or more to an attorney are reported"
        return "no", "under the threshold"
    if entity is None:
        return "check", "no W-9: entity type unknown"
    if entity == "foreign":
        return "check", "foreign payee: no 1099, but Form 1042-S may apply"
    if entity == "other":
        return "check", "entity type other"
    if entity in CORPORATIONS:
        if pays_for in ("attorney", "medical"):
            return "yes", f"{pays_for} payments to a corporation are reported"
        if pays_for is None:
            return "check", "corporation: exempt unless attorney or medical payments"
        return "no", "corporation"
    return "yes", "" if pays_for else "if for services, rents or other reportable payments"


def report(ledger: Ledger, year: int) -> list[dict]:
    """Every payee paid in a calendar year: total paid, whether it is reportable on a 1099
    (yes, no or check), and whether the W-9 is missing."""
    rows = ledger.db.execute(  # with max(), sqlite takes b.payee from the latest payment
        "select max(b.paid_at), b.payee, sum(b.amount), count(*), p.w9_at, p.entity,"
        " p.tin_end, p.pays_for from bills b left join payee_accounts p"
        " on p.payee = lower(b.payee) where b.status = 'paid' and b.paid_at >= ?"
        " and b.paid_at < ? group by lower(b.payee) order by sum(b.amount) desc",
        (f"{year:04d}-01-01", f"{year + 1:04d}-01-01"),
    ).fetchall()
    out = []
    for _, name, total, count, w9_at, entity, tin_end, pays_for in rows:
        reportable, why = _reportable(total, year, entity if w9_at else None, pays_for)
        out.append({
            "payee": name, "total": total, "bills": count, "entity": entity if w9_at else None,
            "tin_end": tin_end, "w9_missing": w9_at is None, "reportable": reportable,
            "why": why,
        })
    return out
