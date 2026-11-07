"""Dead-letter path: reason mapping, file-level failures, verbatim records.

The malformed fixture is read from the committed set, so this also pins the
fixture/contract agreement that the contract document describes.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_cost_margin.ingest import ingest
from ai_cost_margin.schema import SCHEMA_VERSION
from ai_cost_margin.store import Store, read_jsonl

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase1"

# Amendment A7: batch-005 is 9 events - 7 single-rule failures (indices 0-6)
# plus 2 unavailable_fields inconsistencies (indices 7-8).
EXPECTED_REASONS = [
    "missing_required_field",
    "unknown_field",
    "wrong_type",
    "wrong_type",
    "negative_token_count",
    "invalid_timestamp",
    "unknown_schema_version",
    "unavailable_marker_inconsistent",
    "unavailable_marker_inconsistent",
]


def _store(home: Path) -> Store:
    return Store(home)


def test_batch_005_reason_mapping(tmp_path):
    report = ingest([FIXTURES / "batch-005-malformed.json"], tmp_path / "store")

    assert report.totals["received"] == 9
    assert report.totals["dead_lettered"] == 9
    assert report.totals["validated"] == 0

    rows = read_jsonl(_store(tmp_path / "store").dead_letter_path)
    assert [row["reason"] for row in rows] == EXPECTED_REASONS
    assert [row["event_index"] for row in rows] == list(range(9))
    assert _store(tmp_path / "store").events_path.exists() is False


def test_invalid_json_is_a_file_level_failure(tmp_path):
    report = ingest([FIXTURES / "batch-007-invalid-json.json"], tmp_path / "store")
    store = _store(tmp_path / "store")

    assert report.totals["received"] == 0
    assert report.totals["dead_lettered"] == 1

    rows = read_jsonl(store.dead_letter_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == "invalid_json"
    assert rows[0]["event_index"] is None
    assert rows[0]["record"] is None

    manifest = read_jsonl(store.manifest_path)
    assert len(manifest) == 1
    assert manifest[0]["status"] == "failed"
    assert manifest[0]["reason"] == "invalid_json"


def test_dead_letter_keeps_the_unknown_field_verbatim(tmp_path):
    """Amendment A4: rejected synthetic records are retained, unknown fields too."""
    ingest([FIXTURES / "batch-005-malformed.json"], tmp_path / "store")

    rows = read_jsonl(_store(tmp_path / "store").dead_letter_path)
    unknown = [row for row in rows if row["reason"] == "unknown_field"]
    assert len(unknown) == 1
    assert unknown[0]["record"]["prompt"] == "synthetic text that must never be needed"


def _write_batch(directory: Path, name: str, **overrides) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": "batch-envelope",
        "provider": "synthetic-token-provider",
        "account_id": "acct-demo",
        "arrival_time": "2026-11-01T00:00:00Z",
        "events": [],
    }
    for key, value in overrides.items():
        if value is None and key in payload:
            payload.pop(key)
        else:
            payload[key] = value
    path = directory / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_empty_event_list_is_valid(tmp_path):
    batch = _write_batch(tmp_path / "in", "empty.json", events=[])
    report = ingest([batch], tmp_path / "store")

    assert report.files[0].status == "applied"
    assert report.totals["received"] == 0
    assert read_jsonl(_store(tmp_path / "store").manifest_path)[0]["status"] == "applied"


def test_envelope_unknown_field_fails_the_file(tmp_path):
    batch = _write_batch(tmp_path / "in", "extra.json", events=[], source_system="legacy")
    report = ingest([batch], tmp_path / "store")

    assert report.files[0].status == "failed"
    rows = read_jsonl(_store(tmp_path / "store").dead_letter_path)
    assert rows[0]["reason"] == "unknown_field"
    assert rows[0]["event_index"] is None


def test_missing_events_key_fails_the_file(tmp_path):
    batch = _write_batch(tmp_path / "in", "noevents.json", events=None)
    report = ingest([batch], tmp_path / "store")

    assert report.files[0].status == "failed"
    rows = read_jsonl(_store(tmp_path / "store").dead_letter_path)
    assert rows[0]["reason"] == "missing_events"
