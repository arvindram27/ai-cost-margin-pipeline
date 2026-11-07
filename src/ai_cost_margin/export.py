"""Sanitized export generator for Phase 5 (P5-C1).

The generator turns ONE verified published Phase 4 run into the committed,
allowlisted export the local app will read. Its input is both the published
artifact files under ``<runs>/<run_fingerprint>/`` and the published attempt's
stage rows in the private DuckDB store, bound by six consistency checks. Every
check runs before any write; a failure is a typed exit 2 with nothing written.

The store at ``--home`` is opened read-only and read inside one explicit read
transaction. This module never writes to the store, not even metadata.

Two digests are recorded and kept strictly distinct:

* the attested source financial digest (``ai-cost-margin/margin-digest/v1``),
  verified against the complete published source before any projection;
* the export integrity digest (``ai-cost-margin/export-integrity/v1``) over
  the actual sanitized export bytes, recomputable by the app and the tests.

The export carries no raw provider event IDs, account IDs, request IDs,
provider attempt IDs or payload hashes. Event identity inside the export is a
surrogate ``evt-NNNN`` key assigned in the published attempt's stage order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import duckdb

from .digest import DIGEST_DEFINITION, compute_digest
from .runstore import PIPELINE_DB_NAME, event_stream_digest
from .store import canonical_json, content_sha256, pretty_json

__all__ = [
    "DATA_FILES",
    "EXPORT_INTEGRITY_DEFINITION",
    "EXPORT_SCHEMA_VERSION",
    "ExportError",
    "build_parser",
    "export_integrity_digest",
    "generate_export",
    "main",
]

EXPORT_SCHEMA_VERSION = "1.0"
EXPORT_INTEGRITY_DEFINITION = "ai-cost-margin/export-integrity/v1"

SEVEN_FILES = (
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "lineage.jsonl",
    "margin-report.json",
    "margin-digest.txt",
    "run-manifest.json",
)
TABLE_FILES = ("customer-margins.jsonl", "workload-margins.jsonl", "unallocated.jsonl")
DATA_FILES = TABLE_FILES + (
    "scenario-events.jsonl",
    "lineage.jsonl",
    "price-list.json",
    "plans.json",
)

BANNED_KEYS = (
    b"provider_event_id",
    b"account_id",
    b"request_id",
    b"attempt_id",
    b"payload_sha256",
)

_CUSTOMER_ID_PATTERN = re.compile(r"^cust-\d{3}$")

_CUSTOMER_FIELDS = frozenset(
    {
        "cost_label",
        "currency",
        "customer_id",
        "estimated_cost",
        "events_costed",
        "events_superseded",
        "events_unpriced",
        "margin_label",
        "margin_status",
        "period",
        "plan_id",
        "plans_version_id",
        "revenue_label",
        "simulated_margin",
        "simulated_revenue",
        "unpriced_inconsistent_token_counts",
        "unpriced_model_not_in_price_list",
        "unpriced_no_applicable_rate",
        "unpriced_token_count_unavailable",
        "usage_label",
    }
)
_WORKLOAD_FIELDS = _CUSTOMER_FIELDS | frozenset(
    {"revenue_allocation_basis", "revenue_allocation_status", "workload_type"}
)
_UNALLOCATED_FIELDS = frozenset(
    {
        "bucket",
        "cost_label",
        "currency",
        "customer_id",
        "estimated_cost",
        "events_costed",
        "events_superseded",
        "events_unpriced",
        "period",
        "unpriced_inconsistent_token_counts",
        "unpriced_model_not_in_price_list",
        "unpriced_no_applicable_rate",
        "unpriced_token_count_unavailable",
        "usage_label",
        "workload_type",
    }
)
_TABLE_FIELDS = {
    "customer-margins.jsonl": _CUSTOMER_FIELDS,
    "workload-margins.jsonl": _WORKLOAD_FIELDS,
    "unallocated.jsonl": _UNALLOCATED_FIELDS,
}
_TABLE_KEYS = {
    "customer-margins.jsonl": "customer_margins",
    "workload-margins.jsonl": "workload_margins",
    "unallocated.jsonl": "unallocated",
}


class ExportError(ValueError):
    """The published source cannot be verified; nothing is written."""


def _read_bytes(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ExportError(f"cannot read {what} ({path.name}): {exc}") from exc


def _parse_jsonl(data: bytes, what: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExportError(f"{what} is not valid UTF-8: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line == "":
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExportError(f"{what} contains an invalid JSON line: {exc}") from exc
        if not isinstance(row, dict):
            raise ExportError(f"{what} contains a non-object JSON line")
        rows.append(row)
    return rows


def _parse_json(data: bytes, what: str) -> dict[str, Any]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ExportError(f"{what} is not a JSON object")
    return document


def _check_financial_guard(name: str, rows: Sequence[dict[str, Any]]) -> None:
    expected = _TABLE_FIELDS[name]
    for row in rows:
        if set(row) != set(expected):
            raise ExportError(
                f"{name} row fields do not match the allowlist: "
                f"missing={sorted(set(expected) - set(row))} extra={sorted(set(row) - set(expected))}"
            )
        for value in row.values():
            if value is not None and type(value) not in (str, int):
                raise ExportError(f"{name} contains a non string/integer/null value")
        customer_id = row.get("customer_id")
        if customer_id is not None and (
            not isinstance(customer_id, str)
            or _CUSTOMER_ID_PATTERN.match(customer_id) is None
        ):
            raise ExportError(f"{name} contains a non-synthetic customer_id: {customer_id!r}")


def export_integrity_digest(files: dict[str, dict[str, Any]]) -> str:
    """SHA-256 over the integrity stream of the seven data files."""
    chunks = [f"{EXPORT_INTEGRITY_DEFINITION}\n"]
    for name in sorted(files):
        entry = files[name]
        chunks.append(f"file {name} {entry['rows']} {entry['sha256']}\n")
    return hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()


def _store_snapshot(db_path: Path, attempt_id_holder: dict[str, Any]) -> dict[str, Any]:
    """Collect every store input inside one explicit read transaction."""
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except (duckdb.Error, OSError) as exc:
        raise ExportError(f"cannot open store read-only ({db_path.name}): {exc}") from exc
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            pointer = conn.execute(
                "SELECT attempt_id, run_fingerprint, digest, artifact_path "
                "FROM published_run WHERE singleton = 1"
            ).fetchone()
            if pointer is None:
                raise ExportError("the store has no published run")
            attempt_id = str(pointer[0])
            attempt_id_holder["attempt_id"] = attempt_id

            history = conn.execute(
                "SELECT status, run_fingerprint, digest, dead_lettered, quarantined, duplicates "
                "FROM run_history WHERE attempt_id = ?",
                [attempt_id],
            ).fetchone()
            if history is None:
                raise ExportError("the published attempt has no run_history row")

            event_rows = conn.execute(
                "SELECT event_json FROM validated_events"
            ).fetchall()
            validated_rows = conn.execute(
                "SELECT row_json FROM stage_validated WHERE attempt_id = ? ORDER BY ordinal",
                [attempt_id],
            ).fetchall()
            lineage_rows = conn.execute(
                "SELECT row_json FROM stage_lineage WHERE attempt_id = ? ORDER BY ordinal",
                [attempt_id],
            ).fetchall()
            artifact_rows = conn.execute(
                "SELECT name, sha256 FROM stage_artifacts WHERE attempt_id = ?",
                [attempt_id],
            ).fetchall()
        finally:
            try:
                conn.execute("ROLLBACK")
            except duckdb.Error:
                pass
    finally:
        conn.close()
    return {
        "pointer": {
            "attempt_id": str(pointer[0]),
            "run_fingerprint": str(pointer[1]),
            "digest": str(pointer[2]),
            "artifact_path": str(pointer[3]),
        },
        "history": {
            "status": history[0],
            "run_fingerprint": history[1],
            "digest": history[2],
            "dead_lettered": history[3],
            "quarantined": history[4],
            "duplicates": history[5],
        },
        "event_jsons": [row[0] for row in event_rows],
        "validated": [json.loads(row[0]) for row in validated_rows],
        "lineage": [json.loads(row[0]) for row in lineage_rows],
        "artifacts": {str(name): str(sha) for name, sha in artifact_rows},
    }


def generate_export(
    *,
    home: Path,
    runs_root: Path,
    prices: Path,
    plans: Path,
    out_dir: Path,
) -> dict[str, Any]:
    """Verify one published run and write its sanitized export."""
    home = Path(home)
    runs_root = Path(runs_root)
    prices = Path(prices)
    plans = Path(plans)
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise ExportError(f"export output directory already exists: {out_dir.name}")

    db_path = home / PIPELINE_DB_NAME
    if not db_path.exists():
        raise ExportError(f"store not found: {db_path.name}")
    holder: dict[str, Any] = {}
    snapshot = _store_snapshot(db_path, holder)
    pointer = snapshot["pointer"]
    fingerprint = pointer["run_fingerprint"]
    run_dir = runs_root / fingerprint

    # Check 1: the pointer names an existing run directory.
    if not run_dir.is_dir():
        raise ExportError(f"published run directory does not exist: {fingerprint}")

    published: dict[str, bytes] = {}
    for name in SEVEN_FILES:
        path = run_dir / name
        if not path.is_file():
            raise ExportError(f"published run is missing {name}")
        published[name] = _read_bytes(path, f"published {name}")

    tables = {
        _TABLE_KEYS[name]: _parse_jsonl(published[name], f"published {name}")
        for name in TABLE_FILES
    }
    manifest_doc = _parse_json(published["run-manifest.json"], "published run-manifest.json")
    report_doc = _parse_json(published["margin-report.json"], "published margin-report.json")
    digest_lines = published["margin-digest.txt"].decode("utf-8").splitlines()
    if len(digest_lines) != 2:
        raise ExportError("margin-digest.txt does not hold exactly two lines")
    file_digest = digest_lines[1]

    # Check 2: the financial digest, recomputed from the published tables.
    computed_digest = compute_digest(
        tables["customer_margins"], tables["workload_margins"], tables["unallocated"]
    )
    if not (
        computed_digest
        == file_digest
        == manifest_doc.get("digest")
        == report_doc.get("digest")
    ):
        raise ExportError(
            "financial digest mismatch: "
            f"computed={computed_digest} file={file_digest} "
            f"manifest={manifest_doc.get('digest')} report={report_doc.get('digest')}"
        )
    attested_digest = computed_digest

    # Check 3: the pointer attempt's history row agrees with the pointer.
    history = snapshot["history"]
    if (
        history["status"] != "published"
        or history["run_fingerprint"] != fingerprint
        or history["digest"] != pointer["digest"]
    ):
        raise ExportError(
            "run_history does not agree with the published pointer: "
            f"status={history['status']} fingerprint={history['run_fingerprint']} "
            f"digest={history['digest']}"
        )

    # Check 4: the event stream binds the store, the manifest and the fixtures.
    store_digest = event_stream_digest(snapshot["event_jsons"])
    manifest_store_digest = manifest_doc.get("inputs", {}).get("store_events_sha256")
    repo_root = Path(__file__).resolve().parents[2]
    golden_events_path = repo_root / "fixtures" / "phase1" / "expected" / "events.jsonl"
    golden_raw = _read_bytes(golden_events_path, "committed golden events")
    golden_lines = golden_raw.decode("utf-8").splitlines()
    anchor_digest = event_stream_digest(golden_lines)
    events_in_store = manifest_doc.get("inputs", {}).get("events_in_store")
    if not (store_digest == manifest_store_digest == anchor_digest):
        raise ExportError(
            "event-stream digest mismatch: "
            f"store={store_digest} manifest={manifest_store_digest} anchor={anchor_digest}"
        )
    if events_in_store != len(golden_lines):
        raise ExportError(
            f"events_in_store {events_in_store} != golden events lines {len(golden_lines)}"
        )

    # Check 5: staged artifacts are exactly the seven published files.
    artifacts = snapshot["artifacts"]
    if set(artifacts) != set(SEVEN_FILES):
        raise ExportError(
            f"stage_artifacts names differ from the seven published files: {sorted(artifacts)}"
        )
    actual_names = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    if actual_names != sorted(SEVEN_FILES):
        raise ExportError(f"published run directory holds unexpected files: {actual_names}")
    for name in SEVEN_FILES:
        if artifacts[name] != content_sha256(published[name]):
            raise ExportError(f"stage_artifacts sha256 mismatch for {name}")

    # Check 6: the price and plan inputs are the ones the run recorded.
    price_bytes = _read_bytes(prices, "price list")
    plans_bytes = _read_bytes(plans, "plans")
    inputs = manifest_doc.get("inputs", {})
    if content_sha256(price_bytes) != inputs.get("price_list_sha256"):
        raise ExportError("price list does not match the run manifest hash")
    if content_sha256(plans_bytes) != inputs.get("plans_sha256"):
        raise ExportError("plans do not match the run manifest hash")

    # Financial field allowlist guard (before any projection is written).
    for name in TABLE_FILES:
        _check_financial_guard(name, tables[_TABLE_KEYS[name]])

    # Sanitized scenario events and lineage, with surrogate event keys.
    validated = snapshot["validated"]
    # One surrogate per event IDENTITY, assigned at first occurrence in stage
    # order: contiguous, deterministic, and shared across record versions so
    # the engine's (provider, account, event-id) grouping stays intact.
    surrogates: dict[str, str] = {}
    for event in validated:
        identity = str(event["provider_event_id"])
        if identity not in surrogates:
            surrogates[identity] = f"evt-{len(surrogates) + 1:04d}"

    scenario_rows: list[dict[str, Any]] = []
    for event in validated:
        supersedes = event.get("supersedes")
        if supersedes is None:
            supersedes_key = None
        elif str(supersedes) in surrogates:
            supersedes_key = surrogates[str(supersedes)]
        else:
            raise ExportError(
                f"supersedes target {supersedes!r} is outside the published event set"
            )
        scenario_rows.append(
            {
                "event_key": surrogates[str(event["provider_event_id"])],
                "customer_id": event["customer_id"],
                "model": event["model"],
                "feature": event["feature"],
                "workload_type": event["workload_type"],
                "usage_time": event["usage_time"],
                "tokens_input": event["tokens_input"],
                "tokens_output": event["tokens_output"],
                "tokens_cached": event["tokens_cached"],
                "unavailable_fields": event["unavailable_fields"],
                "record_version": event["record_version"],
                "supersedes_event_key": supersedes_key,
            }
        )

    lineage_rows: list[dict[str, Any]] = []
    for edge in snapshot["lineage"]:
        superseded_id = str(edge["superseded_event_id"])
        superseding_id = str(edge["superseding_event_id"])
        if superseded_id not in surrogates or superseding_id not in surrogates:
            raise ExportError("a lineage edge names an event outside the published set")
        lineage_rows.append(
            {
                "superseded_event_key": surrogates[superseded_id],
                "superseding_event_key": surrogates[superseding_id],
                "superseded_record_version": edge["superseded_record_version"],
                "superseding_record_version": edge["superseding_record_version"],
                "rule": edge["rule"],
                "status": edge["status"],
                "usage_time": edge["usage_time"],
                "arrival_time": edge["arrival_time"],
            }
        )

    price_doc = _parse_json(price_bytes, "price list")
    plans_doc = _parse_json(plans_bytes, "plans")

    payloads: dict[str, bytes] = {
        "customer-margins.jsonl": published["customer-margins.jsonl"],
        "workload-margins.jsonl": published["workload-margins.jsonl"],
        "unallocated.jsonl": published["unallocated.jsonl"],
        "scenario-events.jsonl": "".join(
            canonical_json(row) + "\n" for row in scenario_rows
        ).encode("utf-8"),
        "lineage.jsonl": "".join(
            canonical_json(row) + "\n" for row in lineage_rows
        ).encode("utf-8"),
        "price-list.json": price_bytes,
        "plans.json": plans_bytes,
    }
    row_counts = {
        "customer-margins.jsonl": len(tables["customer_margins"]),
        "workload-margins.jsonl": len(tables["workload_margins"]),
        "unallocated.jsonl": len(tables["unallocated"]),
        "scenario-events.jsonl": len(scenario_rows),
        "lineage.jsonl": len(lineage_rows),
        "price-list.json": len(price_doc.get("entries", [])),
        "plans.json": len(plans_doc.get("entries", [])),
    }
    files_block = {
        name: {"rows": row_counts[name], "sha256": content_sha256(payloads[name])}
        for name in DATA_FILES
    }
    integrity = export_integrity_digest(files_block)

    export_manifest = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "source": {
            "run_fingerprint": fingerprint,
            "artifact_dir": fingerprint,
            "as_of_cutoff": manifest_doc.get("as_of_cutoff"),
            "code_version": manifest_doc.get("code_version"),
            "code_version_source": manifest_doc.get("code_version_source"),
            "working_tree_clean": manifest_doc.get("working_tree_clean"),
            "dirty_status_sha256": manifest_doc.get("dirty_status_sha256"),
            "digest": attested_digest,
            "digest_definition": DIGEST_DEFINITION,
            "store_backend": inputs.get("store_backend"),
            "store_locator": inputs.get("store_locator"),
            "store_events_sha256": store_digest,
            "price_list_id": inputs.get("price_list_id"),
            "price_list_sha256": inputs.get("price_list_sha256"),
            "plans_version_id": inputs.get("plans_version_id"),
            "plans_sha256": inputs.get("plans_sha256"),
            "events_in_store": inputs.get("events_in_store"),
            "events_included": inputs.get("events_included"),
            "events_excluded_by_cutoff": inputs.get("events_excluded_by_cutoff"),
            "events_costed": report_doc.get("events_costed"),
            "events_unpriced": report_doc.get("events_unpriced"),
            "events_superseded": report_doc.get("events_superseded"),
            "unpriced_by_reason": report_doc.get("unpriced_by_reason"),
            "dangling_supersedes": report_doc.get("dangling_supersedes"),
            "unallocated_cost_share_bp": report_doc.get("unallocated_cost_share_bp"),
            "unallocated_event_share_bp": report_doc.get("unallocated_event_share_bp"),
            "dead_lettered": history["dead_lettered"],
            "quarantined": history["quarantined"],
            "duplicates": history["duplicates"],
        },
        "labels": manifest_doc.get("labels"),
        "files": files_block,
        "export_integrity_definition": EXPORT_INTEGRITY_DEFINITION,
        "export_integrity_digest": integrity,
    }
    payloads["export-manifest.json"] = (pretty_json(export_manifest) + "\n").encode("utf-8")

    # Banned-key scan over every export byte payload, before writing.
    for name, data in payloads.items():
        for banned in BANNED_KEYS:
            if banned in data:
                raise ExportError(f"banned key {banned.decode()} would appear in {name}")

    try:
        out_dir.mkdir(parents=True)
        for name in DATA_FILES + ("export-manifest.json",):
            (out_dir / name).write_bytes(payloads[name])
    except OSError as exc:
        raise ExportError(f"cannot write export: {exc}") from exc

    return {
        "run_fingerprint": fingerprint,
        "digest": attested_digest,
        "store_events_sha256": store_digest,
        "manifest_store_events_sha256": manifest_store_digest,
        "fixture_anchor_sha256": anchor_digest,
        "events_in_store": events_in_store,
        "golden_event_lines": len(golden_lines),
        "export_integrity_digest": integrity,
        "out_dir": out_dir,
        "files": files_block,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_cost_margin.export",
        description="Generate the sanitized Phase 5 export from one published run.",
    )
    parser.add_argument("--home", required=True, type=Path, help="working store root, e.g. var")
    parser.add_argument("--runs", required=True, type=Path, help="published runs root")
    parser.add_argument("--prices", required=True, type=Path, help="price-list JSON file")
    parser.add_argument("--plans", required=True, type=Path, help="simulated customer-plan JSON file")
    parser.add_argument("--out", required=True, type=Path, help="fresh export directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_export(
            home=args.home,
            runs_root=args.runs,
            prices=args.prices,
            plans=args.plans,
            out_dir=args.out,
        )
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"check_1 published fingerprint={result['run_fingerprint']}")
    print(f"check_2 attested source financial digest={result['digest']}")
    print(
        "check_4 event-stream digests "
        f"store={result['store_events_sha256']} "
        f"manifest={result['manifest_store_events_sha256']} "
        f"fixture_anchor={result['fixture_anchor_sha256']} "
        f"events_in_store={result['events_in_store']} "
        f"golden_event_lines={result['golden_event_lines']}"
    )
    print(f"export_integrity_digest={result['export_integrity_digest']}")
    print(f"export written to {result['out_dir'].name} ({len(result['files']) + 1} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
