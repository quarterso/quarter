"""Payees screened against the US Treasury's OFAC sanctions lists before a payment is allowed.

`quarter-ledger sanctions update` downloads the SDN list and the Consolidated (non-SDN)
list from OFAC's Sanctions List Service into a folder beside the ledger, with each list's
publish date and SHA-256. The gateway reads that copy and never fetches while it decides.

A name is compared after folding case, accents, punctuation and business suffixes, in any
word order. A wallet address the SDN list names, or an exact match on an SDN entity's
name, is refused. Every other match waits for a person who sees the entry: a person's name
alone is not proof (Hassan Ali is on the SDN list and is a common name), nor is a close
match or the Consolidated list, whose programs mostly restrict rather than block. Weak
aliases are left out, as OFAC's FAQ 124 allows.

A named person who approves a payment held only for a close match clears that payee for
that entry. It is not held for it again until the entry changes or leaves the list.

This is a screening aid, not a compliance program: see the README for what it misses.
"""

import hashlib
import json
import os
import re
import threading
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from .store import dicts

SERVICE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports"
LISTS = {"sdn": f"{SERVICE}/SDN.XML", "consolidated": f"{SERVICE}/CONSOLIDATED.XML"}
STALE_DAYS = 7  # QUARTER_SANCTIONS_STALE_DAYS: older than this, new payees wait for a person
CLOSE = 0.9  # token-sorted similarity from which a name waits for a person
SCREENED = {"Individual", "Entity"}  # vessels and aircraft are not paid
SUFFIXES = {
    "ag", "bv", "co", "company", "corp", "corporation", "gmbh", "inc", "incorporated", "jsc",
    "kg", "limited", "llc", "llp", "lp", "ltd", "nv", "oao", "ojsc", "ooo", "pjsc", "plc",
    "pte", "pty", "sa", "sarl", "spa", "srl", "the", "zao",
}


def words(name: str) -> list[str]:
    """A name as sorted words: no accents, case, punctuation or business suffix."""
    plain = unicodedata.normalize("NFKD", name)
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower().replace("&", " and ")
    plain = re.sub(r"(?<=\b[a-z])\.(?=[a-z]\b)", "", plain)  # s.a. -> sa, l.l.c -> llc
    return sorted(w for w in re.split(r"[^a-z0-9]+", plain) if w and w not in SUFFIXES)


def address_key(text: str) -> str:
    """Hex addresses compare in any case; base58 and others exactly."""
    text = text.strip()
    return text.lower() if text.lower().startswith("0x") else text


@dataclass(frozen=True)
class Match:
    outcome: str  # "clear", "match" (an exact name or address) or "close"
    list: str = ""
    type: str = ""  # Individual or Entity
    uid: str = ""
    name: str = ""  # the entry's primary name
    matched: str = ""  # the name or address that matched
    programs: str = ""
    score: float = 0.0
    entry: str = ""  # the entry's fingerprint: it changes when OFAC changes the entry

    @property
    def refused(self) -> bool:
        """An SDN wallet address or entity name: refused. Anything else waits for a person."""
        return self.outcome == "match" and self.list == "sdn" and self.type != "Individual"

    def describe(self) -> str:
        what = "OFAC SDN list" if self.list == "sdn" else "OFAC Consolidated list"
        also = f" as {self.matched}" if self.matched != self.name else ""
        return f"{what} entry {self.uid} {self.name}{also} ({self.programs}), score {self.score:.2f}"


def fingerprint(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:16]


def _text(el, tag: str) -> str:
    found = el.find(tag)
    return (found.text or "").strip() if found is not None else ""


def parse(raw: bytes, which: str) -> tuple[str, int, list[dict]]:
    """An OFAC XML list (SDN.XML or CONSOLIDATED.XML): its publish date (YYYY-MM-DD), the
    record count it states, and its entries."""
    root = ET.fromstring(raw)
    for el in root.iter():  # drop the namespace, so tags read as in OFAC's schema
        el.tag = el.tag.rpartition("}")[2]
    info = root.find("publshInformation")  # OFAC's spelling
    if info is None or not _text(info, "Publish_Date"):
        raise ValueError(f"{which}: not an OFAC list (no Publish_Date)")
    month, day, year = _text(info, "Publish_Date").split("/")
    entries = []
    for e in root.iter("sdnEntry"):
        names = [" ".join(p for p in (_text(e, "firstName"), _text(e, "lastName")) if p)]
        names += [
            " ".join(p for p in (_text(a, "firstName"), _text(a, "lastName")) if p)
            for a in e.iter("aka")
            if _text(a, "category") != "weak"
        ]
        entries.append({
            "list": which,
            "uid": _text(e, "uid"),
            "type": _text(e, "sdnType"),
            "names": [n for n in names if n],
            "programs": ", ".join(p.text or "" for p in e.iter("program")),
            "addresses": [
                _text(i, "idNumber") for i in e.iter("id")
                if _text(i, "idType").startswith("Digital Currency Address")
            ],
        })
    return f"{year}-{month}-{day}", int(_text(info, "Record_Count") or -1), entries


