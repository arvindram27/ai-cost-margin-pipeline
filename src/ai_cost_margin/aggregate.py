"""Aggregation into customer, workload, and unallocated result tables (Phase 3).

Only current records (``costed`` or ``unpriced``) create margin cells. Superseded
records remain visible as counts in an active cell; a superseded-only cell does
not create a margin row and is reported separately as
``superseded_events_outside_active_cells``.

The allocation basis is input plus output tokens of current costed events. If a
customer-month has any unpriced usage, every workload cell in that month is
incomplete because the excluded event shifts the whole proportional split.
``basis_unavailable`` takes precedence when total costed token weight is zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .costing import UNPRICED_REASONS
from .money import Money
from .revenue import (
    ALLOCATED,
    BASIS_UNAVAILABLE,
    INCOMPLETE_UNPRICED_EXCLUDED,
    PlanList,
    PlanSelection,
    allocate_revenue,
    workload_sort_key,
)

__all__ = [
    "AggregationResult",
    "CompositionError",
    "UnknownPlanError",
    "aggregate_rows",
    "share_basis_points",
]


class UnknownPlanError(ValueError):
    """A current customer has no simulated plan; revenue must not be invented."""


class CompositionError(ValueError):
    """The aggregated cells do not partition the input rows exactly."""


@dataclass(frozen=True)
class AggregationResult:
    customer_margins: list[dict[str, Any]]
    workload_margins: list[dict[str, Any]]
    unallocated: list[dict[str, Any]]
    metrics: dict[str, Any]


def share_basis_points(numerator: int, denominator: int) -> int:
    """Return an exact-integer, round-half-up share in basis points."""
    if denominator == 0:
        return 0
    return (2 * numerator * 10000 + denominator) // (2 * denominator)


def _sum_cost(rows: Sequence[dict[str, Any]], currency: str) -> Money | None:
    total: Money | None = None
    for row in rows:
        if row["cost_status"] != "costed":
            continue
        amount = Money.from_str(row["total_cost"], currency)
        total = amount if total is None else total + amount
    return total


def _units(amount: Money | None) -> int:
    return 0 if amount is None else amount.units


def _active(rows: Sequence[dict[str, Any]]) -> bool:
    return any(row["cost_status"] != "superseded" for row in rows)


def _count(rows: Sequence[dict[str, Any]], status: str) -> int:
    return sum(1 for row in rows if row["cost_status"] == status)


def _reason_count(rows: Sequence[dict[str, Any]], reason: str) -> int:
    return sum(1 for row in rows if row["cost_status"] == "unpriced" and row["unpriced_reason"] == reason)


def _labels() -> dict[str, str]:
    return {
        "usage_label": "synthetic",
        "cost_label": "estimated",
        "revenue_label": "simulated",
        "margin_label": "simulated",
    }


def _count_fields(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    fields = {
        "events_costed": _count(rows, "costed"),
        "events_unpriced": _count(rows, "unpriced"),
        "events_superseded": _count(rows, "superseded"),
    }
    for reason in UNPRICED_REASONS:
        fields[f"unpriced_{reason}"] = _reason_count(rows, reason)
    return fields


def _customer_row(
    period: str,
    customer_id: str,
    selection: PlanSelection,
    rows: Sequence[dict[str, Any]],
    currency: str,
) -> dict[str, Any]:
    cost = _sum_cost(rows, currency)
    revenue = selection.monthly_price
    unpriced = _count(rows, "unpriced")
    margin = None if cost is None else revenue - cost
    return {
        "period": period,
        "customer_id": customer_id,
        "plan_id": selection.plan_id,
        "plans_version_id": selection.plans_version_id,
        "currency": currency,
        "simulated_revenue": str(revenue),
        "estimated_cost": None if cost is None else str(cost),
        "simulated_margin": None if margin is None else str(margin),
        "margin_status": "complete" if unpriced == 0 and cost is not None else "incomplete",
        **_count_fields(rows),
        **_labels(),
    }


def _workload_row(
    period: str,
    customer_id: str,
    workload_type: str,
    selection: PlanSelection,
    rows: Sequence[dict[str, Any]],
    currency: str,
) -> dict[str, Any]:
    cost = _sum_cost(rows, currency)
    return {
        "period": period,
        "customer_id": customer_id,
        "workload_type": workload_type,
        "plan_id": selection.plan_id,
        "plans_version_id": selection.plans_version_id,
        "currency": currency,
        "simulated_revenue": None,
        "estimated_cost": None if cost is None else str(cost),
        "simulated_margin": None,
        "margin_status": "incomplete",
        "revenue_allocation_status": BASIS_UNAVAILABLE,
        "revenue_allocation_basis": None,
        **_count_fields(rows),
        **_labels(),
    }


def _unallocated_row(
    period: str,
    workload_type: str,
    rows: Sequence[dict[str, Any]],
    currency: str,
) -> dict[str, Any]:
    cost = _sum_cost(rows, currency)
    return {
        "bucket": "unallocated",
        "customer_id": None,
        "period": period,
        "workload_type": workload_type,
        "currency": currency,
        "estimated_cost": None if cost is None else str(cost),
        **_count_fields(rows),
        "usage_label": "synthetic",
        "cost_label": "estimated",
    }


def _weight(row: dict[str, Any], event: dict[str, Any]) -> int:
    if row["cost_status"] != "costed":
        return 0
    return event["tokens_input"] + event["tokens_output"]


def aggregate_rows(
    rows: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    plans: PlanList,
    currency: str,
) -> AggregationResult:
    """Aggregate Phase 2 rows and their index-aligned stored events."""
    if len(rows) != len(events):
        raise CompositionError("costed rows and stored events are not index-aligned")
    for row, event in zip(rows, events):
        for field in ("provider", "account_id", "provider_event_id", "record_version"):
            if row[field] != event[field]:
                raise CompositionError(f"costed row/event mismatch on {field}")

    customer_groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    workload_groups: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    unallocated_groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row, event in zip(rows, events):
        period = row["usage_time"][:7]
        customer_id = row["customer_id"]
        workload = row["workload_type"]
        pair = (row, event)
        if customer_id is None:
            unallocated_groups.setdefault((period, workload), []).append(pair)
        else:
            customer_groups.setdefault((period, customer_id), []).append(pair)
            workload_groups.setdefault((period, customer_id, workload), []).append(pair)

    customer_rows: list[dict[str, Any]] = []
    selections: dict[tuple[str, str], PlanSelection] = {}
    customer_group_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key in sorted(customer_groups):
        pairs = customer_groups[key]
        group_rows = [pair[0] for pair in pairs]
        if not _active(group_rows):
            continue
        period, customer_id = key
        selection = plans.select(customer_id, period)
        if selection is None:
            raise UnknownPlanError(f"no simulated plan covers {customer_id!r} in {period}")
        selections[key] = selection
        customer_group_rows[key] = group_rows
        customer_rows.append(_customer_row(period, customer_id, selection, group_rows, currency))

    workload_rows: list[dict[str, Any]] = []
    workload_by_customer: dict[tuple[str, str], list[tuple[str, dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]]] = {}
    for key in sorted(workload_groups, key=lambda value: (value[0], value[1], workload_sort_key(value[2]))):
        pairs = workload_groups[key]
        group_rows = [pair[0] for pair in pairs]
        if not _active(group_rows):
            continue
        period, customer_id, workload = key
        customer_key = (period, customer_id)
        if customer_key not in selections:
            raise CompositionError("active workload cell has no active customer cell")
        row = _workload_row(period, customer_id, workload, selections[customer_key], group_rows, currency)
        workload_rows.append(row)
        workload_by_customer.setdefault(customer_key, []).append((workload, row, pairs))

    # R1: zero total weight wins over incomplete_unpriced_excluded.
    for customer_key in sorted(workload_by_customer):
        cells = workload_by_customer[customer_key]
        customer_row = next(
            row for row in customer_rows
            if (row["period"], row["customer_id"]) == customer_key
        )
        weights = {
            workload: sum(_weight(row, event) for row, event in pairs)
            for workload, _, pairs in cells
        }
        total_weight = sum(weights.values())
        customer_unpriced = customer_row["events_unpriced"] > 0
        if total_weight == 0:
            for _, row, _ in cells:
                row["revenue_allocation_status"] = BASIS_UNAVAILABLE
                row["revenue_allocation_basis"] = None
                row["simulated_revenue"] = None
                row["simulated_margin"] = None
                row["margin_status"] = "incomplete"
            continue

        revenue = Money.from_str(customer_row["simulated_revenue"], currency)
        allocated = allocate_revenue(revenue, weights)
        status = INCOMPLETE_UNPRICED_EXCLUDED if customer_unpriced else ALLOCATED
        for workload, row, pairs in cells:
            cost = _sum_cost([pair[0] for pair in pairs], currency)
            amount = allocated[workload]
            row["revenue_allocation_status"] = status
            row["revenue_allocation_basis"] = "tokens_input_plus_output"
            row["simulated_revenue"] = str(amount)
            row["simulated_margin"] = None if cost is None else str(amount - cost)
            row["margin_status"] = (
                "complete"
                if status == ALLOCATED and row["events_unpriced"] == 0 and cost is not None
                else "incomplete"
            )

    unallocated_rows: list[dict[str, Any]] = []
    for key in sorted(unallocated_groups, key=lambda value: (value[0], workload_sort_key(value[1]))):
        group_rows = [pair[0] for pair in unallocated_groups[key]]
        if not _active(group_rows):
            continue
        unallocated_rows.append(_unallocated_row(key[0], key[1], group_rows, currency))

    events_costed = _count(rows, "costed")
    events_unpriced = _count(rows, "unpriced")
    events_superseded = _count(rows, "superseded")
    counted_superseded = sum(row["events_superseded"] for row in customer_rows)
    counted_superseded += sum(row["events_superseded"] for row in unallocated_rows)
    outside_active = events_superseded - counted_superseded

    total_cost = _sum_cost(rows, currency)
    unallocated_cost = _sum_cost(
        [row for row in rows if row["customer_id"] is None],
        currency,
    )
    unallocated_costed = sum(row["events_costed"] for row in unallocated_rows)
    total_cost_units = _units(total_cost)
    unallocated_cost_units = _units(unallocated_cost)

    metrics = {
        "events_read": len(rows),
        "events_costed": events_costed,
        "events_unpriced": events_unpriced,
        "events_superseded": events_superseded,
        "superseded_events_outside_active_cells": outside_active,
        "periods": sorted({row["usage_time"][:7] for row in rows}),
        "customer_rows": len(customer_rows),
        "workload_rows": len(workload_rows),
        "workload_rows_allocated": sum(
            row["revenue_allocation_status"] == ALLOCATED for row in workload_rows
        ),
        "workload_rows_incomplete_unpriced_basis": sum(
            row["revenue_allocation_status"] == INCOMPLETE_UNPRICED_EXCLUDED
            for row in workload_rows
        ),
        "workload_rows_basis_unavailable": sum(
            row["revenue_allocation_status"] == BASIS_UNAVAILABLE for row in workload_rows
        ),
        "incomplete_customer_margins": sum(
            row["margin_status"] == "incomplete" for row in customer_rows
        ),
        "incomplete_workload_margins": sum(
            row["margin_status"] == "incomplete" for row in workload_rows
        ),
        "unallocated_events_costed": unallocated_costed,
        "unallocated_events_unpriced": sum(row["events_unpriced"] for row in unallocated_rows),
        "unallocated_events_superseded": sum(row["events_superseded"] for row in unallocated_rows),
        "unallocated_cost_units": unallocated_cost_units,
        "total_cost_units": total_cost_units,
        "unallocated_cost_share_bp": share_basis_points(unallocated_cost_units, total_cost_units),
        "unallocated_event_share_bp": share_basis_points(unallocated_costed, events_costed),
    }

    result = AggregationResult(customer_rows, workload_rows, unallocated_rows, metrics)
    _validate_composition(rows, result)
    return result


def _validate_composition(rows: Sequence[dict[str, Any]], result: AggregationResult) -> None:
    metrics = result.metrics
    customer_costed = sum(row["events_costed"] for row in result.customer_margins)
    customer_unpriced = sum(row["events_unpriced"] for row in result.customer_margins)
    unallocated_costed = sum(row["events_costed"] for row in result.unallocated)
    unallocated_unpriced = sum(row["events_unpriced"] for row in result.unallocated)
    workload_costed = sum(row["events_costed"] for row in result.workload_margins)
    workload_unpriced = sum(row["events_unpriced"] for row in result.workload_margins)

    checks = [
        metrics["events_read"] == metrics["events_costed"] + metrics["events_unpriced"] + metrics["events_superseded"],
        customer_costed + unallocated_costed == metrics["events_costed"],
        customer_unpriced + unallocated_unpriced == metrics["events_unpriced"],
        workload_costed == customer_costed,
        workload_unpriced == customer_unpriced,
        # A workload cell that contains only superseded records creates no
        # margin row, so its superseded count stays visible at the customer
        # level but cannot appear in the workload table. It must never exceed
        # the customer count.
        sum(row["events_superseded"] for row in result.workload_margins)
        <= sum(row["events_superseded"] for row in result.customer_margins),
        sum(row["events_superseded"] for row in result.customer_margins)
        + sum(row["events_superseded"] for row in result.unallocated)
        + metrics["superseded_events_outside_active_cells"]
        == metrics["events_superseded"],
    ]

    customer_cost_units = sum(
        _units(Money.from_str(row["estimated_cost"], row["currency"]))
        for row in result.customer_margins if row["estimated_cost"] is not None
    )
    unallocated_cost_units = sum(
        _units(Money.from_str(row["estimated_cost"], row["currency"]))
        for row in result.unallocated if row["estimated_cost"] is not None
    )
    checks.append(customer_cost_units + unallocated_cost_units == metrics["total_cost_units"])

    for customer in result.customer_margins:
        key = (customer["period"], customer["customer_id"])
        cells = [
            row for row in result.workload_margins
            if (row["period"], row["customer_id"]) == key
        ]
        workload_cost = sum(
            _units(Money.from_str(row["estimated_cost"], row["currency"]))
            for row in cells if row["estimated_cost"] is not None
        )
        customer_cost = _units(Money.from_str(customer["estimated_cost"], customer["currency"])) if customer["estimated_cost"] is not None else 0
        checks.append(workload_cost == customer_cost)
        if all(row["revenue_allocation_status"] != BASIS_UNAVAILABLE for row in cells):
            workload_revenue = sum(
                _units(Money.from_str(row["simulated_revenue"], row["currency"]))
                for row in cells if row["simulated_revenue"] is not None
            )
            customer_revenue = _units(Money.from_str(customer["simulated_revenue"], customer["currency"]))
            checks.append(workload_revenue == customer_revenue)

    if not all(checks):
        raise CompositionError("aggregation composition identity failed")
