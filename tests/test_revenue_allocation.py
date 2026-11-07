"""Exact largest-remainder simulated revenue allocation."""

from __future__ import annotations

import pytest

from ai_cost_margin.money import Money
from ai_cost_margin.revenue import allocate_revenue


def test_one_workload_receives_the_full_revenue():
    allocated = allocate_revenue(Money.from_str("49.000000000000", "USD"), {"short_light": 1500})
    assert str(allocated["short_light"]) == "49.000000000000"


def test_proportional_allocation_is_exact_at_the_last_unit():
    allocated = allocate_revenue(
        Money.from_str("1.000000000000", "USD"),
        {"short_light": 1, "long_heavy": 2},
    )
    assert allocated["short_light"].units == 333333333333
    assert allocated["long_heavy"].units == 666666666667
    assert sum(value.units for value in allocated.values()) == 10**12


def test_largest_remainder_ties_use_the_documented_workload_order():
    allocated = allocate_revenue(
        Money(2, "USD"),
        {"spiky": 1, "short_light": 1, "long_heavy": 1, "cache_heavy": 1},
    )
    assert allocated["cache_heavy"].units == 1
    assert allocated["long_heavy"].units == 1
    assert allocated["short_light"].units == 0
    assert allocated["spiky"].units == 0


def test_zero_total_weight_is_an_unavailable_basis():
    with pytest.raises(ValueError, match="basis is unavailable"):
        allocate_revenue(Money.from_str("49.000000000000", "USD"), {"short_light": 0})


def test_zero_weight_cells_get_zero_but_the_total_is_preserved():
    allocated = allocate_revenue(
        Money.from_str("99.000000000000", "USD"),
        {"short_light": 0, "spiky": 5, "cache_heavy": 7},
    )
    assert allocated["short_light"].is_zero()
    assert sum(value.units for value in allocated.values()) == 99 * 10**12


def test_huge_revenue_and_weights_stay_exact_beyond_28_digits():
    revenue = Money(10**40 + 123456789, "USD")
    weights = {"short_light": 999999999999999999999999999, "spiky": 888888888888888888888888888}
    allocated = allocate_revenue(revenue, weights)
    assert sum(value.units for value in allocated.values()) == revenue.units
    assert allocated["short_light"].units == revenue.units - allocated["spiky"].units


def test_invalid_weights_are_rejected():
    with pytest.raises(ValueError):
        allocate_revenue(Money(100, "USD"), {"short_light": -1})
    with pytest.raises(ValueError):
        allocate_revenue(Money(100, "USD"), {"short_light": True})
