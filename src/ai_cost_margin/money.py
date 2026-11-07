"""Exact money for Phase 2, built on integer-scaled units (never floats).

An amount is stored as an integer count of 10^-12 currency units, so every
operation here is exact integer arithmetic. That matters because the Phase 1
schema places no upper bound on an integer token count: a fixed decimal
precision would round silently once an amount grew past it, while integer
arithmetic cannot.

The public ``amount`` is a ``decimal.Decimal`` built from the exact digits, and
``str(money)`` renders exactly twelve decimal places. No decimal rounding
context is consulted to produce a value, so nothing here can round.

Rates enter as integer "micro" numerators: a rate string ``"0.750000"`` means
0.750000 per 1,000,000 tokens, so its micro numerator is ``750000``. A unit
cost is then ``tokens * micro`` scaled units, i.e. ``tokens * rate / 10^6``,
which is exact by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "CURRENCY_PATTERN",
    "CurrencyMismatchError",
    "DENOMINATOR",
    "Money",
    "SCALE",
]

SCALE = 12
DENOMINATOR = 10 ** SCALE

CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_CANONICAL_PATTERN = re.compile(r"^-?\d+\.\d{12}$")


class CurrencyMismatchError(ValueError):
    """Raised when two amounts in different currencies are combined."""


def _valid_currency(currency: object) -> bool:
    return isinstance(currency, str) and CURRENCY_PATTERN.match(currency) is not None


@dataclass(frozen=True)
class Money:
    """An exact amount of one currency, held as 10^-12 units."""

    units: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.units, bool) or not isinstance(self.units, int):
            raise TypeError("units must be an int: floats are never money")
        if not _valid_currency(self.currency):
            raise ValueError(f"currency must match {CURRENCY_PATTERN.pattern!r}")

    @classmethod
    def from_tokens_and_micro_rate(cls, tokens: int, micro_rate: int, currency: str) -> "Money":
        """Exact unit cost for ``tokens`` at a rate of ``micro_rate / 10^6`` per million."""
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise TypeError("tokens must be an int")
        if isinstance(micro_rate, bool) or not isinstance(micro_rate, int):
            raise TypeError("micro_rate must be an int")
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        if micro_rate < 0:
            raise ValueError("micro_rate must be >= 0")
        return cls(tokens * micro_rate, currency)

    @classmethod
    def from_str(cls, text: str, currency: str) -> "Money":
        """Parse the canonical twelve-decimal string form exactly."""
        if not isinstance(text, str) or _CANONICAL_PATTERN.match(text) is None:
            raise ValueError(f"amount must match {_CANONICAL_PATTERN.pattern!r}")
        negative = text.startswith("-")
        body = text[1:] if negative else text
        whole, fraction = body.split(".")
        units = int(whole) * DENOMINATOR + int(fraction)
        return cls(-units if negative else units, currency)

    @property
    def amount(self) -> Decimal:
        """The exact amount as a ``Decimal`` (built from digits, never rounded)."""
        return Decimal(str(self))

    def __add__(self, other: object) -> "Money":
        self._require_same_currency(other)
        return Money(self.units + other.units, self.currency)  # type: ignore[union-attr]

    def __sub__(self, other: object) -> "Money":
        self._require_same_currency(other)
        return Money(self.units - other.units, self.currency)  # type: ignore[union-attr]

    def _require_same_currency(self, other: object) -> None:
        if not isinstance(other, Money):
            raise TypeError("can only combine Money with Money")
        if other.currency != self.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} with {other.currency}")

    def quantise(self) -> "Money":
        """Already exact at twelve decimal places; returns itself.

        The call site stays explicit so a future change of scale cannot silently
        move the normalisation point.
        """
        return self

    def is_zero(self) -> bool:
        return self.units == 0

    def __str__(self) -> str:
        sign = "-" if self.units < 0 else ""
        whole, fraction = divmod(abs(self.units), DENOMINATOR)
        return f"{sign}{whole}.{fraction:012d}"
