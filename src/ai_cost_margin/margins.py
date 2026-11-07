"""Phase 3 margin run: cutoff -> Phase 2 costing -> aggregation -> artifact.

The production CLI has no code-version override: it always probes git. The
internal ``code_version_override`` parameter exists only for tests and the
explicit golden-generation invocation.

Validation and all output content are computed before any write. Validation
errors therefore write nothing. A mid-write OS failure can leave a partial run
directory; the next identical run surfaces it as exit 2 through the byte-compare
replay check. Atomic publish remains M4 scope.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .aggregate import CompositionError, UnknownPlanError, aggregate_rows
from .costing import CostingError, LineageAmbiguityError, cost_events
from .digest import (
    AGGREGATION_DEFINITION_ID,
    ARTIFACT_SCHEMA_VERSION,
    DIGEST_DEFINITION,
    compute_digest,
    digest_text,
    render_table,
)
from .lineage import build_lineage
from .pricing import PriceScheduleError, load_price_list
from .revenue import PlanScheduleError, load_plans
from .runs import (
    CodeVersionError,
    OutputError,
    ReplayMismatchError,
    compute_run_fingerprint,
    fixture_code_version,
    resolve_code_version,
    write_run_artifact,
)
from .store import Store, StoreError, canonical_json, content_sha256, pretty_json, read_jsonl

__all__ = [
    "MarginInputError",
    "PlanCurrencyMismatchError",
    "assemble_artifact",
    "build_parser",
    "main",
    "run_margins",
]


class MarginInputError(ValueError):
    """A run input is unusable."""


class PlanCurrencyMismatchError(ValueError):
    """The simulated plan currency differs from the price-list currency."""


def _json_lines(rows: Sequence[dict[str, Any]]) -> bytes:
    return "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")


def assemble_artifact(
    *,
    included_events: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
    excluded_by_cutoff: int,
    dangling_supersedes: int,
    stored_events_count: int,
    costing_report: dict[str, Any],
    customer_margins: Sequence[dict[str, Any]],
    workload_margins: Sequence[dict[str, Any]],
    unallocated: Sequence[dict[str, Any]],
    metrics: dict[str, Any],
    price_list: Any,
    plan_list: Any,
    prices: Path,
    plans: Path,
    price_list_sha256: str,
    plans_sha256: str,
    cutoff: str,
    code_version: Any,
    manifest_store_inputs: dict[str, Any],
) -> dict[str, Any]:
    """Compute the digest, report, manifest and seven files for one run.

    Compute and write are separate (Phase 4). The legacy ``run_margins`` path
    passes its JSONL store fields as ``manifest_store_inputs``; the Phase 4
    runner passes the backend-neutral A7 fields. Everything else is shared, so
    the digest definition and the financial tables cannot drift between them.
    """
    digest = compute_digest(customer_margins, workload_margins, unallocated)
    fingerprint = compute_run_fingerprint(
        included_events=included_events,
        cutoff=cutoff,
        price_list_sha256=price_list_sha256,
        plans_sha256=plans_sha256,
        code_version=code_version,
    )

    table_bytes = {
        "customer_margins": render_table("customer_margins", customer_margins),
        "workload_margins": render_table("workload_margins", workload_margins),
        "unallocated": render_table("unallocated", unallocated),
        "lineage": _json_lines(edges),
    }
    tables = {
        "customer_margins": {
            "file": "customer-margins.jsonl",
            "rows": len(customer_margins),
            "sha256": content_sha256(table_bytes["customer_margins"]),
        },
        "workload_margins": {
            "file": "workload-margins.jsonl",
            "rows": len(workload_margins),
            "sha256": content_sha256(table_bytes["workload_margins"]),
        },
        "unallocated": {
            "file": "unallocated.jsonl",
            "rows": len(unallocated),
            "sha256": content_sha256(table_bytes["unallocated"]),
        },
        "lineage": {
            "file": "lineage.jsonl",
            "rows": len(edges),
            "sha256": content_sha256(table_bytes["lineage"]),
        },
    }

    shared = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "aggregation_definition_id": AGGREGATION_DEFINITION_ID,
        "run_fingerprint": fingerprint,
        "as_of_cutoff": cutoff,
        **code_version.as_dict(),
        "digest": digest,
        "digest_definition": DIGEST_DEFINITION,
        "tables": tables,
    }
    report = {
        **shared,
        "periods": metrics["periods"],
        "currency": price_list.currency,
        "cost_label": "estimated",
        "revenue_label": "simulated",
        "margin_label": "simulated",
        "usage_label": "synthetic",
        "price_list_id": price_list.price_list_id,
        "price_list_label": price_list.label,
        "price_list_sha256": price_list_sha256,
        "plans_version_id": plan_list.plans_version_id,
        "plans_label": plan_list.label,
        "plans_sha256": plans_sha256,
        "revenue_model_id": "flat-monthly-plan/v1",
        "revenue_allocation_basis": "tokens_input_plus_output",
        "events_in_store": stored_events_count,
        "events_excluded_by_cutoff": excluded_by_cutoff,
        "events_read": costing_report["events_read"],
        "events_costed": costing_report["events_costed"],
        "events_unpriced": costing_report["events_unpriced"],
        "events_superseded": costing_report["events_superseded"],
        "unpriced_by_reason": costing_report["unpriced_by_reason"],
        "price_versions_used": costing_report["price_versions_used"],
        "superseded_events_outside_active_cells": metrics["superseded_events_outside_active_cells"],
        "unallocated_events_costed": metrics["unallocated_events_costed"],
        "unallocated_events_unpriced": metrics["unallocated_events_unpriced"],
        "unallocated_events_superseded": metrics["unallocated_events_superseded"],
        "unallocated_cost_units": metrics["unallocated_cost_units"],
        "total_cost_units": metrics["total_cost_units"],
        "unallocated_cost_share_bp": metrics["unallocated_cost_share_bp"],
        "unallocated_event_share_bp": metrics["unallocated_event_share_bp"],
        "customer_rows": metrics["customer_rows"],
        "workload_rows": metrics["workload_rows"],
        "workload_rows_allocated": metrics["workload_rows_allocated"],
        "workload_rows_incomplete_unpriced_basis": metrics["workload_rows_incomplete_unpriced_basis"],
        "workload_rows_basis_unavailable": metrics["workload_rows_basis_unavailable"],
        "incomplete_customer_margins": metrics["incomplete_customer_margins"],
        "incomplete_workload_margins": metrics["incomplete_workload_margins"],
        "dangling_supersedes": dangling_supersedes,
        "composition_status": "verified",
    }
    manifest = {
        **shared,
        "periods": metrics["periods"],
        "inputs": {
            "price_list_file": prices.name,
            "price_list_id": price_list.price_list_id,
            "price_list_sha256": price_list_sha256,
            "plans_file": plans.name,
            "plans_version_id": plan_list.plans_version_id,
            "plans_label": plan_list.label,
            "plans_sha256": plans_sha256,
            **manifest_store_inputs,
        },
        "labels": {
            "usage": "synthetic",
            "cost": "estimated",
            "revenue": "simulated",
            "margin": "simulated",
        },
    }

    files = {
        "customer-margins.jsonl": table_bytes["customer_margins"],
        "workload-margins.jsonl": table_bytes["workload_margins"],
        "unallocated.jsonl": table_bytes["unallocated"],
        "lineage.jsonl": table_bytes["lineage"],
        "margin-report.json": (pretty_json(report) + "\n").encode("utf-8"),
        "margin-digest.txt": digest_text(digest),
        "run-manifest.json": (pretty_json(manifest) + "\n").encode("utf-8"),
    }
    return {
        "run_fingerprint": fingerprint,
        "digest": digest,
        "report": report,
        "manifest": manifest,
        "files": files,
        "table_bytes": table_bytes,
        "tables": tables,
    }


def run_margins(
    *,
    home: Path,
    prices: Path,
    plans: Path,
    cutoff: str,
    out_root: Path,
    code_version_override: str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Run margins and return the run summary, including provenance and digest."""
    home = Path(home)
    prices = Path(prices)
    plans = Path(plans)
    out_root = Path(out_root)

    price_list = load_price_list(prices)
    plan_list = load_plans(plans)
    if plan_list.currency != price_list.currency:
        raise PlanCurrencyMismatchError(
            f"plan currency {plan_list.currency!r} does not match price-list currency {price_list.currency!r}"
        )

    events_path = Store(home).events_path
    stored_events = read_jsonl(events_path)
    try:
        lineage = build_lineage(stored_events, cutoff)
    except ValueError as exc:
        raise MarginInputError(f"invalid cutoff or stored timestamp: {exc}") from exc

    costed_rows, costing_report = cost_events(lineage.events, price_list)
    aggregated = aggregate_rows(costed_rows, lineage.events, plan_list, price_list.currency)

    price_list_hash = content_sha256(prices.read_bytes())
    plans_hash = content_sha256(plans.read_bytes())
    store_hash = content_sha256(events_path.read_bytes()) if events_path.exists() else content_sha256(b"")

    if code_version_override is not None:
        code_version = fixture_code_version(code_version_override)
    else:
        code_version = resolve_code_version(repo_root)

    artifact = assemble_artifact(
        included_events=lineage.events,
        edges=lineage.edges,
        excluded_by_cutoff=lineage.excluded_by_cutoff,
        dangling_supersedes=lineage.dangling_supersedes,
        stored_events_count=len(stored_events),
        costing_report=costing_report,
        customer_margins=aggregated.customer_margins,
        workload_margins=aggregated.workload_margins,
        unallocated=aggregated.unallocated,
        metrics=aggregated.metrics,
        price_list=price_list,
        plan_list=plan_list,
        prices=prices,
        plans=plans,
        price_list_sha256=price_list_hash,
        plans_sha256=plans_hash,
        cutoff=cutoff,
        code_version=code_version,
        manifest_store_inputs={
            "store_events_file": events_path.name,
            "store_events_sha256": store_hash,
            "events_in_store": len(stored_events),
            "events_included": len(lineage.events),
            "events_excluded_by_cutoff": lineage.excluded_by_cutoff,
        },
    )
    write_result = write_run_artifact(
        out_root, artifact["run_fingerprint"], artifact["files"]
    )
    return {
        "run_dir": write_result.run_dir,
        "run_fingerprint": artifact["run_fingerprint"],
        "replayed": write_result.replayed,
        "digest": artifact["digest"],
        "report": artifact["report"],
        "manifest": artifact["manifest"],
        "files": artifact["files"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_cost_margin.margins",
        description="Aggregate costed events into simulated customer/workload margins.",
    )
    parser.add_argument("--home", required=True, type=Path, help="private store root, e.g. var")
    parser.add_argument("--prices", required=True, type=Path, help="price-list JSON file")
    parser.add_argument("--plans", required=True, type=Path, help="simulated customer-plan JSON file")
    parser.add_argument("--cutoff", required=True, help="as-of UTC cutoff, inclusive")
    parser.add_argument("--out", required=True, type=Path, help="run root; a fingerprint subdirectory is created")
    return parser


def _nullable(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_margins(
            home=args.home,
            prices=args.prices,
            plans=args.plans,
            cutoff=args.cutoff,
            out_root=args.out,
        )
    except PriceScheduleError as exc:
        print(f"error: price schedule is invalid ({len(exc.violations)} violation(s)):", file=sys.stderr)
        for violation in exc.violations:
            print(f"  {violation}", file=sys.stderr)
        return 2
    except PlanScheduleError as exc:
        print(f"error: plan schedule is invalid ({len(exc.violations)} violation(s)):", file=sys.stderr)
        for violation in exc.violations:
            print(f"  {violation}", file=sys.stderr)
        return 2
    except (
        MarginInputError,
        PlanCurrencyMismatchError,
        UnknownPlanError,
        CompositionError,
        LineageAmbiguityError,
        CostingError,
        StoreError,
        CodeVersionError,
        OutputError,
        ReplayMismatchError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = result["report"]
    print(f"margin run {result['run_fingerprint']} -> {result['run_dir'].name}")
    print(
        f"  events_read={report['events_read']} costed={report['events_costed']} "
        f"unpriced={report['events_unpriced']} superseded={report['events_superseded']} "
        f"unallocated_costed={report['unallocated_events_costed']} "
        f"unallocated_cost_share_bp={report['unallocated_cost_share_bp']}"
    )
    print(
        f"  customer_rows={report['customer_rows']} workload_rows={report['workload_rows']} "
        f"incomplete_customers={report['incomplete_customer_margins']} "
        f"incomplete_workloads={report['incomplete_workload_margins']}"
    )
    print(f"  digest={result['digest']}")
    print(
        f"  code_version_source={report['code_version_source']} "
        f"code_version={report['code_version']}"
    )
    print(
        f"  working_tree_clean={_nullable(report['working_tree_clean'])} "
        f"dirty_status_sha256={_nullable(report['dirty_status_sha256'])}"
    )
    if result["replayed"]:
        print("  exact replay verified; no files overwritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
