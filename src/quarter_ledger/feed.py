"""The Quarter feed: prices and tool rules that Quarter signs and every gateway pulls.

Vendors change prices and agents find new ways to do damage faster than a release ships, so
a gateway with QUARTER_FEED (an https address) and QUARTER_FEED_KEY (the ed25519-pub key it
must be signed with) fetches the feed when it starts and once a day. The feed is
{"feed": {"version", "issued", "prices", "rules"}, "signature": hex}, signed over the body's
canonical JSON. A feed that does not verify, or is no newer than the one held, is ignored.
The last good one is kept beside the ledger, so a gateway that restarts offline still has it.

The customer's own settings always win: feed prices fill in models their price file does not
set, over the built-in ones, and feed rules come after their own rules. A feed may only hold
tool rules that ask a person (approve), never deny: Quarter can make a call wait, never stop
one the customer allowed. Its HTTP rules (version 2) apply to calls none of the customer's
rules matched. Each feed taken is audited with its version and hash, so the
hash-chained record shows which rules were in force when.
"""

import hashlib
import json
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from . import ed25519
from .pricing import BUILT_IN, PriceBook
from .rules import Rules, load_http_rules, load_rules

DAY = 86_400
MAX_BYTES = 1_000_000


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign_feed(body: dict, signing_key: str) -> dict:
    """The envelope Quarter publishes, signed with its ed25519: feed key."""
    if not signing_key.startswith("ed25519:"):
        raise ValueError("the feed is signed with an ed25519: key")
    checked(body)
    seed = bytes.fromhex(signing_key[8:])
    return {"feed": body, "signature": ed25519.sign(seed, canonical(body)).hex()}


def verified(envelope: dict, public: str) -> dict | None:
    """The feed body if the signature holds under the ed25519-pub key, else None."""
    try:
        body, signature = envelope["feed"], bytes.fromhex(envelope["signature"])
        key = bytes.fromhex(public.removeprefix("ed25519-pub:"))
        return body if isinstance(body, dict) and ed25519.verify(key, canonical(body), signature) else None
    except (KeyError, TypeError, ValueError):
        return None


def checked(body: dict) -> tuple[dict, Rules]:
    """The feed's prices and rules, or ValueError: a version, prices that load, and only tool
    rules that ask a person."""
    if not isinstance(body.get("version"), int) or body["version"] < 1:
        raise ValueError("a feed needs a whole version number from 1")
    prices = body.get("prices") or {}
    try:
        rules = load_rules(body.get("rules") or [])
    except TypeError as e:
        raise ValueError(f"feed rules: {e}") from None
    try:
        PriceBook.load(prices)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as e:
        raise ValueError(f"feed prices: {e!r}") from None
    if rules.calls or rules.defaults or rules.routes or rules.redactions:
        raise ValueError("a feed holds tool rules only")
    if any(r.action != "approve" for r in rules.tools):
        raise ValueError("feed rules may only ask a person (approve), never deny")
    rules.feed_calls = load_http_rules(body.get("http") or [])
    return prices, rules


class Feed:
    """Keeps a gateway's prices and rules current from the feed. own_prices and own_rules are
    the customer's, as configured; the feed is laid under and after them."""

    def __init__(self, gateway, url: str, public: str, own_prices: dict | None, own_rules: Rules):
        parts = urlsplit(url)
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError(f"the feed must use https: {url}")
        if not public.startswith("ed25519-pub:"):
            raise ValueError("QUARTER_FEED_KEY is the feed's ed25519-pub: key")
        self.gw, self.url, self.public = gateway, url, public
        self.own_prices, self.own_rules = own_prices or {}, own_rules
        self.version = 0
        db = gateway.ledger.path
        self.kept = None if db == ":memory:" else Path(db).with_suffix(".feed.json")
        self._stop = threading.Event()

    def start(self) -> None:
        """Take the kept feed now, then fetch in the background, now and once a day."""
        if self.kept and self.kept.exists():
            self.take(self.kept.read_bytes(), keep=False)

        def run():
            while not self._stop.is_set():
                self.fetch()
                self._stop.wait(DAY)

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def fetch(self) -> bool:
        try:
            with urllib.request.urlopen(self.url, timeout=10) as resp:
                raw = resp.read(MAX_BYTES + 1)
        except (OSError, ValueError) as e:
            self.gw.ledger.audit("feed_unreachable", None, f"{self.url}: {e}")
            return False
        return len(raw) <= MAX_BYTES and self.take(raw)

    def take(self, raw: bytes, keep: bool = True) -> bool:
        """Apply a fetched feed if it verifies and is newer. False otherwise."""
        try:
            body = verified(json.loads(raw), self.public)
            prices, rules = checked(body) if body else (None, None)
        except ValueError as e:
            self.gw.ledger.audit("feed_refused", None, f"{self.url}: {e}")
            return False
        if body is None:
            self.gw.ledger.audit("feed_refused", None, f"{self.url}: signature does not verify")
            return False
        if body["version"] <= self.version:
            return False
        self.gw.prices = PriceBook.load(self.prices(prices))
        tools = [replace(r, origin="feed rule") for r in rules.tools]
        self.gw.rules = replace(self.own_rules, tools=[*self.own_rules.tools, *tools], feed_calls=rules.feed_calls)
        self.version = body["version"]
        if keep and self.kept:
            spare = self.kept.with_suffix(".tmp")
            spare.write_bytes(raw)
            spare.replace(self.kept)
        detail = {"version": body["version"], "issued": body.get("issued"), "sha256": hashlib.sha256(raw).hexdigest(),
                  "prices": sum(len(m) for m in prices.values()), "rules": len(tools),
                  "http_rules": len(rules.feed_calls)}
        self.gw.ledger.audit("feed_applied", None, json.dumps(detail, sort_keys=True))
        return True

    def prices(self, feed: dict) -> dict:
        """Built-in, then the feed, then the customer's own, model by model."""
        built_in = json.loads(BUILT_IN.read_text())
        vendors = (built_in.keys() | feed.keys() | self.own_prices.keys())
        return {
            v: {**built_in.get(v, {}), **feed.get(v, {}), **self.own_prices.get(v, {})}
            for v in vendors if not v.startswith("_")
        }