def update(folder: str, opener=None) -> dict:
    """Download every list, check each parses and holds the records it says, then keep
    it with its publish date and hash in lists.json. Nothing is replaced unless all are."""
    lists, raws, opener = {}, {}, opener or urllib.request.urlopen
    for which, url in LISTS.items():
        with opener(url, timeout=120) as resp:
            raw = resp.read()
        published, stated, entries = parse(raw, which)
        if stated != len(entries):
            raise ValueError(f"{which}: says {stated} records, holds {len(entries)}")
        raws[which] = raw
        lists[which] = {
            "url": url, "file": f"{which}.xml", "published": published, "records": len(entries),
            "sha256": hashlib.sha256(raw).hexdigest(), "fetched": datetime.now(UTC).isoformat(),
        }
    Path(folder).mkdir(parents=True, exist_ok=True)
    for which, raw in raws.items():
        staged = Path(folder, f"{which}.xml.tmp")
        staged.write_bytes(raw)
        os.replace(staged, Path(folder, f"{which}.xml"))
    staged = Path(folder, "lists.json.tmp")
    staged.write_text(json.dumps(lists, indent=2) + "\n")
    os.replace(staged, Path(folder, "lists.json"))
    return lists


class Screen:
    """The stored lists, read again whenever `sanctions update` replaces them."""

    def __init__(self, folder: str, stale_days: int = STALE_DAYS):
        self.folder, self.stale_days = folder, stale_days
        self.lists: dict = {}
        self._stamp = None
        # (exact name: [(entry, name)], word: {name index}, [(name, entry, as listed)], address: entry),
        # replaced whole so a payment screened during a reload reads one version
        self._index: tuple[dict, dict, list, dict] = ({}, {}, [], {})
        self._lock = threading.Lock()

    def _load(self) -> tuple[dict, dict, list, dict]:
        meta = Path(self.folder, "lists.json")
        try:
            stamp = meta.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        with self._lock:
            if stamp == self._stamp:
                return self._index
            lists = json.loads(meta.read_text()) if stamp else {}
            exact, by_word, names, addresses = {}, {}, [], {}
            for which, info in lists.items():
                for e in parse(Path(self.folder, info["file"]).read_bytes(), which)[2]:
                    for a in e["addresses"]:
                        addresses[address_key(a)] = e
                    if e["type"] not in SCREENED:
                        continue
                    for name in e["names"]:
                        key = " ".join(words(name))
                        if not key:
                            continue
                        exact.setdefault(key, []).append((e, name))
                        for w in set(key.split()):
                            by_word.setdefault(w, set()).add(len(names))
                        names.append((key, e, name))
            self.lists, self._index, self._stamp = lists, (exact, by_word, names, addresses), stamp
            return self._index

    def stored(self) -> dict:
        self._load()
        return self.lists

    def age_days(self, now: datetime | None = None) -> int | None:
        """Days since the oldest list was last downloaded; None with no list stored."""
        if not self.stored():
            return None
        fetched = min(datetime.fromisoformat(i["fetched"]) for i in self.lists.values())
        return ((now or datetime.now(UTC)) - fetched).days

    def stale(self, now: datetime | None = None) -> bool:
        age = self.age_days(now)
        return age is not None and age > self.stale_days

    def check(self, payee: str) -> Match:
        """The strongest match for a payee's name or wallet address."""
        exact, by_word, names, addresses = self._load()
        e = addresses.get(address_key(payee))
        if e:
            return Match("match", e["list"], "address", e["uid"], e["names"][0], payee.strip(),
                         e["programs"], 1.0, fingerprint(e))
        key = " ".join(words(payee))
        if not key:
            return Match("clear")
        if key in exact:  # SDN before Consolidated, an entity before a person
            e, name = min(exact[key], key=lambda m: (m[0]["list"] != "sdn", m[0]["type"] == "Individual"))
            return Match("match", e["list"], e["type"], e["uid"], e["names"][0], name, e["programs"], 1.0,
                         fingerprint(e))
        best, best_score = None, CLOSE
        for i in set().union(*(by_word.get(w, ()) for w in key.split())):
            other, e, name = names[i]
            if 2 * min(len(key), len(other)) / (len(key) + len(other)) < best_score:
                continue  # too different in length to reach the line
            score = SequenceMatcher(None, key, other, autojunk=False).ratio()
            if score >= best_score:
                best, best_score = (e, name), score
        if best is None:
            return Match("clear")
        e, name = best
        return Match("close", e["list"], e["type"], e["uid"], e["names"][0], name, e["programs"],
                     round(best_score, 3), fingerprint(e))

    def review(self, ledger, rail: str, payee: str, known, approval: dict | None = None
               ) -> tuple[str | None, str | None]:
        """(refuse, ask) for one payment: a reason to refuse it, or one a person must clear.
        Writes the screen to the audit log either way. known() says whether Quarter has
        paid this payee before or it is on the payee list; asked only of a stale list.
        approval is the one the payment carries, decided by a named person."""
        lists = self.stored()
        if not lists:
            return None, None
        m = self.check(payee)
        stale = self.stale()
        refuse = ask = None
        held = f"{payee} may be sanctioned: {m.describe()}; check before paying"
        cleared = m.outcome == "close" and self._cleared(ledger, rail, payee, m, held, approval)
        if m.refused:
            refuse = f"{payee} matches the {m.describe()}"
        elif m.outcome != "clear" and not cleared:
            ask = held
        elif stale and not known() and not cleared:
            ask = (f"the OFAC lists were last downloaded {self.age_days()} days ago and {payee}"
                   " is a new payee; check it, then run quarter-ledger sanctions update")
        ledger.audit("sanctions_screen", None, json.dumps({
            "rail": rail, "payee": payee,
            "outcome": "refused" if refuse else "held" if ask else "cleared" if cleared else "clear",
            "match": m.outcome, "list": m.list, "uid": m.uid, "name": m.matched, "score": m.score,
            "published": {w: i["published"] for w, i in lists.items()},
            "sha256": {w: i["sha256"][:16] for w, i in lists.items()}, "stale": stale,
        }, sort_keys=True))
        return refuse, ask

    def _cleared(self, ledger, rail: str, payee: str, m: Match, held: str, approval: dict | None) -> bool:
        """Whether a person cleared this payee of this close match: before, for the entry as
        it stands, or now, by approving a payment held for this match alone (held)."""
        key = " ".join(words(payee))
        row = ledger.db.execute(
            "select entry from sanctions_cleared where payee = ? and list = ? and uid = ?", (key, m.list, m.uid)
        ).fetchone()
        if row and row[0] == m.entry:
            return True
        if not (approval and approval["status"] == "approved" and approval["decided_by"]
                and approval["vendor"] == f"{rail}:{payee}"
                and approval["reason"] == held):
            return False
        with ledger._write():
            ledger.db.execute(
                "insert or replace into sanctions_cleared (payee, list, uid, entry, cleared_at, cleared_by,"
                " approval_id) values (?, ?, ?, ?, ?, ?, ?)",
                (key, m.list, m.uid, m.entry, datetime.now(UTC).isoformat(), approval["decided_by"], approval["id"]),
            )
            ledger._audit_row("sanctions_cleared", approval["decided_by"], json.dumps({
                "payee": key, "rail": rail, "list": m.list, "uid": m.uid, "name": m.matched, "score": m.score,
                "entry": m.entry, "approval": approval["id"],
            }, sort_keys=True))
        return True

    def expire(self, ledger, by: str) -> list[dict]:
        """Forget each cleared payee whose entry changed or left the lists, so that a new
        listing is held again. Run after every update."""
        _, _, names, _ = self._load()
        listed = {(e["list"], e["uid"]): fingerprint(e) for _, e, _ in names}
        ended = [c for c in cleared(ledger) if listed.get((c["list"], c["uid"])) != c["entry"]]
        for c in ended:
            why = "changed" if (c["list"], c["uid"]) in listed else "removed"
            _forget(ledger, c, by, f"entry {why}")
        return ended

    def summary(self, now: datetime | None = None) -> str:
        """One line for status: which lists, how old, and whether payees are screened."""
        lists = self.stored()
        if not lists:
            return "Sanctions: no OFAC list, payees not screened (quarter-ledger sanctions update)"
        dates = ", ".join(f"{w} {i['published']}" for w, i in lists.items())
        line = f"Sanctions: OFAC {dates}, fetched {self.age_days(now)} day(s) ago"
        if self.stale(now):
            line += f"\n  STALE (over {self.stale_days} days): new payees wait for a person"
        return line


def cleared(ledger) -> list[dict]:
    """Every payee cleared of a close match, newest first."""
    return dicts(ledger.db.execute(
        "select payee, list, uid, entry, cleared_at, cleared_by, approval_id from sanctions_cleared"
        " order by cleared_at desc"
    ))


def forget(ledger, payee: str, by: str) -> list[dict]:
    """Forget a payee's clearings: its next payment is held for a person again."""
    key = " ".join(words(payee))
    gone = [c for c in cleared(ledger) if c["payee"] == key]
    for c in gone:
        _forget(ledger, c, by, "forgotten")
    return gone


def _forget(ledger, c: dict, by: str, why: str) -> None:
    with ledger._write():
        ledger.db.execute("delete from sanctions_cleared where payee = ? and list = ? and uid = ?",
                          (c["payee"], c["list"], c["uid"]))
        ledger._audit_row("sanctions_uncleared", by, json.dumps(
            {"payee": c["payee"], "list": c["list"], "uid": c["uid"], "reason": why}, sort_keys=True))
