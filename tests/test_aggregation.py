"""Customer/workload aggregation, unallocated usage, and completeness rules."""

from __future__ import annotations

import hashlib

import pytest

from ai_cost_margin.aggregate import UnknownPlanError, aggregate_rows
from ai_cost_margin.costing import cost_events
from ai_cost_margin.lineage import build_lineage
from ai_cost_margin.money import DENOMINATOR
from ai_cost_margin.pricing import parse_price_list
from ai_cost_margin.revenue import (
    ALLOCATED,
    BASIS_UNAVAILABLE,
    INCOMPLETE_UNPRICED_EXCLUDED,
    parse_plans,
)

CUTOFF = "2026-11-01T23:59:59Z"
RATES = {"cached": "0.150000", "input": "0.750000", "output": "2.250000"}


def _price_list():
    return parse_price_list(
        {
            "currency": "USD",
            "entries": [
                {
                    "currency": "USD",
                    "effective_from": "2026-10-01T00:00:00Z",
                    "effective_to": None,
                    "model": "m",
                    "price_version_id": "v1",
                    "provider": "p",
                    "rates_per_million": dict(RATES),
                }
            ],
            "label": "synthetic",
            "price_list_id": "aggregation-test",
            "rate_unit": "per_1_000_000_tokens",
            "timezone": "UTC",
        }
    )


def _plans(first_price="10.000000000000"):
    return parse_plans(
        {
            "plans_version_id": "test-plans",
            "label": "simulated",
            "currency": "USD",
            "timezone": "UTC",
            "entries": [
                {
                    "customer_id": "cust-001",
                    "plan_id": "test-plan",
                    "monthly_price": first_price,
                    "effective_from": "2026-10-01T00:00:00Z",
                    "effective_to": None,
                },
                {
                    "customer_id": "cust-002",
                    "plan_id": "test-plan-2",
                    "monthly_price": "20.000000000000",
                    "effective_from": "2026-10-01T00:00:00Z",
                    "effective_to": None,
                },
            ],
        }
    )


def _event(
    event_id,
    *,
    customer="cust-001",
    workload="short_light",
    model="m",
    usage="2026-10-05T00:00:00Z",
    tokens_input=1000,
    tokens_output=500,
    tokens_cached=0,
    version=1,
    supersedes=None,
):
    return {
        "provider": "p",
        "account_id": "acct-demo",
        "provider_event_id": event_id,
        "attempt_id": "1",
        "request_id": f"req-{event_id}",
        "model": model,
        "feature": "demo-assistant",
        "workload_type": workload,
        "customer_id": customer,
        "usage_time": usage,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_cached": tokens_cached,
        "unavailable_fields": [],
        "record_version": version,
        "supersedes": supersedes,
        "arrival_time": "2026-11-01T00:00:00Z",
        "payload_sha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
    }


def _aggregate(events, plans=None):
    lineage = build_lineage(events, CUTOFF)
    rows, _ = cost_events(lineage.events, _price_list())
    result = aggregate_rows(rows, lineage.events, _plans() if plans is None else plans, "USD")
    return result, rows


def test_unattributable_usage_is_unallocated_and_its_share_is_reported():
    result, _ = _aggregate([_event("evt-a"), _event("evt-unallocated", customer=None)])
    assert len(result.customer_margins) == 1
    assert len(result.unallocated) == 1
    bucket = result.unallocated[0]
    assert bucket["customer_id"] is None
    assert bucket["bucket"] == "unallocated"
    assert bucket["events_costed"] == 1
    assert result.metrics["unallocated_cost_share_bp"] == 5000
    assert result.metrics["unallocated_event_share_bp"] == 5000


def test_customer_and_workload_costs_reconcile_exactly():
    result, rows = _aggregate([
        _event("evt-a", workload="short_light"),
        _event("evt-b", workload="spiky"),
    ])
    customer = result.customer_margins[0]
    expected = sum(int(row["total_cost"].replace(".", "")) for row in rows)
    assert int(customer["estimated_cost"].replace(".", "")) == expected
    assert sum(
        int(row["estimated_cost"].replace(".", "")) for row in result.workload_margins
    ) == expected


