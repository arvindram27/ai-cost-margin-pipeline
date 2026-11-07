"""Exact money: integer-scaled units, no floats, no silent rounding."""

from __future__ import annotations

import decimal

import pytest

from ai_cost_margin.money import DENOMINATOR, CurrencyMismatchError, Money


def test_addition_within_one_currency_is_exact():
    total = Money.from_str("0.000000000001", "USD") + Money.from_str("0.000000000002", "USD")
    assert str(total) == "0.000000000003"


def test_currency_mismatch_raises():
    with pytest.raises(CurrencyMismatchError):
        Money.from_str("0.000000000001", "USD") + Money.from_str("0.000000000001", "EUR")
    with pytest.raises(CurrencyMismatchError):
        Money.from_str("0.000000000001", "USD") - Money.from_str("0.000000000001", "EUR")


def test_float_amount_is_rejected():
    with pytest.raises(TypeError):
        Money(1.5, "USD")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money.from_tokens_and_micro_rate(10.5, 750000, "USD")  # type: ignore[arg-type]


def test_quantise_keeps_twelve_decimal_places():
    money = Money.from_tokens_and_micro_rate(1, 1, "USD")
    assert str(money.quantise()) == "0.000000000001"
    assert money.quantise() == money


def test_string_form_has_no_exponent():
    money = Money.from_tokens_and_micro_rate(10**20, 1234567, "USD")
    rendered = str(money)
    assert "e" not in rendered.lower()
    whole, fraction = rendered.split(".")
    assert len(fraction) == 12
    assert whole.isdigit()


def test_zero_is_rendered_consistently():
    zero = Money(0, "USD")
    assert str(zero) == "0.000000000000"
    assert zero.is_zero()
    assert zero.amount == decimal.Decimal("0")


def test_huge_token_count_is_exact_beyond_28_significant_digits():
    tokens = 999999999999999999999999999
    micro = 1234567
    money = Money.from_tokens_and_micro_rate(tokens, micro, "USD")

    units = tokens * micro
    whole, fraction = divmod(units, DENOMINATOR)
    assert money.units == units
    assert str(money) == f"{whole}.{fraction:012d}"

    # A fixed 28-digit decimal precision would round this value. Money must not.
    with decimal.localcontext() as context:
        context.prec = 28
        rounded = +money.amount
    assert rounded != money.amount


def test_huge_addition_is_exact():
    tokens = 999999999999999999999999999
    left = Money.from_tokens_and_micro_rate(tokens, 1234567, "USD")
    right = Money.from_tokens_and_micro_rate(tokens, 7654321, "USD")
    total = left + right

    units = tokens * (1234567 + 7654321)
    whole, fraction = divmod(units, DENOMINATOR)
    assert total.units == units
    assert str(total) == f"{whole}.{fraction:012d}"
    assert Money.from_str(str(total), "USD") == total


def test_round_trip_string_parsing_is_lossless():
    money = Money.from_tokens_and_micro_rate(4825, 750000, "USD")
    assert Money.from_str(str(money), "USD") == money
    with pytest.raises(ValueError):
        Money.from_str("1.5", "USD")
    with pytest.raises(ValueError):
        Money.from_str("1E+6", "USD")
