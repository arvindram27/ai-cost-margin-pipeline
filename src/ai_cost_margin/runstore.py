"""DuckDB working store for Phase 4: schema, transactions, run history, publish.

The runner is the only writer. Every stage writes its own run-scoped rows and
its ``run_stages`` checkpoint inside one transaction, so a killed stage leaves
no partial stage output. The global validated-event append is never deleted; a
resumed attempt copies the stage rows and counters it adopts under its own
``attempt_id``.

The publish pointer (``published_run``) is the visibility authority: a
runner-internal query sees the previous published run or none, never a staged
attempt (amendment A8 scopes that to runner-internal query discipline). The
pointer flip and the attempt's published status commit as one transaction, and
a JSON-log IO failure after that commit never rolls the record back (R8).

DuckDB's own writer lock is the single-writer authority across processes:
the OS file lock DuckDB takes on open serializes pipeline runners in
different OS processes. Within one process, connections to the same file
share one DuckDB instance, so the lock does not serialize two runner
invocations in the same process; the supported usage is one pipeline runner
invocation per OS process. Only an open-time ``duckdb.IOException`` whose
message contains the observed Windows sharing-violation phrase ``being used
by another process``, or both POSIX phrases ``Could not set lock on file``
and ``Conflicting lock is held``, maps to ``PipelineBusyError``; every other
open failure is a typed ``StoreOpenError`` preserving the original message
(A10 / R4).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from .store import Store, StoreError, append_jsonl, canonical_json, content_sha256

__all__ = [
    "PIPELINE_DB_NAME",
    "PIPELINE_VERSION",
    "DuckDbStoreAdapter",
    "PipelineBusyError",
    "PipelineStateError",
    "RunLogError",
    "RunStore",
    "StoreOpenError",
    "connect_store",
    "event_stream_digest",
    "new_attempt_id",
    "read_status",
    "rows_stream_digest",
    "utc_now",
]

PIPELINE_VERSION = "0.4.0"
PIPELINE_DB_NAME = "pipeline.duckdb"

STAGE_ORDER = ("raw", "validated", "costed", "aggregated")

RAW_TABLES = ("ingest_manifest", "dead_letters", "quarantined_records", "ingest_reports")
VALIDATED_TABLES = ("stage_validated", "stage_lineage")
COSTED_TABLES = ("stage_costed",)
AGGREGATED_TABLES = (
    "stage_customer_margins",
    "stage_workload_margins",
    "stage_unallocated",
    "stage_artifacts",
)

STAGE_TABLES = {
    "raw": RAW_TABLES,
    "validated": VALIDATED_TABLES,
    "costed": COSTED_TABLES,
    "aggregated": AGGREGATED_TABLES,
}

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS raw_batches (
        content_sha256 VARCHAR PRIMARY KEY,
        size_bytes BIGINT NOT NULL,
        first_attempt_id VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_manifest (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dead_letters (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        event_index BIGINT,
        reason VARCHAR NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarantined_records (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        event_index BIGINT,
        reason VARCHAR NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_reports (
        attempt_id VARCHAR PRIMARY KEY,
        report_json VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS validated_events (
        provider VARCHAR NOT NULL,
        account_id VARCHAR NOT NULL,
        provider_event_id VARCHAR NOT NULL,
        record_version BIGINT NOT NULL,
        event_json VARCHAR NOT NULL,
        payload_sha256 VARCHAR NOT NULL,
        arrival_time VARCHAR NOT NULL,
        PRIMARY KEY (provider, account_id, provider_event_id, record_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_validated (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_lineage (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_costed (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_customer_margins (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_workload_margins (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_unallocated (
        attempt_id VARCHAR NOT NULL,
        ordinal BIGINT NOT NULL,
        row_json VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_artifacts (
        attempt_id VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        data BLOB NOT NULL,
        sha256 VARCHAR NOT NULL,
        PRIMARY KEY (attempt_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_stages (
        attempt_id VARCHAR NOT NULL,
        stage VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        input_digest VARCHAR,
        output_digest VARCHAR,
        row_count BIGINT,
        meta_json VARCHAR,
        started_at_utc VARCHAR,
        finished_at_utc VARCHAR,
        duration_ms BIGINT,
        PRIMARY KEY (attempt_id, stage)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_history (
        attempt_id VARCHAR PRIMARY KEY,
        invocation_sha256 VARCHAR,
        run_fingerprint VARCHAR,
        pipeline_version VARCHAR,
        code_version VARCHAR,
        code_version_source VARCHAR,
        working_tree_clean BOOLEAN,
        dirty_status_sha256 VARCHAR,
        as_of_cutoff VARCHAR,
        price_list_sha256 VARCHAR,
        plans_sha256 VARCHAR,
        pre_raw_store_sha256 VARCHAR,
        post_raw_store_sha256 VARCHAR,
        started_at_utc VARCHAR,
        finished_at_utc VARCHAR,
        interrupted_at_utc VARCHAR,
        status VARCHAR NOT NULL,
        current_stage VARCHAR,
        last_completed_stage VARCHAR,
        stage_durations_ms VARCHAR,
        records_read BIGINT,
        records_validated BIGINT,
        records_dead_lettered BIGINT,
        records_quarantined BIGINT,
        records_unpriced BIGINT,
        records_unallocated BIGINT,
        received BIGINT,
        inserted BIGINT,
        duplicates BIGINT,
        quarantined BIGINT,
        dead_lettered BIGINT,
        dangling_supersedes BIGINT,
        schema_versions_seen VARCHAR,
        price_versions_applied VARCHAR,
        digest VARCHAR,
        digest_definition VARCHAR,
        artifact_dir VARCHAR,
        published_at_utc VARCHAR,
        error VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published_run (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        attempt_id VARCHAR NOT NULL,
        run_fingerprint VARCHAR NOT NULL,
        digest VARCHAR NOT NULL,
        artifact_path VARCHAR NOT NULL,
        published_at_utc VARCHAR NOT NULL
    )
    """,
)


