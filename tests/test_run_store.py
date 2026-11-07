"""DuckDB working store: schema, digests, lifecycle, publish, and lock mapping.

The lock tests cover the observed ``duckdb==2.0.0`` boundary: contention is
cross-process, so a spawned writer process holds the file lock and the mapper
must produce ``PipelineBusyError``, while a second connection in this same
process shares the instance and opens without contention. They print the
observed exception class and message so the build gate's bounded probe can
quote the runtime literal.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from ai_cost_margin.runs import fixture_code_version
from ai_cost_margin.runstore import (
    PIPELINE_DB_NAME,
    DuckDbStoreAdapter,
    PipelineBusyError,
    RunStore,
    StoreOpenError,
    connect_store,
    event_stream_digest,
    new_attempt_id,
    read_status,
    rows_stream_digest,
    utc_now,
)
from ai_cost_margin.store import Store, canonical_json, content_sha256


def _open(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    db_path = home / PIPELINE_DB_NAME
    conn = connect_store(db_path)
    store = RunStore(conn, home, db_path)
    store.initialize()
    return conn, store, home, db_path


def _attempt(store: RunStore, invocation: str = "inv-1") -> str:
    return store.start_attempt(
        invocation_sha256=invocation,
        pipeline_version="0.4.0",
        code_version=fixture_code_version("phase4-tests"),
        cutoff="2026-11-01T23:59:59Z",
        price_list_sha256="a" * 64,
        plans_sha256="b" * 64,
        pre_raw_store_sha256=event_stream_digest([]),
    )


def _event(event_id: str, version: int = 1) -> dict:
    return {
        "schema_version": "1.0",
        "provider": "synthetic-token-provider",
        "account_id": "acct-demo",
        "provider_event_id": event_id,
        "record_version": version,
        "payload_sha256": content_sha256(event_id.encode("utf-8")),
        "arrival_time": "2026-11-01T00:00:00Z",
        "usage_time": "2026-10-01T08:00:00Z",
        "supersedes": None,
    }


# --- real lock contention -------------------------------------------------


def test_lock_second_connect_in_the_same_process_shares_the_instance(tmp_path):
    """Same-process opens share one DuckDB instance; they never contend.

    This documents the boundary of the single-writer guarantee: enforcement is
    cross-process only, via the OS file lock. A successful second open here
    does NOT prove exclusive ownership of anything, and no serialization of
    two runner invocations in one process is asserted or implied.
    """
    db_path = tmp_path / "pipeline.duckdb"
    holder = duckdb.connect(str(db_path))
    try:
        holder.execute("CREATE TABLE IF NOT EXISTS held (x INTEGER)")
        holder.execute("INSERT INTO held VALUES (1)")
        second = connect_store(db_path)
        try:
            value = second.execute("SELECT x FROM held").fetchone()[0]
        finally:
            second.close()
        assert value == 1
    finally:
        holder.close()


def test_lock_real_second_writer_process_is_busy(tmp_path, capsys):
    db_path = tmp_path / "pipeline.duckdb"
    holder_script = (
        "import sys, time\n"
        "import duckdb\n"
        "conn = duckdb.connect(sys.argv[1])\n"
        "conn.execute('CREATE TABLE IF NOT EXISTS held (x INTEGER)')\n"
        "conn.execute('INSERT INTO held VALUES (1)')\n"
        "print('HELD', flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(db_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = process.stdout.readline()
        assert line.strip() == "HELD", process.stderr.read()
        with pytest.raises(PipelineBusyError) as excinfo:
            connect_store(db_path)
        cause = excinfo.value.__cause__
        observed = str(cause)
        print(f"LOCK_SUBPROCESS_CLASS={type(cause).__module__}.{type(cause).__name__}")
        print(f"LOCK_SUBPROCESS_MESSAGE={observed}")
        assert isinstance(cause, duckdb.IOException)
        assert "being used by another process" in observed
        assert "write lock" in str(excinfo.value)
    finally:
        process.kill()
        process.wait(timeout=10)


def test_lock_matcher_never_reports_corruption_as_busy(tmp_path):
    db_path = tmp_path / "pipeline.duckdb"
    db_path.write_bytes(b"this is definitely not a duckdb database file")
    with pytest.raises(StoreOpenError) as excinfo:
        connect_store(db_path)
    assert not isinstance(excinfo.value, PipelineBusyError)
    assert "cannot open store" in str(excinfo.value)


# --- canonical digests ----------------------------------------------------


def test_event_stream_digest_is_sorted_and_lf_terminated():
    expected = hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    assert event_stream_digest(["beta", "alpha"]) == expected


def test_event_stream_digest_is_order_invariant():
    rows = ['{"a":1}', '{"a":2}', '{"a":3}']
    assert event_stream_digest(rows) == event_stream_digest(list(reversed(rows)))


def test_rows_stream_digest_names_each_table_and_keeps_row_order():
    left = rows_stream_digest({"t2": ["x"], "t1": ["a", "b"]})
    right = rows_stream_digest({"t1": ["a", "b"], "t2": ["x"]})
    assert left == right
    reordered = rows_stream_digest({"t1": ["b", "a"], "t2": ["x"]})
    assert left != reordered
    stream = b"t1:a\nt1:b\nt2:x\n"
    assert left == hashlib.sha256(stream).hexdigest()


# --- schema and attempts --------------------------------------------------


def test_initialize_is_idempotent(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        store.initialize()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        for required in (
            "run_history",
            "run_stages",
            "published_run",
            "validated_events",
            "stage_artifacts",
        ):
            assert required in tables
    finally:
        conn.close()


def test_start_attempt_records_invocation_and_running_status(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "order-sensitive-invocation")
        row = store.attempt_row(attempt_id)
        assert row["status"] == "running"
        assert row["invocation_sha256"] == "order-sensitive-invocation"
        assert row["pipeline_version"] == "0.4.0"
        assert row["run_fingerprint"] is None
        assert row["last_completed_stage"] is None
    finally:
        conn.close()


def test_find_resumable_requires_an_interrupted_attempt(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "inv-resume")
        assert store.find_resumable("inv-resume") is None
        store.begin()
        interrupted = store.stale_scan()
        store.commit()
        assert interrupted == [
            {"attempt_id": attempt_id, "last_completed_stage": None}
        ]
        assert store.find_resumable("inv-resume") == attempt_id
        assert store.attempt_row(attempt_id)["status"] == "interrupted"
    finally:
        conn.close()


def test_stale_scan_abandons_running_stage_checkpoints(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "inv-abandon")
        store.begin()
        store.write_run_stage(
            attempt_id,
            "raw",
            status="running",
            input_digest="in",
            output_digest="out",
            row_count=0,
            meta={},
            duration_ms=0,
        )
        store.commit()
        store.begin()
        store.stale_scan()
        store.commit()
        assert store.stage_record(attempt_id, "raw")["status"] == "abandoned"
    finally:
        conn.close()


def test_publish_sets_pointer_and_attempt_status(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "inv-publish")
        store.begin()
        published_at = store.publish(
            attempt_id,
            run_fingerprint="f" * 64,
            digest="d" * 64,
            artifact_path="f" * 64,
            digest_definition="ai-cost-margin/margin-digest/v1",
        )
        store.commit()
        pointer = store.published_pointer()
        assert pointer["attempt_id"] == attempt_id
        assert pointer["published_at_utc"] == published_at
        row = store.attempt_row(attempt_id)
        assert row["status"] == "published"
        assert row["digest"] == "d" * 64
        assert row["artifact_dir"] == "f" * 64
    finally:
        conn.close()


def test_read_status_returns_attempt_stage_checkpoints(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "inv-status")
        store.begin()
        store.write_run_stage(
            attempt_id,
            "raw",
            status="committed",
            input_digest="in",
            output_digest="out",
            row_count=7,
            meta={"counters": {"received": 7}},
            duration_ms=3,
        )
        store.commit()
    finally:
        conn.close()
    payload = read_status(db_path)
    assert payload["published"] is None
    assert len(payload["attempts"]) == 1
    row = payload["attempts"][0]
    assert row["attempt_id"] == attempt_id
    assert row["stages"]["raw"]["status"] == "committed"
    assert row["stages"]["raw"]["row_count"] == 7


def test_read_status_of_a_missing_database_is_empty(tmp_path):
    assert read_status(tmp_path / "absent.duckdb") == {
        "published": None,
        "attempts": [],
    }


# --- adapter --------------------------------------------------------------


def test_adapter_roundtrips_rows_and_ordinals(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "inv-adapter")
        adapter = DuckDbStoreAdapter(store, attempt_id)
        store.begin()
        adapter.append_manifest(
            [
                {
                    "content_sha256": "c1",
                    "status": "applied",
                    "file": "one.json",
                },
                {
                    "content_sha256": "c2",
                    "status": "duplicate_file",
                    "file": "two.json",
                },
            ]
        )
        adapter.append_dead_letter(
            [
                {
                    "content_sha256": "c3",
                    "event_index": 4,
                    "reason": "invalid_event",
                }
            ]
        )
        adapter.append_quarantine(
            [
                {
                    "content_sha256": "c4",
                    "event_index": None,
                    "reason": "changed_payload",
                }
            ]
        )
        adapter.write_report({"ingest_id": "sha256:abc", "files": [], "totals": {}})
        store.commit()

        reader = DuckDbStoreAdapter(store, attempt_id)
        manifest = reader.read_manifest()
        assert [row["content_sha256"] for row in manifest] == ["c1", "c2"]
        assert reader.read_dead_letter()[0]["event_index"] == 4
        assert reader.read_quarantine()[0]["reason"] == "changed_payload"
        report = conn.execute(
            "SELECT report_json FROM ingest_reports WHERE attempt_id = ?",
            [attempt_id],
        ).fetchone()[0]
        assert json.loads(report)["ingest_id"] == "sha256:abc"
    finally:
        conn.close()


def test_adapter_write_raw_is_verify_or_write(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "inv-raw")
        adapter = DuckDbStoreAdapter(store, attempt_id)
        data = b'{"batch_id":"batch-001"}'
        digest = content_sha256(data)
        store.begin()
        adapter.write_raw(digest, data)
        store.commit()
        target = home / "raw" / f"{digest}.json"
        assert target.read_bytes() == data
        count = conn.execute(
            "SELECT COUNT(*) FROM raw_batches WHERE content_sha256 = ?", [digest]
        ).fetchone()[0]
        assert count == 1
        # A second delivery re-verifies instead of overwriting.
        store.begin()
        adapter.write_raw(digest, data)
        store.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM raw_batches WHERE content_sha256 = ?", [digest]
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_adapter_events_and_schema_versions(tmp_path):
    conn, store, home, db_path = _open(tmp_path)
    try:
        attempt_id = _attempt(store, "inv-events")
        adapter = DuckDbStoreAdapter(store, attempt_id)
        store.begin()
        adapter.append_events([_event("evt-a"), _event("evt-b")])
        store.commit()
        assert len(adapter.read_events()) == 2
        assert store.schema_versions_seen() == {"1.0": 2}
        assert store.event_count() == 2
    finally:
        conn.close()


def test_operational_identifiers_and_timestamps_are_well_formed():
    assert utc_now().endswith("Z")
    assert utc_now().count("T") == 1
    first = new_attempt_id()
    second = new_attempt_id()
    assert first.startswith("attempt-")
    assert first != second
