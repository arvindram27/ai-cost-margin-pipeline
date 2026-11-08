"""What-if scenario recomputation over the committed synthetic export.

Scenario figures are recomputed IN MEMORY through the existing pricing
engine (pricing, costing, revenue allocation, aggregation and the canonical
digest) for user-chosen levers. Nothing here is persisted: no writes to the
export, the repository, or any store. The reconstructed payload hash is a
reconstruction marker computed from the exported row; it is NOT the original
Phase 1 payload hash and is never displayed.

This module imports no Streamlit and holds no mutable module state, so two
concurrent sessions can neither see nor affect each other's scenarios.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import export_data
from ai_cost_margin import aggregate, costing, digest, pricing, revenue
from ai_cost_margin.store import canonical_json

__all__ = [
    "ScenarioLevers",
    "ScenarioResult",
    "build_engine_events",
    "build_price_list",
    "compute_scenario",
]

_TOKEN_FIELDS = ("tokens_input", "tokens_output", "tokens_cached")


@dataclass(frozen=True)
class ScenarioLevers:
    """The bounded what-if levers (10 volume_tenths means 100 percent)."""

    volume_tenths: int
    flat_rates: tuple = ()
    substitutions: tuple = ()


@dataclass(frozen=True)
class ScenarioResult:
    """Frozen scenario tables, the frozen costing report, and the digest."""

    customer_rows: tuple
    workload_rows: tuple
    unallocated_rows: tuple
    report: export_data.Record
    digest: str


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return export_data.Record(
            tuple((key, _freeze_value(value[key])) for key in sorted(value))
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _scaled(tokens: int | None, volume_tenths: int) -> int | None:
    if tokens is None:
        return None
    return (tokens * volume_tenths + 5) // 10


def build_engine_events(
    bundle: export_data.ExportBundle, levers: ScenarioLevers
) -> list[dict[str, Any]]:
    """Reconstruct engine-shaped events from the exported scenario rows."""
    price_doc = bundle.price_list.as_dict()
    providers = {entry["provider"] for entry in price_doc["entries"]}
    if len(providers) != 1:
        raise ValueError(
            f"expected exactly one provider in the exported price list, "
            f"found {sorted(providers)}"
        )
    provider = next(iter(providers))
    substitutions = dict(levers.substitutions)

    events: list[dict[str, Any]] = []
    for record in bundle.scenario_events:
        row = record.as_dict()
        payload_sha256 = hashlib.sha256(
            canonical_json(row).encode("utf-8")
        ).hexdigest()
        event: dict[str, Any] = {
            "provider": provider,
            "account_id": "synthetic-account",
            "provider_event_id": row["event_key"],
            "attempt_id": "1",
            "request_id": row["event_key"],
            "payload_sha256": payload_sha256,
            "customer_id": row["customer_id"],
            "feature": row["feature"],
            "model": substitutions.get(row["model"], row["model"]),
            "workload_type": row["workload_type"],
            "usage_time": row["usage_time"],
            "unavailable_fields": list(row["unavailable_fields"]),
            "record_version": row["record_version"],
            "supersedes": row["supersedes_event_key"],
        }
        for field in _TOKEN_FIELDS:
            event[field] = _scaled(row[field], levers.volume_tenths)
        events.append(event)
    return events


def build_price_list(
    bundle: export_data.ExportBundle, levers: ScenarioLevers
) -> dict[str, Any]:
    """The exported price document with any flat-rate overrides applied."""
    document = bundle.price_list.as_dict()
    overrides = {
        model: (input_rate, output_rate, cached_rate)
        for model, input_rate, output_rate, cached_rate in levers.flat_rates
    }
    for entry in document["entries"]:
        rates = overrides.get(entry["model"])
        if rates is not None:
            entry["rates_per_million"] = {
                "input": rates[0],
                "output": rates[1],
                "cached": rates[2],
            }
    return document


def compute_scenario(
    bundle: export_data.ExportBundle, levers: ScenarioLevers
) -> ScenarioResult:
    """Recompute the margin tables for ``levers`` through the engine."""
    events = build_engine_events(bundle, levers)
    price_list = pricing.parse_price_list(build_price_list(bundle, levers))
    rows, report = costing.cost_events(events, price_list)
    plans = revenue.parse_plans(bundle.plans.as_dict())
    result = aggregate.aggregate_rows(rows, events, plans, price_list.currency)
    return ScenarioResult(
        customer_rows=tuple(_freeze_value(row) for row in result.customer_margins),
        workload_rows=tuple(_freeze_value(row) for row in result.workload_margins),
        unallocated_rows=tuple(_freeze_value(row) for row in result.unallocated),
        report=_freeze_value(report),
        digest=digest.compute_digest(
            result.customer_margins, result.workload_margins, result.unallocated
        ),
    )
