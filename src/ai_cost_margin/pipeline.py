"""Phase 4 one-command runner: raw -> validated -> costed -> aggregated -> publish.

The runner is the single writer. It opens ``<home>/pipeline.duckdb`` once, takes
the DuckDB writer lock, performs the stale-run scan while holding that lock,
creates a new attempt row, adopts the longest verified committed prefix from an
interrupted attempt with the same ``invocation_sha256`` (A2/R5), runs every
missing stage in its own transaction, and publishes atomically (A1/A3).

Deterministic result bytes and the digest are the same as the Phase 3 legacy
path for the same inputs and cutoff. Attempt ids, invocation hashes,
timestamps, durations, publish state and the DuckDB file bytes are operational
metadata: they live in ``run_history`` and the JSON run log, never in the seven
published files.

The production CLI always probes git and has no code-version override flag. The
only environment hook is ``AI_COST_MARGIN_KILL_POINT``, read only by the drill:
it terminates the process with ``os._exit(137)`` at one of the three audited
points and is absent in every normal run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .aggregate import CompositionError, UnknownPlanError, aggregate_rows
from .costing import CostingError, LineageAmbiguityError, cost_events
from .digest import DIGEST_DEFINITION
from .ingest import IngestError, ingest
from .lineage import build_lineage
from .margins import (
    MarginInputError,
    PlanCurrencyMismatchError,
    assemble_artifact,
)
from .pricing import PriceScheduleError, load_price_list, parse_utc
from .revenue import PlanScheduleError, load_plans
from .runs import (
    CodeVersionError,
    OutputError,
    ReplayMismatchError,
    compute_run_fingerprint,
    hash_canonical,
    resolve_code_version,
    write_run_artifact,
)
from .runstore import (
    AGGREGATED_TABLES,
    COSTED_TABLES,
    PIPELINE_DB_NAME,
    PIPELINE_VERSION,
    VALIDATED_TABLES,
    PipelineBusyError,
    PipelineStateError,
    RunLogError,
    RunStore,
    StoreOpenError,
    connect_store,
    read_status,
)
from .store import StoreError, canonical_json, content_sha256

__all__ = [
    "SEVEN_FILES",
    "PipelineInputError",
    "build_parser",
    "main",
    "run_pipeline",
]

SEVEN_FILES = (
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "lineage.jsonl",
    "margin-report.json",
    "margin-digest.txt",
    "run-manifest.json",
)

LOG_SCHEMA_VERSION = "1.0"
KILL_POINTS = ("before_raw_commit", "before_costed_commit", "after_publish_rename")


class PipelineInputError(ValueError):
    """The command's inputs are unusable; nothing has been written."""


def _kill(point: str) -> None:
    """Immediate termination at an audited kill point (drill only)."""
    if os.environ.get("AI_COST_MARGIN_KILL_POINT") == point:
        os._exit(137)


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _counters(totals: dict[str, int]) -> dict[str, Any]:
    return {
        "received": totals["received"],
        "inserted": totals["inserted"],
        "duplicates": totals["duplicates"],
        "quarantined": totals["quarantined"],
        "dead_lettered": totals["dead_lettered"],
        "dangling_supersedes": totals["dangling_supersedes"],
        "records_validated": totals["validated"],
    }


