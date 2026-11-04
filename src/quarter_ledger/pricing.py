"""Turns a vendor's reported usage into a cost.

List prices ship in prices.json, dated and with their sources. The customer's own price file
extends or overrides them, model by model:
{
  "openai":  {"gpt-5": {"input_per_million": "1.25", "output_per_million": "10"}},
  "serpapi": {"*": {"per_request": "0.015"}}
}
Token prices charge reported usage. Per-request prices charge a flat amount per call, for
search, scraping and similar APIs. "*" covers any model of that vendor. A price that rises
with prompt length adds "long_context": {"above_tokens", "input_per_million",
"output_per_million"}.

Prompt-cache tokens have their own prices where the vendor publishes them:
"cache_read_per_million", "cache_write_per_million" (Anthropic's 5-minute and OpenAI's
30-minute writes) and "cache_write_extended_per_million" (1-hour and 12-hour writes). A
cache price that is missing falls back to the input price, so nothing is undercounted.

A model with no price is never treated as free: ceiling() prices it at the vendor's highest
known input and output prices.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .money import to_micros

BUILT_IN = Path(__file__).with_name("prices.json")


class UnknownPrice(Exception):
    pass


@dataclass(frozen=True)
class Price:
    input_per_million: int = 0
    output_per_million: int = 0
    per_request: int = 0
    cache_read_per_million: int | None = None
    cache_write_per_million: int | None = None
    cache_write_extended_per_million: int | None = None
    long_above: int | None = None  # prompt tokens above which `long` applies
    long: "Price | None" = None

    @property
    def is_flat(self) -> bool:
        return self.input_per_million == 0 and self.output_per_million == 0

    def cost(self, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
             cache_write: int = 0, cache_write_extended: int = 0) -> int:
        """input_tokens is the whole prompt, cached tokens included; cache_read and the
        cache writes say how many of those tokens the vendor read from or wrote to a cache."""
        if self.long and input_tokens > self.long_above:
            return self.long.cost(
                input_tokens, output_tokens, cache_read, cache_write, cache_write_extended
            )
        uncached = max(0, input_tokens - cache_read - cache_write - cache_write_extended)
        tokens = (
            uncached * self.input_per_million
            + cache_read * self.cache_read
            + cache_write * self.cache_write
            + cache_write_extended * self.cache_write_extended
            + output_tokens * self.output_per_million
        )
        return self.per_request + tokens // 1_000_000

    @property
    def cache_read(self) -> int:
        return _or(self.cache_read_per_million, self.input_per_million)

    @property
    def cache_write(self) -> int:
        return _or(self.cache_write_per_million, self.input_per_million)

    @property
    def cache_write_extended(self) -> int:
        return _or(self.cache_write_extended_per_million, self.cache_write)


def _or(price: int | None, fallback: int) -> int:
    return fallback if price is None else price


def _micros_or_none(value) -> int | None:
    return None if value is None else to_micros(value)


def _price(p: dict) -> Price:
    long = p.get("long_context")
    return Price(
        input_per_million=to_micros(p.get("input_per_million", 0)),
        output_per_million=to_micros(p.get("output_per_million", 0)),
        per_request=to_micros(p.get("per_request", 0)),
        cache_read_per_million=_micros_or_none(p.get("cache_read_per_million")),
        cache_write_per_million=_micros_or_none(p.get("cache_write_per_million")),
        cache_write_extended_per_million=_micros_or_none(
            p.get("cache_write_extended_per_million")
        ),
        long_above=int(long["above_tokens"]) if long else None,
        long=_price(long) if long else None,
    )


def with_built_in(raw: dict | None) -> dict:
    """The built-in prices with the customer's laid over them, model by model."""
    built_in = json.loads(BUILT_IN.read_text())
    raw = raw or {}
    return {
        vendor: {**built_in.get(vendor, {}), **raw.get(vendor, {})}
        for vendor in built_in.keys() | raw.keys()
        if not vendor.startswith("_")
    }


class PriceBook:
    def __init__(self, prices: dict[str, dict[str, Price]]):
        self._prices = prices

    @classmethod
    def load(cls, raw: dict) -> "PriceBook":
        return cls(
            {
                vendor: {m: _price(p) for m, p in models.items()}
                for vendor, models in raw.items()
                if not vendor.startswith("_")
            }
        )

    def price(self, vendor: str, model: str) -> Price:
        models = self._prices.get(vendor, {})
        found = models.get(model) or models.get("*")
        if found is None:
            raise UnknownPrice(f"no price for {vendor}/{model or '*'}")
        return found

    def knows(self, vendor: str) -> bool:
        """Whether any price is set for the vendor: else it is an API, not a model."""
        return bool(self._prices.get(vendor))

    def ceiling(self, vendor: str) -> Price:
        """The highest input, output and cache-write prices known for the vendor, or for any
        vendor. Cache reads take the input price."""
        known = [p for p in self._prices.get(vendor, {}).values() if not p.is_flat] or [
            p for models in self._prices.values() for p in models.values() if not p.is_flat
        ]
        tiers = known + [p.long for p in known if p.long]
        return Price(
            input_per_million=max((p.input_per_million for p in tiers), default=0),
            output_per_million=max((p.output_per_million for p in tiers), default=0),
            cache_write_per_million=max((p.cache_write for p in tiers), default=0),
            cache_write_extended_per_million=max(
                (p.cache_write_extended for p in tiers), default=0
            ),
        )
