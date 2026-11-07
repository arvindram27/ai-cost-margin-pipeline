"""Simulated monthly customer plans and exact revenue allocation (Phase 3).

The plan file is synthetic and labelled ``simulated``. It is not real revenue.
A customer with current usage in a UTC month is charged the full monthly price
for that month; there is no proration. Plan selection uses the period start.

Workload revenue is allocated by billable token volume (input plus output) of
current costed events only. The allocation is exact integer largest-remainder
apportionment on 10^-12 currency units. Unpriced events never contribute to the
basis, even when their token counts are known.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .money import CURRENCY_PATTERN, Money
from .pricing import parse_utc

__all__ = [
    "ALLOCATED",
    "BASIS_UNAVAILABLE",
    "INCOMPLETE_UNPRICED_EXCLUDED",
    "PlanInterval",
    "PlanList",
    "PlanScheduleError",
    "PlanSelection",
    "WORKLOAD_ORDER",
    "allocate_revenue",
    "load_plans",
    "parse_plans",
    "workload_sort_key",
]

ALLOCATED = "allocated"
INCOMPLETE_UNPRICED_EXCLUDED = "incomplete_unpriced_excluded"
BASIS_UNAVAILABLE = "basis_unavailable"
WORKLOAD_ORDER = ("cache_heavy", "long_heavy", "short_light", "spiky")

ROOT_FIELDS = frozenset(
    {"plans_version_id", "label", "currency", "timezone", "notes", "entries"}
)
ENTRY_FIELDS = frozenset(
    {"customer_id", "plan_id", "monthly_price", "effective_from", "effective_to"}
)
MONEY_PATTERN = re.compile(r"^\d+\.\d{12}$")
PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}$")


class PlanScheduleError(ValueError):
    """The simulated plan schedule is invalid; margins must not run."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = tuple(sorted(violations))
        super().__init__("; ".join(self.violations) if self.violations else "invalid plan schedule")


@dataclass(frozen=True)
class PlanInterval:
    """One effective-dated monthly price for one synthetic customer."""

    customer_id: str
    plan_id: str
    monthly_price: Money
    effective_from: datetime
    effective_to: datetime | None


@dataclass(frozen=True)
class PlanSelection:
    """The simulated plan applying to one customer-month."""

    customer_id: str
    plan_id: str
    plans_version_id: str
    monthly_price: Money


@dataclass(frozen=True)
class PlanList:
    """A validated, versioned set of simulated customer plans."""

    plans_version_id: str
    label: str
    currency: str
    timezone: str
    intervals: dict[str, tuple[PlanInterval, ...]]

    def select(self, customer_id: str, period: str) -> PlanSelection | None:
        """Select the plan covering the first instant of ``period`` (UTC)."""
        if not isinstance(period, str) or PERIOD_PATTERN.match(period) is None:
            raise ValueError("period must match YYYY-MM")
        start = parse_utc(f"{period}-01T00:00:00Z")
        for interval in self.intervals.get(customer_id, ()):
            if interval.effective_from <= start:
                if interval.effective_to is None or start < interval.effective_to:
                    return PlanSelection(
                        customer_id=customer_id,
                        plan_id=interval.plan_id,
                        plans_version_id=self.plans_version_id,
                        monthly_price=interval.monthly_price,
                    )
        return None


def workload_sort_key(workload_type: str) -> tuple[int, str]:
    """The single deterministic workload ordering used by tables and ties."""
    if workload_type in WORKLOAD_ORDER:
        return (WORKLOAD_ORDER.index(workload_type), workload_type)
    return (len(WORKLOAD_ORDER), workload_type)


def allocate_revenue(revenue: Money, weights: dict[str, int]) -> dict[str, Money]:
    """Allocate ``revenue`` exactly in proportion to non-negative integer weights.

    Leftover 10^-12 units are distributed one at a time by descending remainder,
    then by the documented workload order. The returned amounts always sum to
    exactly ``revenue``.
    """
    if not weights:
        raise ValueError("cannot allocate revenue without workload cells")
    for workload, weight in weights.items():
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
            raise ValueError(f"invalid allocation weight for {workload!r}")
    total_weight = sum(weights.values())
    if total_weight == 0:
        raise ValueError("allocation basis is unavailable")

    bases: dict[str, int] = {}
    remainders: dict[str, int] = {}
    for workload, weight in weights.items():
        raw = revenue.units * weight
        bases[workload] = raw // total_weight
        remainders[workload] = raw % total_weight

    leftover = revenue.units - sum(bases.values())
    order = sorted(
        weights,
        key=lambda workload: (-remainders[workload], workload_sort_key(workload)),
    )
    for workload in order[:leftover]:
        bases[workload] += 1

    if sum(bases.values()) != revenue.units:
        raise ValueError("exact revenue allocation failed")
    return {
        workload: Money(bases[workload], revenue.currency)
        for workload in weights
    }


def _valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value != ""
        and not any(character.isspace() for character in value)
    )


def _valid_timestamp(value: Any) -> bool:
    try:
        parse_utc(value)
    except ValueError:
        return False
    return True