def test_superseded_events_are_counted_but_never_costed():
    result, _ = _aggregate([
        _event("evt-a", version=1),
        _event("evt-a", version=2, tokens_input=2000),
    ])
    customer = result.customer_margins[0]
    assert customer["events_superseded"] == 1
    assert customer["events_costed"] == 1
    assert customer["estimated_cost"] == "0.002625000000"


def test_unpriced_usage_marks_every_workload_cell_in_the_customer_month_incomplete():
    result, _ = _aggregate([
        _event("evt-a", workload="short_light"),
        _event("evt-b", workload="spiky"),
        _event("evt-c", workload="long_heavy", model="unlisted", tokens_input=9000, tokens_output=9000),
    ])
    assert {row["revenue_allocation_status"] for row in result.workload_margins} == {
        INCOMPLETE_UNPRICED_EXCLUDED
    }
    assert all(row["margin_status"] == "incomplete" for row in result.workload_margins)
    # The unpriced event's known 18,000 tokens do not enter the basis: the two
    # costed cells have equal weight and split the revenue equally, while the
    # unpriced cell has zero weight and receives an exact zero allocation.
    by_workload = {row["workload_type"]: row for row in result.workload_margins}
    assert by_workload["short_light"]["simulated_revenue"] == "5.000000000000"
    assert by_workload["spiky"]["simulated_revenue"] == "5.000000000000"
    assert by_workload["long_heavy"]["simulated_revenue"] == "0.000000000000"
    assert result.customer_margins[0]["margin_status"] == "incomplete"


def test_basis_unavailable_takes_precedence_over_unpriced_incompleteness():
    result, _ = _aggregate([
        _event("evt-a", workload="short_light", tokens_input=0, tokens_output=0),
        _event("evt-b", workload="long_heavy", model="unlisted"),
    ])
    assert len(result.workload_margins) == 2
    assert {row["revenue_allocation_status"] for row in result.workload_margins} == {
        BASIS_UNAVAILABLE
    }
    assert all(row["simulated_revenue"] is None for row in result.workload_margins)
    assert all(row["margin_status"] == "incomplete" for row in result.workload_margins)


def test_a_fully_priced_customer_month_has_allocated_complete_workloads():
    result, _ = _aggregate([
        _event("evt-a", workload="short_light"),
        _event("evt-b", workload="spiky"),
    ])
    assert {row["revenue_allocation_status"] for row in result.workload_margins} == {ALLOCATED}
    assert all(row["margin_status"] == "complete" for row in result.workload_margins)
    assert result.customer_margins[0]["margin_status"] == "complete"


def test_an_unplanned_customer_is_fatal():
    with pytest.raises(UnknownPlanError):
        _aggregate([_event("evt-a", customer="cust-999")])


def test_each_active_month_gets_its_own_full_monthly_revenue():
    result, _ = _aggregate([
        _event("evt-oct", usage="2026-10-05T00:00:00Z"),
        _event("evt-nov", usage="2026-11-05T00:00:00Z"),
    ])
    assert [row["period"] for row in result.customer_margins] == ["2026-10", "2026-11"]
    assert [row["simulated_revenue"] for row in result.customer_margins] == [
        "10.000000000000",
        "10.000000000000",
    ]


def test_a_negative_margin_keeps_its_sign_and_twelve_decimals():
    result, _ = _aggregate([_event("evt-a")], plans=_plans("0.000000000001"))
    margin = result.customer_margins[0]["simulated_margin"]
    assert margin.startswith("-")
    assert len(margin.split(".")[1]) == 12


def test_unallocated_unpriced_usage_remains_visible_with_a_reason():
    result, _ = _aggregate([_event("evt-a", customer=None, model="unlisted")])
    bucket = result.unallocated[0]
    assert bucket["events_unpriced"] == 1
    assert bucket["unpriced_model_not_in_price_list"] == 1
    assert bucket["estimated_cost"] is None


def test_huge_cost_aggregation_is_exact_beyond_28_digits():
    tokens = 999999999999999999999999999
    result, _ = _aggregate([
        _event("evt-huge", tokens_input=tokens, tokens_output=0, tokens_cached=0)
    ])
    expected_units = tokens * 750000
    whole, fraction = divmod(expected_units, DENOMINATOR)
    assert result.customer_margins[0]["estimated_cost"] == f"{whole}.{fraction:012d}"
