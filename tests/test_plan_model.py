"""Simulated plan validation, versioning, and period-start selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_cost_margin.revenue import PlanScheduleError, load_plans, parse_plans

ROOT = Path(__file__).resolve().parents[1]
PHASE3 = ROOT / "fixtures" / "phase3"


def _entry(customer="cust-001", plan="starter", price="49.000000000000", start="2026-10-01T00:00:00Z", end=None):
    return {
        "customer_id": customer,
        "plan_id": plan,
        "monthly_price": price,
        "effective_from": start,
        "effective_to": end,
    }


def _document(entries=None):
    return {
        "plans_version_id": "test-plans",
        "label": "simulated",
        "currency": "USD",
        "timezone": "UTC",
        "entries": [_entry()] if entries is None else entries,
    }


def test_committed_plans_load_and_select_a_customer_month():
    plans = load_plans(PHASE3 / "customer-plans-simulated-v1.json")
    selected = plans.select("cust-001", "2026-10")
    assert selected is not None
    assert selected.plan_id == "starter"
    assert selected.plans_version_id == "simulated-plans-v1"
    assert str(selected.monthly_price) == "49.000000000000"
    assert plans.label == "simulated"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "plans-gap.json",
        "plans-overlap.json",
        "plans-duplicate.json",
        "plans-bad-label.json",
        "plans-bad-money.json",
    ],
)
def test_invalid_plan_fixtures_are_rejected(fixture_name):
    with pytest.raises(PlanScheduleError):
        load_plans(PHASE3 / "invalid" / fixture_name)


def test_unknown_plan_fields_are_rejected():
    document = _document()
    document["unexpected"] = True
    with pytest.raises(PlanScheduleError, match="unknown plans field"):
        parse_plans(document)


def test_plan_selection_uses_the_period_start():
    plans = parse_plans(
        _document(
            [
                _entry(price="49.000000000000", end="2026-11-01T00:00:00Z"),
                _entry(plan="growth", price="99.000000000000", start="2026-11-01T00:00:00Z"),
            ]
        )
    )
    assert str(plans.select("cust-001", "2026-10").monthly_price) == "49.000000000000"
    assert plans.select("cust-001", "2026-11").plan_id == "growth"


def test_an_uncovered_customer_or_month_returns_none():
    plans = parse_plans(_document())
    assert plans.select("cust-999", "2026-10") is None
    assert plans.select("cust-001", "2026-09") is None


def test_a_malformed_period_is_rejected():
    plans = parse_plans(_document())
    with pytest.raises(ValueError, match="YYYY-MM"):
        plans.select("cust-001", "2026-10-01")