def load_plans(path: Path) -> PlanList:
    """Read and validate a simulated plan file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanScheduleError([f"cannot read plans: {type(exc).__name__}"]) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanScheduleError([f"plans are not valid JSON: {exc.msg}"]) from exc
    return parse_plans(document)


def parse_plans(document: Any) -> PlanList:
    """Validate a decoded plan document and build per-customer intervals."""
    if not isinstance(document, dict):
        raise PlanScheduleError(["plans must be a JSON object"])

    violations: list[str] = []
    for name in sorted(set(document) - ROOT_FIELDS):
        violations.append(f"unknown plans field {name!r}")
    for name in ("plans_version_id", "label", "currency", "timezone", "entries"):
        if name not in document:
            violations.append(f"missing plans field {name!r}")
    if violations:
        raise PlanScheduleError(violations)

    if not _valid_id(document["plans_version_id"]):
        violations.append("plans_version_id must be a non-empty string without whitespace")
    if document["label"] != "simulated":
        violations.append(f"label must be 'simulated', got {document['label']!r}")
    currency = document["currency"]
    if not isinstance(currency, str) or CURRENCY_PATTERN.match(currency) is None:
        violations.append("currency must match '^[A-Z]{3}$'")
    if document["timezone"] != "UTC":
        violations.append("timezone must be 'UTC'")
    if "notes" in document and not isinstance(document["notes"], str):
        violations.append("notes must be a string")

    entries = document["entries"]
    if not isinstance(entries, list):
        violations.append("entries must be an array")
        raise PlanScheduleError(violations)

    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            violations.append(f"{where} must be an object")
            continue
        unknown = sorted(set(entry) - ENTRY_FIELDS)
        for name in unknown:
            violations.append(f"{where} has unknown field {name!r}")
        missing = [name for name in sorted(ENTRY_FIELDS) if name not in entry]
        for name in missing:
            violations.append(f"{where} is missing field {name!r}")
        if unknown or missing:
            continue

        for name in ("customer_id", "plan_id"):
            if not _valid_id(entry[name]):
                violations.append(f"{where} {name} must be a non-empty string without whitespace")

        price_text = entry["monthly_price"]
        monthly_price: Money | None = None
        if not isinstance(price_text, str) or MONEY_PATTERN.match(price_text) is None:
            violations.append(f"{where} monthly_price must match '^\\d+\\.\\d{{12}}$'")
        elif isinstance(currency, str) and CURRENCY_PATTERN.match(currency):
            try:
                monthly_price = Money.from_str(price_text, currency)
            except ValueError as exc:
                violations.append(f"{where} monthly_price is invalid: {exc}")

        if not _valid_timestamp(entry["effective_from"]):
            violations.append(f"{where} effective_from must be a strict UTC timestamp")
            continue
        effective_to = entry["effective_to"]
        if effective_to is not None and not _valid_timestamp(effective_to):
            violations.append(f"{where} effective_to must be null or a strict UTC timestamp")
            continue
        start = parse_utc(entry["effective_from"])
        end = parse_utc(effective_to) if effective_to is not None else None
        if end is not None and start >= end:
            violations.append(f"{where} effective_from must be before effective_to")
        if monthly_price is not None:
            parsed.append(
                {
                    "customer_id": entry["customer_id"],
                    "plan_id": entry["plan_id"],
                    "monthly_price": monthly_price,
                    "effective_from": start,
                    "effective_to": end,
                }
            )

    starts = [(item["customer_id"], item["effective_from"]) for item in parsed]
    for duplicated in sorted({value for value in starts if starts.count(value) > 1}):
        violations.append(
            f"duplicate interval start for {duplicated[0]} at {duplicated[1].isoformat()}"
        )

    grouped: dict[str, list[PlanInterval]] = {}
    for item in parsed:
        grouped.setdefault(item["customer_id"], []).append(
            PlanInterval(
                customer_id=item["customer_id"],
                plan_id=item["plan_id"],
                monthly_price=item["monthly_price"],
                effective_from=item["effective_from"],
                effective_to=item["effective_to"],
            )
        )

    intervals: dict[str, tuple[PlanInterval, ...]] = {}
    for customer_id in sorted(grouped):
        customer_intervals = sorted(grouped[customer_id], key=lambda value: value.effective_from)
        open_ended = 0
        for position, interval in enumerate(customer_intervals):
            if interval.effective_to is None:
                open_ended += 1
                if position != len(customer_intervals) - 1:
                    violations.append(
                        f"{customer_id}: open-ended interval is not the last interval"
                    )
        if open_ended > 1:
            violations.append(f"{customer_id}: more than one open-ended interval")
        for previous, following in zip(customer_intervals, customer_intervals[1:]):
            if previous.effective_to is None:
                continue
            if following.effective_from < previous.effective_to:
                violations.append(f"{customer_id}: overlap between plan intervals")
            elif following.effective_from > previous.effective_to:
                violations.append(f"{customer_id}: gap between plan intervals")
        intervals[customer_id] = tuple(customer_intervals)

    if violations:
        raise PlanScheduleError(violations)

    return PlanList(
        plans_version_id=document["plans_version_id"],
        label=document["label"],
        currency=currency,
        timezone=document["timezone"],
        intervals=intervals,
    )
