import json

from quarter_ledger.money import to_micros
from quarter_ledger.pricing import BUILT_IN, PriceBook, with_built_in


def test_built_in_prices_cover_the_default_vendors_and_are_dated():
    raw = json.loads(BUILT_IN.read_text())
    assert raw["_checked"] and set(raw["_sources"]) == {"openai", "anthropic", "gemini", "openrouter"}
    book = PriceBook.load(with_built_in(None))
    for vendor, model in [
        ("openai", "gpt-5"),
        ("anthropic", "claude-sonnet-4-5"),
        ("gemini", "gemini-2.5-pro"),
        ("openrouter", "openai/gpt-5"),
    ]:
        assert not book.price(vendor, model).is_flat


def test_customer_prices_override_and_extend_the_built_in_ones():
    raw = with_built_in(
        {
            "openai": {"gpt-5": {"input_per_million": "1", "output_per_million": "2"}},
            "serpapi": {"*": {"per_request": "0.015"}},
        }
    )
    book = PriceBook.load(raw)
    assert book.price("openai", "gpt-5").output_per_million == to_micros(2)
    assert not book.price("openai", "gpt-5-mini").is_flat
    assert book.price("serpapi", "any").per_request == to_micros("0.015")


def test_long_prompts_pay_the_long_context_price():
    price = PriceBook.load(
        {
            "v": {
                "m": {
                    "input_per_million": "1",
                    "output_per_million": "2",
                    "long_context": {
                        "above_tokens": 1000,
                        "input_per_million": "10",
                        "output_per_million": "20",
                    },
                }
            }
        }
    ).price("v", "m")
    assert price.cost(1000, 100) == 1000 + 200
    assert price.cost(1001, 100) == 10_010 + 2000


def test_ceiling_is_the_highest_known_price_including_long_prompts():
    book = PriceBook.load(
        {
            "v": {
                "a": {"input_per_million": "5", "output_per_million": "1"},
                "b": {
                    "input_per_million": "1",
                    "output_per_million": "3",
                    "long_context": {"above_tokens": 10, "input_per_million": "2", "output_per_million": "9"},
                },
            },
            "flat": {"*": {"per_request": "1"}},
        }
    )
    ceiling = book.ceiling("v")
    assert (ceiling.input_per_million, ceiling.output_per_million) == (to_micros(5), to_micros(9))
    assert book.ceiling("flat") == ceiling  # a vendor with no token prices borrows every vendor's
