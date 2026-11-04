"""Amounts are integer micro-dollars (1 USD = 1_000_000) so sums never drift."""

from decimal import Decimal

MICROS = 1_000_000


# The most any single payment or call may be: far above any real one, and low enough that
# no realistic ledger can overflow SQLite's 64-bit sums.
MAX_AMOUNT = 1_000_000_000 * MICROS  # $1 billion


def to_micros(usd: str | int | Decimal) -> int:
    return int((Decimal(str(usd)) * MICROS).to_integral_value())


def to_cents(micros: int) -> int:
    """Round half up to whole cents."""
    return (micros + 5_000) // 10_000


def cents_str(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def usd_display(micros: int) -> str:
    return cents_str(to_cents(micros))


def usd_shown(micros: int, digits=usd_display) -> str:
    """An amount for people to read, never for a file: one under a cent is <$0.01, not $0.00,
    so a small charge does not look like nothing. Exports and journals keep usd_display."""
    return "<$0.01" if 0 < micros < 10_000 else f"${digits(micros)}"