def _load_meta(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None or not record.get("meta_json"):
        return {}
    return json.loads(record["meta_json"])


def _delivery_paths(entries: Sequence[Path]) -> list[Path]:
    paths: list[Path] = []
    for entry in entries:
        path = Path(entry)
        if path.is_dir():
            paths.extend(
                sorted(
                    item
                    for item in path.iterdir()
                    if item.is_file() and item.suffix == ".json"
                )
            )
        elif path.is_file():
            paths.append(path)
        else:
            raise PipelineInputError(f"batch path not found: {path}")
    if not paths:
        raise PipelineInputError("no input files were delivered")
    return paths


class _Runner:
    """One attempt's staged work against an already-locked run store."""

    def __init__(
        self,
        *,
        store: RunStore,
        home: Path,
        out_root: Path,
        prices: Path,
        plans: Path,
        price_list: Any,
        plan_list: Any,
        price_list_sha256: str,
        plans_sha256: str,
        cutoff: str,
        code_version: Any,
        deliveries: Sequence[tuple[Path, bytes]],
        delivery_hashes: Sequence[str],
        attempt_id: str,
        pre_raw_store_sha256: str,
        from_stage: str | None,
    ) -> None:
        self.store = store
        self.home = home
        self.out_root = out_root
        self.prices = prices
        self.plans = plans
        self.price_list = price_list
        self.plan_list = plan_list
        self.price_list_sha256 = price_list_sha256
        self.plans_sha256 = plans_sha256
        self.cutoff = cutoff
        self.code_version = code_version
        self.deliveries = deliveries
        self.delivery_hashes = delivery_hashes
        self.attempt_id = attempt_id
        self.pre_raw_store_sha256 = pre_raw_store_sha256
        self.from_stage = from_stage
        self.adopted: dict[str, dict[str, Any]] = {}
        self.fingerprint: str | None = None
        self.digest: str | None = None
        self.validated_output: str | None = None
        self.costed_output: str | None = None
        self.validated_meta: dict[str, Any] = {}
        self.costed_meta: dict[str, Any] = {}

    # ---- adoption (A2 / R5) ------------------------------------------
    def adopt(self, previous_id: str) -> None:
        current = self.store.current_store_digest()

        raw_record = self.store.stage_record(previous_id, "raw")
        if raw_record is None or raw_record["output_digest"] != current:
            # R5: a committed raw is adoptable only when the live global store
            # still equals its recorded post-raw digest. An external change
            # means adopt nothing and recompute raw against current state.
            return
        self.store.copy_stage_rows(previous_id, self.attempt_id, "raw")
        raw_meta = _load_meta(raw_record)
        self.store.write_run_stage(
            self.attempt_id,
            "raw",
            status="adopted",
            input_digest=raw_record["input_digest"] or "",
            output_digest=raw_record["output_digest"] or "",
            row_count=int(raw_record["row_count"] or 0),
            meta=raw_meta,
            duration_ms=0,
        )
        totals = raw_meta.get("counters", {})
        self.store.update_attempt(
            self.attempt_id,
            post_raw_store_sha256=current,
            schema_versions_seen=canonical_json(self.store.schema_versions_seen()),
            **_counters(totals),
        )
        self.adopted["raw"] = raw_meta
        if self.from_stage == "validated":
            return

        validated_record = self.store.stage_record(previous_id, "validated")
        if validated_record is None or validated_record["input_digest"] != current:
            return
        self.store.copy_stage_rows(previous_id, self.attempt_id, "validated")
        validated_output = self.store.stage_tables_digest(self.attempt_id, VALIDATED_TABLES)
        if validated_output != validated_record["output_digest"]:
            raise PipelineStateError("adopted validated stage does not re-verify")
        validated_meta = _load_meta(validated_record)
        self.store.write_run_stage(
            self.attempt_id,
            "validated",
            status="adopted",
            input_digest=validated_record["input_digest"] or "",
            output_digest=validated_output,
            row_count=int(validated_record["row_count"] or 0),
            meta=validated_meta,
            duration_ms=0,
        )
        self.fingerprint = validated_meta["run_fingerprint"]
        self.validated_meta = validated_meta
        self.validated_output = validated_output
        self.store.update_attempt(self.attempt_id, run_fingerprint=self.fingerprint)
        self.adopted["validated"] = validated_meta
        if self.from_stage == "costed":
            return

        costed_input = hash_canonical(
            {"validated_output": validated_output, "price_list_sha256": self.price_list_sha256}
        )
        costed_record = self.store.stage_record(previous_id, "costed")
        if costed_record is None or costed_record["input_digest"] != costed_input:
            return
        self.store.copy_stage_rows(previous_id, self.attempt_id, "costed")
        costed_output = self.store.stage_tables_digest(self.attempt_id, COSTED_TABLES)
        if costed_output != costed_record["output_digest"]:
            raise PipelineStateError("adopted costed stage does not re-verify")
        costed_meta = _load_meta(costed_record)
        self.store.write_run_stage(
            self.attempt_id,
            "costed",
            status="adopted",
            input_digest=costed_record["input_digest"] or "",
            output_digest=costed_output,
            row_count=int(costed_record["row_count"] or 0),
            meta=costed_meta,
            duration_ms=0,
        )
        costing_report = costed_meta.get("costing_report", {})
        self.store.update_attempt(
            self.attempt_id,
            records_read=costing_report.get("events_read"),
            records_unpriced=costing_report.get("events_unpriced"),
            price_versions_applied=canonical_json(costing_report.get("price_versions_used", [])),
        )
        self.costed_meta = costed_meta
        self.costed_output = costed_output
        self.adopted["costed"] = costed_meta
        if self.from_stage == "aggregated":
            return

        aggregated_input = hash_canonical(
            {"costed_output": costed_output, "plans_sha256": self.plans_sha256}
        )
        aggregated_record = self.store.stage_record(previous_id, "aggregated")
        if aggregated_record is None or aggregated_record["input_digest"] != aggregated_input:
            return
        self.store.copy_stage_rows(previous_id, self.attempt_id, "aggregated")
        aggregated_output = self._aggregated_digest(self.attempt_id)
        if aggregated_output != aggregated_record["output_digest"]:
            raise PipelineStateError("adopted aggregated stage does not re-verify")
        aggregated_meta = _load_meta(aggregated_record)
        self.store.write_run_stage(
            self.attempt_id,
            "aggregated",
            status="adopted",
            input_digest=aggregated_record["input_digest"] or "",
            output_digest=aggregated_output,
            row_count=int(aggregated_record["row_count"] or 0),
            meta=aggregated_meta,
            duration_ms=0,
        )
        self.digest = aggregated_meta["digest"]
        self.adopted["aggregated"] = aggregated_meta

    # ---- stages -------------------------------------------------------
    def run_raw(self) -> None:
        start = time.perf_counter()
        input_digest = hash_canonical(
            {
                "deliveries": list(self.delivery_hashes),
                "pre_raw_store_sha256": self.pre_raw_store_sha256,
            }
        )
        self.store.begin()
        try:
            self.store.update_attempt(self.attempt_id, current_stage="raw")
            report = ingest(
                [path for path, _ in self.deliveries],
                self.home,
                adapter=self.store.adapter(self.attempt_id),
            )
            post_raw = self.store.current_store_digest()
            _kill("before_raw_commit")
            meta = {
                "ingest_id": report.ingest_id,
                "counters": dict(report.totals),
                "post_raw_store_sha256": post_raw,
            }
            self.store.write_run_stage(
                self.attempt_id,
                "raw",
                status="committed",
                input_digest=input_digest,
                output_digest=post_raw,
                row_count=int(report.totals["received"]),
                meta=meta,
                duration_ms=_elapsed_ms(start),
            )
            self.store.update_attempt(
                self.attempt_id,
                post_raw_store_sha256=post_raw,
                schema_versions_seen=canonical_json(self.store.schema_versions_seen()),
                **_counters(report.totals),
            )
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise

    def run_validated(self) -> None:
        start = time.perf_counter()
        input_digest = self.store.current_store_digest()
        self.store.begin()
        try:
            self.store.update_attempt(self.attempt_id, current_stage="validated")
            events = self.store.load_events()
            try:
                lineage = build_lineage(events, self.cutoff)
            except ValueError as exc:
                raise MarginInputError(f"invalid cutoff or stored timestamp: {exc}") from exc
            fingerprint = compute_run_fingerprint(
                included_events=lineage.events,
                cutoff=self.cutoff,
                price_list_sha256=self.price_list_sha256,
                plans_sha256=self.plans_sha256,
                code_version=self.code_version,
            )
            self.store.replace_stage_rows(self.attempt_id, "stage_validated", lineage.events)
            self.store.replace_stage_rows(self.attempt_id, "stage_lineage", lineage.edges)
            output_digest = self.store.stage_tables_digest(self.attempt_id, VALIDATED_TABLES)
            meta = {
                "events_in_store": len(events),
                "events_included": len(lineage.events),
                "events_excluded_by_cutoff": lineage.excluded_by_cutoff,
                "dangling_supersedes": lineage.dangling_supersedes,
                "run_fingerprint": fingerprint,
            }
            self.store.write_run_stage(
                self.attempt_id,
                "validated",
                status="committed",
                input_digest=input_digest,
                output_digest=output_digest,
                row_count=len(lineage.events),
                meta=meta,
                duration_ms=_elapsed_ms(start),
            )
            self.store.update_attempt(self.attempt_id, run_fingerprint=fingerprint)
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        self.fingerprint = fingerprint
        self.validated_meta = meta
        self.validated_output = output_digest

    def run_costed(self) -> None:
        start = time.perf_counter()
        input_digest = hash_canonical(
            {
                "validated_output": self.validated_output,
                "price_list_sha256": self.price_list_sha256,
            }
        )
        self.store.begin()
        try:
            self.store.update_attempt(self.attempt_id, current_stage="costed")
            events = self.store.read_stage_rows(self.attempt_id, "stage_validated")
            rows, report = cost_events(events, self.price_list)
            self.store.replace_stage_rows(self.attempt_id, "stage_costed", rows)
            output_digest = self.store.stage_tables_digest(self.attempt_id, COSTED_TABLES)
            meta = {"costing_report": report}
            self.store.write_run_stage(
                self.attempt_id,
                "costed",
                status="committed",
                input_digest=input_digest,
                output_digest=output_digest,
                row_count=len(rows),
                meta=meta,
                duration_ms=_elapsed_ms(start),
            )
            self.store.update_attempt(
                self.attempt_id,
                records_read=report["events_read"],
                records_unpriced=report["events_unpriced"],
                price_versions_applied=canonical_json(report["price_versions_used"]),
            )
            _kill("before_costed_commit")
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        self.costed_meta = meta
        self.costed_output = output_digest

    def _aggregated_digest(self, attempt_id: str) -> str:
        table_digests = {
            table: self.store.stage_tables_digest(attempt_id, [table])
            for table in (
                "stage_customer_margins",
                "stage_workload_margins",
                "stage_unallocated",
            )
        }
        return hash_canonical(
            {"tables": table_digests, "artifacts": self.store.artifact_sha_map(attempt_id)}
        )

    def run_aggregated(self) -> None:
        start = time.perf_counter()
        input_digest = hash_canonical(
            {"costed_output": self.costed_output, "plans_sha256": self.plans_sha256}
        )
        self.store.begin()
        try:
            self.store.update_attempt(self.attempt_id, current_stage="aggregated")
            events = self.store.read_stage_rows(self.attempt_id, "stage_validated")
            costed_rows = self.store.read_stage_rows(self.attempt_id, "stage_costed")
            edges = self.store.read_stage_rows(self.attempt_id, "stage_lineage")
            aggregated = aggregate_rows(
                costed_rows, events, self.plan_list, self.price_list.currency
            )
            stored_events = self.store.load_events()
            artifact = assemble_artifact(
                included_events=events,
                edges=edges,
                excluded_by_cutoff=int(self.validated_meta["events_excluded_by_cutoff"]),
                dangling_supersedes=int(self.validated_meta["dangling_supersedes"]),
                stored_events_count=len(stored_events),
                costing_report=self.costed_meta["costing_report"],
                customer_margins=aggregated.customer_margins,
                workload_margins=aggregated.workload_margins,
                unallocated=aggregated.unallocated,
                metrics=aggregated.metrics,
                price_list=self.price_list,
                plan_list=self.plan_list,
                prices=self.prices,
                plans=self.plans,
                price_list_sha256=self.price_list_sha256,
                plans_sha256=self.plans_sha256,
                cutoff=self.cutoff,
                code_version=self.code_version,
                manifest_store_inputs={
                    "store_backend": "duckdb",
                    "store_events_sha256": self.store.current_store_digest(),
                    "store_locator": PIPELINE_DB_NAME,
                    "events_in_store": len(stored_events),
                    "events_included": len(events),
                    "events_excluded_by_cutoff": int(
                        self.validated_meta["events_excluded_by_cutoff"]
                    ),
                },
            )
            if artifact["run_fingerprint"] != self.fingerprint:
                raise PipelineStateError("aggregated fingerprint differs from validated fingerprint")
            self.store.replace_stage_rows(
                self.attempt_id, "stage_customer_margins", aggregated.customer_margins
            )
            self.store.replace_stage_rows(
                self.attempt_id, "stage_workload_margins", aggregated.workload_margins
            )
            self.store.replace_stage_rows(
                self.attempt_id, "stage_unallocated", aggregated.unallocated
            )
            self.store.write_artifacts(self.attempt_id, artifact["files"])
            output_digest = self._aggregated_digest(self.attempt_id)
            metrics = aggregated.metrics
            meta = {
                "digest": artifact["digest"],
                "metrics": metrics,
                "artifact_sha256": {
                    name: content_sha256(data) for name, data in artifact["files"].items()
                },
            }
            self.store.write_run_stage(
                self.attempt_id,
                "aggregated",
                status="committed",
                input_digest=input_digest,
                output_digest=output_digest,
                row_count=len(aggregated.customer_margins)
                + len(aggregated.workload_margins)
                + len(aggregated.unallocated),
                meta=meta,
                duration_ms=_elapsed_ms(start),
            )
            self.store.update_attempt(
                self.attempt_id,
                records_unallocated=metrics["unallocated_events_costed"]
                + metrics["unallocated_events_unpriced"],
            )
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        self.digest = artifact["digest"]

    # ---- publish (A1 / A3) -------------------------------------------
    def publish(self) -> dict[str, Any]:
        files = self.store.read_artifacts(self.attempt_id)
        if set(files) != set(SEVEN_FILES):
            raise PipelineStateError("aggregated stage is missing published artifact files")
        ordered = {name: files[name] for name in SEVEN_FILES}
        write_result = write_run_artifact(
            self.out_root,
            self.fingerprint or "",
            ordered,
            attempt_token=self.attempt_id,
        )
        _kill("after_publish_rename")
        self.store.begin()
        try:
            self.store.publish(
                self.attempt_id,
                run_fingerprint=self.fingerprint or "",
                digest=self.digest or "",
                artifact_path=write_result.run_dir.name,
                digest_definition=DIGEST_DEFINITION,
            )
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        return {
            "attempt_id": self.attempt_id,
            "run_fingerprint": self.fingerprint,
            "digest": self.digest,
            "run_dir": write_result.run_dir,
            "replayed": write_result.replayed,
        }


def run_pipeline(
    *,
    home: Path,
    batches: Sequence[Path],
    prices: Path,
    plans: Path,
    cutoff: str,
    out_root: Path,
    from_stage: str | None = None,
    no_resume: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Run the whole pipeline once and return the published run summary."""
    home = Path(home)
    out_root = Path(out_root)
    prices = Path(prices)
    plans = Path(plans)

    delivery_paths = _delivery_paths(batches)
    deliveries: list[tuple[Path, bytes]] = []
    for path in delivery_paths:
        try:
            deliveries.append((path, path.read_bytes()))
        except OSError as exc:
            raise PipelineInputError(f"cannot read {path}: {exc}") from exc
    delivery_hashes = [content_sha256(data) for _, data in deliveries]

    try:
        parse_utc(cutoff)
    except ValueError as exc:
        raise PipelineInputError(f"invalid cutoff: {exc}") from exc

    price_list = load_price_list(prices)
    plan_list = load_plans(plans)
    if plan_list.currency != price_list.currency:
        raise PlanCurrencyMismatchError(
            f"plan currency {plan_list.currency!r} does not match "
            f"price-list currency {price_list.currency!r}"
        )
    price_hash = content_sha256(prices.read_bytes())
    plans_hash = content_sha256(plans.read_bytes())
    code_version = resolve_code_version(repo_root)

    invocation_sha256 = hash_canonical(
        {
            "pipeline_version": PIPELINE_VERSION,
            "deliveries": delivery_hashes,
            "price_list_sha256": price_hash,
            "plans_sha256": plans_hash,
            "as_of_cutoff": cutoff,
            "code_version": code_version.code_version,
            "code_version_source": code_version.code_version_source,
            "working_tree_clean": code_version.working_tree_clean,
            "dirty_status_sha256": code_version.dirty_status_sha256,
        }
    )

    home.mkdir(parents=True, exist_ok=True)
    db_path = home / PIPELINE_DB_NAME
    conn = connect_store(db_path)
    store = RunStore(conn, home, db_path)
    store.initialize()
    attempt_id: str | None = None
    try:
        store.begin()
        interrupted = store.stale_scan()
        store.commit()
        for item in interrupted:
            store.append_log(
                {
                    "log_schema_version": LOG_SCHEMA_VERSION,
                    "event": "run_interrupted_detected",
                    "attempt_id": item["attempt_id"],
                    "last_completed_stage": item["last_completed_stage"],
                }
            )

        pre_raw = store.current_store_digest()
        store.begin()
        attempt_id = store.start_attempt(
            invocation_sha256=invocation_sha256,
            pipeline_version=PIPELINE_VERSION,
            code_version=code_version,
            cutoff=cutoff,
            price_list_sha256=price_hash,
            plans_sha256=plans_hash,
            pre_raw_store_sha256=pre_raw,
        )
        store.commit()

        runner = _Runner(
            store=store,
            home=home,
            out_root=out_root,
            prices=prices,
            plans=plans,
            price_list=price_list,
            plan_list=plan_list,
            price_list_sha256=price_hash,
            plans_sha256=plans_hash,
            cutoff=cutoff,
            code_version=code_version,
            deliveries=deliveries,
            delivery_hashes=delivery_hashes,
            attempt_id=attempt_id,
            pre_raw_store_sha256=pre_raw,
            from_stage=from_stage,
        )

        try:
            if not no_resume:
                previous = store.find_resumable(invocation_sha256)
                if previous is not None:
                    runner.adopt(previous)
            if "raw" not in runner.adopted:
                runner.run_raw()
            if "validated" not in runner.adopted:
                runner.run_validated()
            if "costed" not in runner.adopted:
                runner.run_costed()
            if "aggregated" not in runner.adopted:
                runner.run_aggregated()
            result = runner.publish()
        except Exception as exc:
            store.rollback()
            store.begin()
            store.fail_attempt(attempt_id, f"{type(exc).__name__}: {exc}")
            store.commit()
            try:
                store.append_log(
                    {
                        "log_schema_version": LOG_SCHEMA_VERSION,
                        "event": "run_failed",
                        "attempt_id": attempt_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            except RunLogError:
                pass
            raise

        # R8: the publish record is already committed. A log IO failure here is
        # a typed exit-2 error and never rolls the published record back.
        store.append_log(
            {
                "log_schema_version": LOG_SCHEMA_VERSION,
                "event": "run_completed",
                "attempt_id": attempt_id,
                "run_fingerprint": result["run_fingerprint"],
                "digest": result["digest"],
                "replayed": result["replayed"],
            }
        )
        return result
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_cost_margin.pipeline",
        description="Run the Phase 4 raw -> validated -> costed -> aggregated pipeline.",
    )
    parser.add_argument("--home", required=True, type=Path, help="working store root, e.g. var")
    parser.add_argument(
        "--batches",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="batch file or directory of top-level *.json batches; repeatable, in delivery order",
    )
    parser.add_argument("--prices", type=Path, help="price-list JSON file")
    parser.add_argument("--plans", type=Path, help="simulated customer-plan JSON file")
    parser.add_argument("--cutoff", help="as-of UTC cutoff, inclusive")
    parser.add_argument("--out", type=Path, help="artifact root; a fingerprint subdirectory is published")
    parser.add_argument(
        "--from-stage",
        choices=("validated", "costed", "aggregated"),
        default=None,
        help="reuse verified checkpoints before this stage; recompute from it",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="do not adopt an interrupted attempt's committed prefix",
    )
    parser.add_argument(
        "--status-json",
        action="store_true",
        help="print the published pointer and attempt rows, then exit",
    )
    return parser


def _typed_errors() -> tuple[type[BaseException], ...]:
    return (
        PipelineInputError,
        PipelineBusyError,
        StoreOpenError,
        PipelineStateError,
        RunLogError,
        StoreError,
        IngestError,
        PriceScheduleError,
        PlanScheduleError,
        MarginInputError,
        PlanCurrencyMismatchError,
        UnknownPlanError,
        CompositionError,
        LineageAmbiguityError,
        CostingError,
        CodeVersionError,
        OutputError,
        ReplayMismatchError,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.status_json:
        try:
            status = read_status(Path(args.home) / PIPELINE_DB_NAME)
        except (PipelineBusyError, StoreOpenError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(canonical_json(status))
        return 0

    missing = [
        name
        for name in ("prices", "plans", "cutoff", "out")
        if getattr(args, name) is None
    ]
    if not args.batches or missing:
        print(
            "error: --batches, --prices, --plans, --cutoff and --out are required",
            file=sys.stderr,
        )
        return 2

    try:
        result = run_pipeline(
            home=args.home,
            batches=args.batches,
            prices=args.prices,
            plans=args.plans,
            cutoff=args.cutoff,
            out_root=args.out,
            from_stage=args.from_stage,
            no_resume=args.no_resume,
        )
    except _typed_errors() as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"run {result['attempt_id']} fingerprint {result['run_fingerprint']} "
        f"-> {result['run_dir'].name}"
    )
    print(f"  digest={result['digest']} replayed={str(result['replayed']).lower()}")
    print(
        "run_result "
        + canonical_json(
            {
                "attempt_id": result["attempt_id"],
                "run_fingerprint": result["run_fingerprint"],
                "digest": result["digest"],
                "run_dir_name": result["run_dir"].name,
                "replayed": result["replayed"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
