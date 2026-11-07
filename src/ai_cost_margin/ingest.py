"""Idempotent, file-driven ingestion for the Phase 1 synthetic fixtures.

Data flow::

    batch file -> content hash -> raw copy -> envelope validation
              -> per-record validation -> dead-letter
              -> per-key payload history -> duplicate | version | quarantine | insert
              -> append-only store under var/ -> run report

No wall clock is read. ``arrival_time`` comes from the batch envelope and the
``ingest_id`` is content-addressed, so the same deliveries always produce the
same bytes.

The manifest row for a file is written **last** and is that file's commit
marker. A crash leaves appended rows without a marker; because events are
deduplicated by payload and dead-letter/quarantine rows are deduplicated by
``(content_sha256, event_index, reason)``, a clean re-run converges instead of
duplicating. This is not atomicity: a torn JSONL line is a hard stop, and true
transactions arrive in Phase 4.

CLI (from the repo root, with ``src`` on ``PYTHONPATH``)::

    $env:PYTHONPATH = "$PWD\\src"
    .venv\\Scripts\\python.exe -m ai_cost_margin.ingest --home var --dir fixtures\\phase1
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .schema import validate_envelope, validate_event
from .store import StoreError, content_sha256, payload_sha256
from .store_adapter import StoreAdapter, default_adapter

__all__ = ["COUNT_KEYS", "FileOutcome", "IngestError", "IngestReport", "ingest", "main"]

COUNT_KEYS = (
    "received",
    "validated",
    "inserted",
    "duplicates",
    "quarantined",
    "dead_lettered",
    "dangling_supersedes",
)

STATUS_APPLIED = "applied"
STATUS_DUPLICATE_FILE = "duplicate_file_ignored"
STATUS_FAILED = "failed"


class IngestError(Exception):
    """An input could not be delivered (missing, unreadable, unusable)."""


@dataclass
class FileOutcome:
    """What one delivered file produced."""

    file_name: str
    content_sha256: str
    status: str
    reason: str | None
    counts: dict[str, int]


@dataclass
class IngestReport:
    """The result of one ingest invocation."""

    ingest_id: str
    files: list[FileOutcome]
    totals: dict[str, int]


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def _outcome_dict(outcome: FileOutcome) -> dict[str, Any]:
    return {
        "file_name": outcome.file_name,
        "content_sha256": outcome.content_sha256,
        "status": outcome.status,
        "reason": outcome.reason,
        "counts": dict(outcome.counts),
    }


def _store_record(event: dict, provider: str, account_id: str, arrival_time: str) -> dict:
    """The event plus the envelope context an event is not allowed to supply."""
    record = dict(event)
    record["provider"] = provider
    record["account_id"] = account_id
    record["arrival_time"] = arrival_time
    record["payload_sha256"] = payload_sha256(event)
    return record


def _manifest_row(
    *,
    ingest_id: str,
    digest: str,
    file_name: str,
    batch_id: str | None,
    provider: str | None,
    account_id: str | None,
    status: str,
    reason: str | None,
    counts: dict[str, int],
    size_bytes: int,
) -> dict[str, Any]:
    return {
        "ingest_id": ingest_id,
        "file_name": file_name,
        "content_sha256": digest,
        "size_bytes": size_bytes,
        "batch_id": batch_id,
        "provider": provider,
        "account_id": account_id,
        "status": status,
        "reason": reason,
        "counts": dict(counts),
    }


def _dead_row(
    *,
    ingest_id: str,
    digest: str,
    file_name: str,
    event_index: int | None,
    reason: str,
    message: str,
    record: Any,
) -> dict[str, Any]:
    # Dead-letter rows keep the rejected synthetic record verbatim, including
    # any unknown fields and their values. That is safe only because Phase 1
    # ingests synthetic fixtures; before any real provider data is ingested
    # (Phase 7 gate) redaction or field stripping must be added first.
    return {
        "ingest_id": ingest_id,
        "file_name": file_name,
        "content_sha256": digest,
        "event_index": event_index,
        "reason": reason,
        "message": message,
        "record": record,
    }


def _quarantine_row(
    *,
    ingest_id: str,
    digest: str,
    file_name: str,
    event_index: int,
    reason: str,
    provider_event_id: str,
    stored_payload: str,
    incoming_payload: str,
) -> dict[str, Any]:
    return {
        "ingest_id": ingest_id,
        "file_name": file_name,
        "content_sha256": digest,
        "event_index": event_index,
        "reason": reason,
        "provider_event_id": provider_event_id,
        "stored_payload_sha256": stored_payload,
        "incoming_payload_sha256": incoming_payload,
    }


def ingest(
    paths: Sequence[Path],
    home: Path | str,
    adapter: StoreAdapter | None = None,
) -> IngestReport:
    """Ingest ``paths`` (in delivery order) into the store rooted at ``home``.

    ``adapter`` is the Phase 4 (A6) seam. When it is ``None`` the default JSONL
    adapter is used, so the Phase 1 CLI and every existing test keep the exact
    previous behaviour. The adapter only reads dedup state and appends rows;
    validation and dedup stay here, exactly once.
    """
    home_path = Path(home)
    store_adapter: StoreAdapter = default_adapter(home_path) if adapter is None else adapter

    deliveries: list[tuple[Path, bytes, str]] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise IngestError(f"cannot read {path}: {exc}") from exc
        deliveries.append((path, data, content_sha256(data)))

    if not deliveries:
        raise IngestError("no input files were delivered")

    ingest_id = "sha256:" + content_sha256(
        "\n".join(digest for _, _, digest in deliveries).encode("utf-8")
    )

    existing_events = store_adapter.read_events()
    manifest = store_adapter.read_manifest()
    dead_letter = store_adapter.read_dead_letter()
    quarantine = store_adapter.read_quarantine()

    # Every stored payload per dedup key (amendment A2), so re-delivery of any
    # known version is a duplicate rather than a version regression.
    payloads_by_key: dict[tuple[str, str, str], set[str]] = {}
    current_by_key: dict[tuple[str, str, str], dict] = {}
    for record in existing_events:
        key = (record["provider"], record["account_id"], record["provider_event_id"])
        payloads_by_key.setdefault(key, set()).add(record["payload_sha256"])
        current_by_key[key] = record

    applied = {row["content_sha256"] for row in manifest if row.get("status") == STATUS_APPLIED}
    dead_seen = {
        (row["content_sha256"], row.get("event_index"), row["reason"]) for row in dead_letter
    }
    quarantine_seen = {
        (row["content_sha256"], row.get("event_index"), row["reason"]) for row in quarantine
    }

    outcomes: list[FileOutcome] = []

    for path, data, digest in deliveries:
        store_adapter.write_raw(digest, data)
        counts = _empty_counts()
        file_events: list[dict] = []
        file_dead: list[dict] = []
        file_quarantine: list[dict] = []

        if digest in applied:
            counts_out: dict[str, int] = counts
            store_adapter.append_manifest(
                [
                    _manifest_row(
                        ingest_id=ingest_id,
                        digest=digest,
                        file_name=path.name,
                        batch_id=None,
                        provider=None,
                        account_id=None,
                        status=STATUS_DUPLICATE_FILE,
                        reason=None,
                        counts=counts_out,
                        size_bytes=len(data),
                    )
                ],
            )
            outcomes.append(
                FileOutcome(path.name, digest, STATUS_DUPLICATE_FILE, None, dict(counts_out))
            )
            continue

        try:
            envelope = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            reason = "invalid_json"
            message = f"file is not valid UTF-8 JSON: {type(exc).__name__}: {exc}"
            counts["dead_lettered"] += 1
            seen_key = (digest, None, reason)
            if seen_key not in dead_seen:
                dead_seen.add(seen_key)
                file_dead.append(
                    _dead_row(
                        ingest_id=ingest_id,
                        digest=digest,
                        file_name=path.name,
                        event_index=None,
                        reason=reason,
                        message=message,
                        record=None,
                    )
                )
            store_adapter.append_dead_letter(file_dead)
            store_adapter.append_manifest(
                [
                    _manifest_row(
                        ingest_id=ingest_id,
                        digest=digest,
                        file_name=path.name,
                        batch_id=None,
                        provider=None,
                        account_id=None,
                        status=STATUS_FAILED,
                        reason=reason,
                        counts=counts,
                        size_bytes=len(data),
                    )
                ],
            )
            outcomes.append(FileOutcome(path.name, digest, STATUS_FAILED, reason, dict(counts)))
            continue

        envelope_issues = validate_envelope(envelope)
        if envelope_issues:
            issue = envelope_issues[0]
            counts["dead_lettered"] += 1
            seen_key = (digest, None, issue.reason)
            if seen_key not in dead_seen:
                dead_seen.add(seen_key)
                file_dead.append(
                    _dead_row(
                        ingest_id=ingest_id,
                        digest=digest,
                        file_name=path.name,
                        event_index=None,
                        reason=issue.reason,
                        message=issue.message,
                        record=envelope,
                    )
                )
            store_adapter.append_dead_letter(file_dead)
            store_adapter.append_manifest(
                [
                    _manifest_row(
                        ingest_id=ingest_id,
                        digest=digest,
                        file_name=path.name,
                        batch_id=None,
                        provider=None,
                        account_id=None,
                        status=STATUS_FAILED,
                        reason=issue.reason,
                        counts=counts,
                        size_bytes=len(data),
                    )
                ],
            )
            outcomes.append(FileOutcome(path.name, digest, STATUS_FAILED, issue.reason, dict(counts)))
            continue

        provider: str = envelope["provider"]
        account_id: str = envelope["account_id"]
        arrival_time: str = envelope["arrival_time"]
        batch_id: str = envelope["batch_id"]

        for index, raw_event in enumerate(envelope["events"]):
            counts["received"] += 1
            issues = validate_event(raw_event)
            if issues:
                issue = issues[0]
                counts["dead_lettered"] += 1
                seen_key = (digest, index, issue.reason)
                if seen_key not in dead_seen:
                    dead_seen.add(seen_key)
                    file_dead.append(
                        _dead_row(
                            ingest_id=ingest_id,
                            digest=digest,
                            file_name=path.name,
                            event_index=index,
                            reason=issue.reason,
                            message=issue.message,
                            record=raw_event,
                        )
                    )
                continue

            counts["validated"] += 1
            incoming_payload = payload_sha256(raw_event)
            key = (provider, account_id, raw_event["provider_event_id"])

            # Amendment A2: any stored version of this key with this payload is
            # the same event, whatever its record_version.
            if incoming_payload in payloads_by_key.get(key, set()):
                counts["duplicates"] += 1
                continue

            current = current_by_key.get(key)
            reason: str | None = None
            if current is not None:
                incoming_version = raw_event["record_version"]
                current_version = current["record_version"]
                if incoming_version < current_version:
                    reason = "version_regression"
                elif incoming_version == current_version:
                    # Amendment A1: a changed payload at the same version is not
                    # a correction, even when it points at itself.
                    reason = "same_key_changed_payload"

            if reason is not None:
                counts["quarantined"] += 1
                seen_key = (digest, index, reason)
                if seen_key not in quarantine_seen:
                    quarantine_seen.add(seen_key)
                    file_quarantine.append(
                        _quarantine_row(
                            ingest_id=ingest_id,
                            digest=digest,
                            file_name=path.name,
                            event_index=index,
                            reason=reason,
                            provider_event_id=key[2],
                            stored_payload=current["payload_sha256"],
                            incoming_payload=incoming_payload,
                        )
                    )
                continue

            record = _store_record(raw_event, provider, account_id, arrival_time)
            file_events.append(record)
            payloads_by_key.setdefault(key, set()).add(incoming_payload)
            current_by_key[key] = record
            counts["inserted"] += 1

            supersedes = raw_event["supersedes"]
            if supersedes is not None:
                target = (provider, account_id, supersedes)
                if target not in current_by_key:
                    counts["dangling_supersedes"] += 1

        store_adapter.append_events(file_events)
        store_adapter.append_dead_letter(file_dead)
        store_adapter.append_quarantine(file_quarantine)
        store_adapter.append_manifest(
            [
                _manifest_row(
                    ingest_id=ingest_id,
                    digest=digest,
                    file_name=path.name,
                    batch_id=batch_id,
                    provider=provider,
                    account_id=account_id,
                    status=STATUS_APPLIED,
                    reason=None,
                    counts=counts,
                    size_bytes=len(data),
                )
            ],
        )
        applied.add(digest)
        outcomes.append(FileOutcome(path.name, digest, STATUS_APPLIED, None, dict(counts)))

    totals = _empty_counts()
    for outcome in outcomes:
        for key in COUNT_KEYS:
            totals[key] += outcome.counts[key]

    store_adapter.write_report(
        {
            "ingest_id": ingest_id,
            "files": [_outcome_dict(outcome) for outcome in outcomes],
            "totals": totals,
        }
    )
    return IngestReport(ingest_id=ingest_id, files=outcomes, totals=totals)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_cost_margin.ingest",
        description="Ingest Phase 1 synthetic batch files into the private store.",
    )
    parser.add_argument("--home", required=True, type=Path, help="private store root, e.g. var")
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="batch file to deliver; repeat to fix the delivery order",
    )
    parser.add_argument(
        "--dir",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help="directory whose top-level *.json batches are delivered in sorted order; repeatable",
    )
    args = parser.parse_args(argv)

    deliveries: list[Path] = []
    for path in args.file:
        if not path.is_file():
            print(f"error: batch file not found: {path}", file=sys.stderr)
            return 2
        deliveries.append(path)
    for directory in args.dir:
        if not directory.is_dir():
            print(f"error: batch directory not found: {directory}", file=sys.stderr)
            return 2
        deliveries.extend(
            sorted(
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix == ".json"
            )
        )
    if not deliveries:
        print("error: nothing to ingest; pass --file and/or --dir", file=sys.stderr)
        return 2

    try:
        report = ingest(deliveries, args.home)
    except (IngestError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"ingest {report.ingest_id} -> {args.home}")
    for outcome in report.files:
        suffix = f" ({outcome.reason})" if outcome.reason else ""
        print(
            f"  {outcome.file_name}: {outcome.status}{suffix} "
            f"received={outcome.counts['received']} inserted={outcome.counts['inserted']} "
            f"duplicates={outcome.counts['duplicates']} quarantined={outcome.counts['quarantined']} "
            f"dead_lettered={outcome.counts['dead_lettered']}"
        )
    print("  totals: " + " ".join(f"{key}={report.totals[key]}" for key in COUNT_KEYS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