class PipelineBusyError(RuntimeError):
    """Another pipeline run holds the DuckDB write lock."""


class StoreOpenError(RuntimeError):
    """The DuckDB store could not be opened for a reason other than contention."""


class PipelineStateError(StoreError):
    """Stored checkpoint data does not re-verify against its recorded digest."""


class RunLogError(StoreError):
    """The operational JSON run log cannot be appended (observability only)."""


def utc_now() -> str:
    """Operational UTC timestamp; never written into the seven run files."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_attempt_id() -> str:
    """A unique, operational attempt identifier."""
    return "attempt-" + uuid.uuid4().hex


def event_stream_digest(event_jsons: Sequence[str]) -> str:
    """A7 canonical event stream: sorted canonical rows, LF terminated."""
    ordered = sorted(event_jsons)
    return hashlib.sha256("".join(row + "\n" for row in ordered).encode("utf-8")).hexdigest()


def rows_stream_digest(rows_by_table: dict[str, Sequence[str]]) -> str:
    """Digest of stage rows: tables in name order, rows in their stored order."""
    chunks: list[str] = []
    for table in sorted(rows_by_table):
        for row in rows_by_table[table]:
            chunks.append(f"{table}:{row}\n")
    return hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()


def connect_store(db_path: Path | str) -> duckdb.DuckDBPyConnection:
    """Open the working database, mapping only real lock contention to busy.

    Contention is cross-process (see the module docstring). It is recognised
    only by the constant phrases of the observed sharing-violation messages;
    the variable holder detail is never matched on, only carried verbatim.
    """
    path = Path(db_path)
    try:
        return duckdb.connect(str(path))
    except duckdb.IOException as exc:
        message = str(exc)
        windows_lock = "being used by another process" in message
        posix_lock = (
            "Could not set lock on file" in message
            and "Conflicting lock is held" in message
        )
        if windows_lock or posix_lock:
            detail = (
                f"another pipeline run holds the database write lock ({path.name}); "
                "wait for it to finish or confirm it is not running"
            )
            holder_index = message.find("File is already open in")
            if holder_index >= 0:
                detail += "; " + message[holder_index:].strip()
            raise PipelineBusyError(detail) from exc
        raise StoreOpenError(f"cannot open store {path.name}: {message}") from exc
    except (duckdb.Error, OSError) as exc:
        raise StoreOpenError(f"cannot open store {path.name}: {exc}") from exc


class DuckDbStoreAdapter:
    """The A6 adapter backed by the transactional DuckDB working store."""

    def __init__(self, store: "RunStore", attempt_id: str) -> None:
        self.store = store
        self.conn = store.conn
        self.home = store.home
        self.attempt_id = attempt_id
        self._ordinals = {
            table: self._next_ordinal(table)
            for table in ("ingest_manifest", "dead_letters", "quarantined_records")
        }

    def _next_ordinal(self, table: str) -> int:
        row = self.conn.execute(
            f"SELECT COALESCE(MAX(ordinal), -1) + 1 FROM {table} WHERE attempt_id = ?",
            [self.attempt_id],
        ).fetchone()
        return int(row[0])

    def read_events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT event_json FROM validated_events "
            "ORDER BY provider, account_id, provider_event_id, record_version"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def read_manifest(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT row_json FROM ingest_manifest ORDER BY attempt_id, ordinal"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def read_dead_letter(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT row_json FROM dead_letters ORDER BY attempt_id, ordinal"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def read_quarantine(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT row_json FROM quarantined_records ORDER BY attempt_id, ordinal"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def write_raw(self, digest: str, data: bytes) -> None:
        Store(self.home).write_raw(digest, data)
        self.conn.execute(
            "INSERT OR IGNORE INTO raw_batches(content_sha256, size_bytes, first_attempt_id) "
            "VALUES (?, ?, ?)",
            [digest, len(data), self.attempt_id],
        )

    def append_events(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.conn.execute(
                "INSERT INTO validated_events "
                "(provider, account_id, provider_event_id, record_version, event_json, "
                " payload_sha256, arrival_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    row["provider"],
                    row["account_id"],
                    row["provider_event_id"],
                    row["record_version"],
                    canonical_json(row),
                    row["payload_sha256"],
                    row["arrival_time"],
                ],
            )

    def _append_attempt_rows(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            ordinal = self._ordinals[table]
            if table == "ingest_manifest":
                self.conn.execute(
                    "INSERT INTO ingest_manifest "
                    "(attempt_id, ordinal, content_sha256, status, row_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        self.attempt_id,
                        ordinal,
                        row["content_sha256"],
                        row["status"],
                        canonical_json(row),
                    ],
                )
            else:
                self.conn.execute(
                    f"INSERT INTO {table} "
                    "(attempt_id, ordinal, content_sha256, event_index, reason, row_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        self.attempt_id,
                        ordinal,
                        row["content_sha256"],
                        row.get("event_index"),
                        row["reason"],
                        canonical_json(row),
                    ],
                )
            self._ordinals[table] = ordinal + 1

    def append_manifest(self, rows: Iterable[dict[str, Any]]) -> None:
        self._append_attempt_rows("ingest_manifest", rows)

    def append_dead_letter(self, rows: Iterable[dict[str, Any]]) -> None:
        self._append_attempt_rows("dead_letters", rows)

    def append_quarantine(self, rows: Iterable[dict[str, Any]]) -> None:
        self._append_attempt_rows("quarantined_records", rows)

    def write_report(self, report: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ingest_reports(attempt_id, report_json) VALUES (?, ?)",
            [self.attempt_id, canonical_json(report)],
        )


class RunStore:
    """Transactional access to one DuckDB working store."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, home: Path, db_path: Path) -> None:
        self.conn = conn
        self.home = Path(home)
        self.db_path = Path(db_path)
        self.log_path = self.home / "logs" / "runs.jsonl"

    # ---- transactions -------------------------------------------------
    def begin(self) -> None:
        self.conn.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except duckdb.Error:
            pass

    # ---- schema -------------------------------------------------------
    def initialize(self) -> None:
        self.begin()
        try:
            for statement in _DDL:
                self.conn.execute(statement)
        except Exception:
            self.rollback()
            raise
        self.commit()

    # ---- event store --------------------------------------------------
    def current_store_digest(self) -> str:
        rows = self.conn.execute("SELECT event_json FROM validated_events").fetchall()
        return event_stream_digest([row[0] for row in rows])

    def event_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM validated_events").fetchone()[0])

    def schema_versions_seen(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT json_extract_string(event_json, '$.schema_version') AS version, COUNT(*) "
            "FROM validated_events GROUP BY version ORDER BY version"
        ).fetchall()
        return {str(version): int(count) for version, count in rows}

    def load_events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT event_json FROM validated_events "
            "ORDER BY provider, account_id, provider_event_id, record_version"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # ---- attempts -----------------------------------------------------
    def stale_scan(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT attempt_id, last_completed_stage FROM run_history "
            "WHERE status = 'running' ORDER BY started_at_utc, attempt_id"
        ).fetchall()
        interrupted: list[dict[str, Any]] = []
        for attempt_id, last_completed_stage in rows:
            self.conn.execute(
                "UPDATE run_history SET status = 'interrupted', interrupted_at_utc = ? "
                "WHERE attempt_id = ?",
                [utc_now(), attempt_id],
            )
            self.conn.execute(
                "UPDATE run_stages SET status = 'abandoned' "
                "WHERE attempt_id = ? AND status = 'running'",
                [attempt_id],
            )
            interrupted.append(
                {
                    "attempt_id": attempt_id,
                    "last_completed_stage": last_completed_stage,
                }
            )
        return interrupted

    def start_attempt(
        self,
        *,
        invocation_sha256: str,
        pipeline_version: str,
        code_version: Any,
        cutoff: str,
        price_list_sha256: str,
        plans_sha256: str,
        pre_raw_store_sha256: str,
    ) -> str:
        attempt_id = new_attempt_id()
        self.conn.execute(
            "INSERT INTO run_history ("
            "attempt_id, invocation_sha256, run_fingerprint, pipeline_version, code_version, "
            "code_version_source, working_tree_clean, dirty_status_sha256, as_of_cutoff, "
            "price_list_sha256, plans_sha256, pre_raw_store_sha256, started_at_utc, status"
            ") VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            [
                attempt_id,
                invocation_sha256,
                pipeline_version,
                code_version.code_version,
                code_version.code_version_source,
                code_version.working_tree_clean,
                code_version.dirty_status_sha256,
                cutoff,
                price_list_sha256,
                plans_sha256,
                pre_raw_store_sha256,
                utc_now(),
            ],
        )
        return attempt_id

    def find_resumable(self, invocation_sha256: str) -> str | None:
        row = self.conn.execute(
            "SELECT attempt_id FROM run_history "
            "WHERE invocation_sha256 = ? AND status = 'interrupted' "
            "ORDER BY started_at_utc DESC, attempt_id DESC LIMIT 1",
            [invocation_sha256],
        ).fetchone()
        return None if row is None else str(row[0])

    def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE run_history SET {assignments} WHERE attempt_id = ?",
            [*fields.values(), attempt_id],
        )

    def fail_attempt(self, attempt_id: str, message: str) -> None:
        self.conn.execute(
            "UPDATE run_history SET status = 'failed', finished_at_utc = ?, error = ? "
            "WHERE attempt_id = ?",
            [utc_now(), message, attempt_id],
        )

    def attempt_row(self, attempt_id: str) -> dict[str, Any] | None:
        cursor = self.conn.execute("SELECT * FROM run_history WHERE attempt_id = ?", [attempt_id])
        columns = [item[0] for item in cursor.description]
        row = cursor.fetchone()
        return None if row is None else dict(zip(columns, row))

    def attempt_rows(self) -> list[dict[str, Any]]:
        columns = [
            "attempt_id",
            "invocation_sha256",
            "run_fingerprint",
            "status",
            "last_completed_stage",
            "digest",
            "artifact_dir",
            "started_at_utc",
            "finished_at_utc",
            "interrupted_at_utc",
        ]
        sql = "SELECT " + ", ".join(columns) + " FROM run_history ORDER BY started_at_utc, attempt_id"
        return [dict(zip(columns, row)) for row in self.conn.execute(sql).fetchall()]

    # ---- stages -------------------------------------------------------
    def adapter(self, attempt_id: str) -> DuckDbStoreAdapter:
        return DuckDbStoreAdapter(self, attempt_id)

    def stage_record(self, attempt_id: str, stage: str) -> dict[str, Any] | None:
        cursor = self.conn.execute(
            "SELECT status, input_digest, output_digest, row_count, meta_json "
            "FROM run_stages WHERE attempt_id = ? AND stage = ?",
            [attempt_id, stage],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "status": row[0],
            "input_digest": row[1],
            "output_digest": row[2],
            "row_count": row[3],
            "meta_json": row[4],
        }

    def write_run_stage(
        self,
        attempt_id: str,
        stage: str,
        *,
        status: str,
        input_digest: str,
        output_digest: str,
        row_count: int,
        meta: dict[str, Any],
        duration_ms: int,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            "INSERT OR REPLACE INTO run_stages "
            "(attempt_id, stage, status, input_digest, output_digest, row_count, meta_json, "
            " started_at_utc, finished_at_utc, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                attempt_id,
                stage,
                status,
                input_digest,
                output_digest,
                row_count,
                canonical_json(meta),
                now,
                now,
                duration_ms,
            ],
        )
        self.update_attempt(
            attempt_id,
            last_completed_stage=stage,
            current_stage=stage,
        )

    def replace_stage_rows(self, attempt_id: str, table: str, rows: Sequence[dict[str, Any]]) -> None:
        self.conn.execute(f"DELETE FROM {table} WHERE attempt_id = ?", [attempt_id])
        for ordinal, row in enumerate(rows):
            self.conn.execute(
                f"INSERT INTO {table} (attempt_id, ordinal, row_json) VALUES (?, ?, ?)",
                [attempt_id, ordinal, canonical_json(row)],
            )

    def read_stage_rows(self, attempt_id: str, table: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT row_json FROM {table} WHERE attempt_id = ? ORDER BY ordinal",
            [attempt_id],
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def stage_rows_raw(self, attempt_id: str, table: str) -> list[str]:
        rows = self.conn.execute(
            f"SELECT row_json FROM {table} WHERE attempt_id = ? ORDER BY ordinal",
            [attempt_id],
        ).fetchall()
        return [row[0] for row in rows]

    def stage_tables_digest(self, attempt_id: str, tables: Sequence[str]) -> str:
        return rows_stream_digest(
            {table: self.stage_rows_raw(attempt_id, table) for table in tables}
        )

    def write_artifacts(self, attempt_id: str, files: dict[str, bytes]) -> None:
        self.conn.execute("DELETE FROM stage_artifacts WHERE attempt_id = ?", [attempt_id])
        for name, data in files.items():
            self.conn.execute(
                "INSERT INTO stage_artifacts (attempt_id, name, data, sha256) VALUES (?, ?, ?, ?)",
                [attempt_id, name, data, content_sha256(data)],
            )

    def read_artifacts(self, attempt_id: str) -> dict[str, bytes]:
        rows = self.conn.execute(
            "SELECT name, data FROM stage_artifacts WHERE attempt_id = ? ORDER BY name",
            [attempt_id],
        ).fetchall()
        return {row[0]: bytes(row[1]) for row in rows}

    def artifact_sha_map(self, attempt_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT name, sha256 FROM stage_artifacts WHERE attempt_id = ? ORDER BY name",
            [attempt_id],
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ---- adoption -----------------------------------------------------
    def _table_columns(self, table: str) -> list[str]:
        rows = self.conn.execute(f"PRAGMA table_info('{table}')").fetchall()
        return [row[1] for row in rows]

    def copy_stage_rows(self, previous_id: str, new_id: str, stage: str) -> None:
        for table in STAGE_TABLES[stage]:
            if table == "stage_artifacts":
                self.conn.execute(
                    "INSERT INTO stage_artifacts (attempt_id, name, data, sha256) "
                    "SELECT ?, name, data, sha256 FROM stage_artifacts WHERE attempt_id = ?",
                    [new_id, previous_id],
                )
                continue
            if table == "ingest_reports":
                self.conn.execute(
                    "INSERT INTO ingest_reports (attempt_id, report_json) "
                    "SELECT ?, report_json FROM ingest_reports WHERE attempt_id = ?",
                    [new_id, previous_id],
                )
                continue
            columns = [name for name in self._table_columns(table) if name != "attempt_id"]
            column_sql = ", ".join(columns)
            self.conn.execute(
                f"INSERT INTO {table} (attempt_id, {column_sql}) "
                f"SELECT ?, {column_sql} FROM {table} WHERE attempt_id = ?",
                [new_id, previous_id],
            )

    # ---- publish ------------------------------------------------------
    def publish(
        self,
        attempt_id: str,
        *,
        run_fingerprint: str,
        digest: str,
        artifact_path: str,
        digest_definition: str,
    ) -> str:
        published_at = utc_now()
        self.conn.execute(
            "INSERT OR REPLACE INTO published_run "
            "(singleton, attempt_id, run_fingerprint, digest, artifact_path, published_at_utc) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            [attempt_id, run_fingerprint, digest, artifact_path, published_at],
        )
        self.conn.execute(
            "UPDATE run_history SET status = 'published', finished_at_utc = ?, "
            "digest = ?, digest_definition = ?, artifact_dir = ?, published_at_utc = ? "
            "WHERE attempt_id = ?",
            [published_at, digest, digest_definition, artifact_path, published_at, attempt_id],
        )
        return published_at

    def published_pointer(self) -> dict[str, Any] | None:
        cursor = self.conn.execute("SELECT * FROM published_run WHERE singleton = 1")
        columns = [item[0] for item in cursor.description]
        row = cursor.fetchone()
        return None if row is None else dict(zip(columns, row))

    # ---- JSON run log -------------------------------------------------
    def append_log(self, record: dict[str, Any]) -> None:
        try:
            append_jsonl(self.log_path, [record])
        except StoreError as exc:
            raise RunLogError(f"cannot append to the run log: {exc}") from exc


def read_status(db_path: Path | str) -> dict[str, Any]:
    """Read-only published pointer and attempt rows, for the drill and tests.

    Each attempt row also carries its stage checkpoints. That is operational
    state: it is what the kill drill asserts against, and it never enters a
    published file. A crash can leave a WAL that a read-only connection cannot
    replay, so a read-write open (which recovers the WAL) is the fallback.
    """
    path = Path(db_path)
    if not path.exists():
        return {"published": None, "attempts": []}
    try:
        conn = duckdb.connect(str(path), read_only=True)
    except (duckdb.Error, OSError):
        conn = connect_store(path)
    try:
        store = RunStore(conn, path.parent, path)
        try:
            published = store.published_pointer()
        except duckdb.CatalogException:
            published = None
        attempts: list[dict[str, Any]] = []
        try:
            rows = store.attempt_rows()
        except duckdb.CatalogException:
            rows = []
        for row in rows:
            stages: dict[str, Any] = {}
            for stage in STAGE_ORDER:
                try:
                    record = store.stage_record(row["attempt_id"], stage)
                except duckdb.CatalogException:
                    record = None
                if record is not None:
                    stages[stage] = {
                        "status": record["status"],
                        "input_digest": record["input_digest"],
                        "output_digest": record["output_digest"],
                        "row_count": record["row_count"],
                    }
            attempts.append({**row, "stages": stages})
    finally:
        conn.close()
    return {"published": published, "attempts": attempts}
