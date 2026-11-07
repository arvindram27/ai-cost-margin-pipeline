"""Dedup, versioning, quarantine and re-run convergence tests.

Every test builds its own batches in ``tmp_path``; nothing here touches the
repo's ``var/`` store or the committed fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_cost_margin.ingest import ingest
from ai_cost_margin.schema import SCHEMA_VERSION
from ai_cost_margin.store import Store, read_jsonl

_DELETE = object()

PROVIDER = "synthetic-token-provider"
ACCOUNT = "acct-demo"
ARRIVAL = "2026-11-01T00:00:00Z"


def _event(event_id: str = "evt-001", **overrides):
    event = {
        "schema_version": SCHEMA_VERSION,
        "provider_event_id": event_id,
        "attempt_id": "1",
        "request_id": f"req-{event_id}",
        "model": "synth-small",
        "feature": "demo-assistant",
        "workload_type": "short_light",
        "customer_id": "cust-001",
        "usage_time": "2026-10-01T08:00:00Z",
        "tokens_input": 100,
        "tokens_output": 20,
        "tokens_cached": 0,
        "unavailable_fields": [],
        "record_version": 1,
        "supersedes": None,
    }
    for key, value in overrides.items():
        if value is _DELETE:
            event.pop(key, None)
        else:
            event[key] = value
    return event


def _write_batch(
    directory: Path,
    name: str,
    events: list[dict],
    *,
    batch_id: str = "batch-test",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "provider": PROVIDER,
        "account_id": ACCOUNT,
        "arrival_time": ARRIVAL,
        "events": events,
    }
    path = directory / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _store(home: Path) -> Store:
    return Store(home)


def test_duplicate_file_delivery_in_one_invocation(tmp_path):
    source = tmp_path / "in"
    batch = _write_batch(source, "a.json", [_event()])
    home = tmp_path / "store"

    report = ingest([batch, batch], home)

    assert [outcome.status for outcome in report.files] == ["applied", "duplicate_file_ignored"]
    assert len(read_jsonl(_store(home).events_path)) == 1
    assert len(read_jsonl(_store(home).manifest_path)) == 2


def test_duplicate_file_delivery_across_invocations(tmp_path):
    source = tmp_path / "in"
    batch = _write_batch(source, "a.json", [_event()])
    home = tmp_path / "store"

    ingest([batch], home)
    first_events = _store(home).events_path.read_bytes()
    report = ingest([batch], home)

    assert [outcome.status for outcome in report.files] == ["duplicate_file_ignored"]
    assert _store(home).events_path.read_bytes() == first_events


def test_identical_event_redelivery_is_a_duplicate(tmp_path):
    source = tmp_path / "in"
    first = _write_batch(source, "a.json", [_event()], batch_id="batch-a")
    second = _write_batch(source, "b.json", [_event()], batch_id="batch-b")
    home = tmp_path / "store"

    report = ingest([first, second], home)

    assert report.totals["duplicates"] == 2 - 1  # one insert, one duplicate
    assert report.totals["inserted"] == 1
    assert len(read_jsonl(_store(home).events_path)) == 1


def test_billable_retries_with_distinct_event_ids_are_kept(tmp_path):
    source = tmp_path / "in"
    attempts = [
        _event("evt-attempt-1", attempt_id="1", request_id="req-retry", tokens_output=900),
        _event("evt-attempt-2", attempt_id="2", request_id="req-retry", tokens_output=940),
    ]
    batch = _write_batch(source, "retries.json", attempts)
    home = tmp_path / "store"

    report = ingest([batch], home)

    assert report.totals["inserted"] == 2
    rows = read_jsonl(_store(home).events_path)
    assert [row["attempt_id"] for row in rows] == ["1", "2"]
    assert {row["request_id"] for row in rows} == {"req-retry"}


def test_changed_payload_at_same_version_is_quarantined(tmp_path):
    source = tmp_path / "in"
    original = _write_batch(source, "a.json", [_event()], batch_id="batch-a")
    changed = _write_batch(
        source, "b.json", [_event(tokens_input=101)], batch_id="batch-b"
    )
    home = tmp_path / "store"

    report = ingest([original, changed], home)

    assert report.totals["quarantined"] == 1
    assert report.totals["inserted"] == 1
    quarantine = read_jsonl(_store(home).quarantine_path)
    assert [row["reason"] for row in quarantine] == ["same_key_changed_payload"]


def test_self_referencing_supersedes_without_version_bump_is_quarantined(tmp_path):
    """Amendment A1: supersedes pointing at itself is not a correction."""
    source = tmp_path / "in"
    original = _write_batch(source, "a.json", [_event()], batch_id="batch-a")
    changed = _write_batch(
        source,
        "b.json",
        [_event(tokens_input=101, supersedes="evt-001")],
        batch_id="batch-b",
    )
    home = tmp_path / "store"

    report = ingest([original, changed], home)

    assert report.totals["quarantined"] == 1
    assert report.totals["inserted"] == 1
    quarantine = read_jsonl(_store(home).quarantine_path)
    assert [row["reason"] for row in quarantine] == ["same_key_changed_payload"]


def test_version_bump_is_accepted(tmp_path):
    source = tmp_path / "in"
    v1 = _write_batch(source, "a.json", [_event()], batch_id="batch-a")
    v2 = _write_batch(
        source, "b.json", [_event(tokens_input=250, record_version=2)], batch_id="batch-b"
    )
    home = tmp_path / "store"

    report = ingest([v1, v2], home)

    assert report.totals["inserted"] == 2
    assert report.totals["quarantined"] == 0
    rows = read_jsonl(_store(home).events_path)
    assert [row["record_version"] for row in rows] == [1, 2]


def test_redelivery_of_an_older_version_is_a_duplicate_not_a_regression(tmp_path):
    """Amendment A2: payload history covers every stored version of the key."""
    source = tmp_path / "in"
    v1 = _write_batch(source, "a.json", [_event()], batch_id="batch-a")
    v2 = _write_batch(
        source, "b.json", [_event(tokens_input=250, record_version=2)], batch_id="batch-b"
    )
    repeat_v1 = _write_batch(source, "c.json", [_event()], batch_id="batch-c")
    home = tmp_path / "store"

    report = ingest([v1, v2, repeat_v1], home)

    assert report.totals["duplicates"] == 1
    assert report.totals["quarantined"] == 0
    assert len(read_jsonl(_store(home).events_path)) == 2
    assert read_jsonl(_store(home).quarantine_path) == []


def test_version_regression_is_quarantined(tmp_path):
    source = tmp_path / "in"
    v3 = _write_batch(
        source, "a.json", [_event(tokens_input=300, record_version=3)], batch_id="batch-a"
    )
    v2 = _write_batch(
        source, "b.json", [_event(tokens_input=200, record_version=2)], batch_id="batch-b"
    )
    home = tmp_path / "store"

    report = ingest([v3, v2], home)

    assert report.totals["quarantined"] == 1
    quarantine = read_jsonl(_store(home).quarantine_path)
    assert [row["reason"] for row in quarantine] == ["version_regression"]


def test_new_key_correction_accepted_and_dangling_supersedes_counted(tmp_path):
    source = tmp_path / "in"
    original = _write_batch(source, "a.json", [_event("evt-001")], batch_id="batch-a")
    correction = _write_batch(
        source,
        "b.json",
        [_event("evt-002", supersedes="evt-001", tokens_input=50)],
        batch_id="batch-b",
    )
    dangling = _write_batch(
        source,
        "c.json",
        [_event("evt-003", supersedes="evt-does-not-exist")],
        batch_id="batch-c",
    )
    home = tmp_path / "store"

    report = ingest([original, correction, dangling], home)

    assert report.totals["inserted"] == 3
    assert report.totals["dangling_supersedes"] == 1


def _truncate_manifest(home: Path) -> None:
    """Simulate an interruption between the row appends and the marker."""
    _store(home).manifest_path.write_text("", encoding="utf-8")


def test_dead_letter_rows_converge_after_a_lost_marker(tmp_path):
    """Amendment A3: a re-run must not duplicate dead-letter rows."""
    source = tmp_path / "in"
    batch = _write_batch(source, "bad.json", [_event(prompt="unexpected")])
    home = tmp_path / "store"

    ingest([batch], home)
    before = _store(home).dead_letter_path.read_bytes()
    assert len(read_jsonl(_store(home).dead_letter_path)) == 1

    _truncate_manifest(home)
    ingest([batch], home)

    assert _store(home).dead_letter_path.read_bytes() == before


def test_quarantine_rows_converge_after_a_lost_marker(tmp_path):
    """Amendment A3: a re-run must not duplicate quarantine rows."""
    source = tmp_path / "in"
    original = _write_batch(source, "a.json", [_event()], batch_id="batch-a")
    changed = _write_batch(
        source, "b.json", [_event(tokens_input=101)], batch_id="batch-b"
    )
    home = tmp_path / "store"

    ingest([original, changed], home)
    before = _store(home).quarantine_path.read_bytes()
    assert len(read_jsonl(_store(home).quarantine_path)) == 1

    _truncate_manifest(home)
    ingest([changed], home)

    assert _store(home).quarantine_path.read_bytes() == before


def test_report_is_written_for_every_run(tmp_path):
    source = tmp_path / "in"
    batch = _write_batch(source, "a.json", [_event()])
    home = tmp_path / "store"

    ingest([batch], home)

    report = json.loads(_store(home).report_path.read_text(encoding="utf-8"))
    assert report["totals"]["inserted"] == 1
    assert report["files"][0]["status"] == "applied"
