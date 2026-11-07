"""Golden ingestion: the committed fixtures must reproduce the committed outputs.

The expected files were generated once from the seeded fixtures and then read
against the ingestion contract before being committed. From here on they are
frozen: a change in behaviour that alters them is a finding to explain, not a
file to refresh casually.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_cost_margin.ingest import ingest
from ai_cost_margin.store import Store, read_jsonl

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase1"
EXPECTED = FIXTURES / "expected"

# Fixed delivery order. batch-001 is delivered twice on purpose: the second
# delivery exercises the duplicate-file path inside a single run.
GOLDEN_DELIVERIES = [
    "batch-001-clean.json",
    "batch-002-retries.json",
    "batch-003-changed-payload.json",
    "batch-004-correction.json",
    "batch-005-malformed.json",
    "batch-006-unavailable-fields.json",
    "batch-007-invalid-json.json",
    "batch-001-clean.json",
]

PARSEABLE_BATCHES = [
    "batch-001-clean.json",
    "batch-002-retries.json",
    "batch-003-changed-payload.json",
    "batch-004-correction.json",
    "batch-005-malformed.json",
    "batch-006-unavailable-fields.json",
]


def _golden_paths() -> list[Path]:
    return [FIXTURES / name for name in GOLDEN_DELIVERIES]


def _expected(name: str) -> bytes:
    return (EXPECTED / name).read_bytes()


def test_golden_outputs_match_committed_expected(tmp_path):
    ingest(_golden_paths(), tmp_path / "store")
    store = Store(tmp_path / "store")

    assert store.events_path.read_bytes() == _expected("events.jsonl")
    assert store.manifest_path.read_bytes() == _expected("manifest.jsonl")
    assert store.dead_letter_path.read_bytes() == _expected("dead-letter.jsonl")
    assert store.quarantine_path.read_bytes() == _expected("quarantine.jsonl")
    assert store.report_path.read_bytes() == _expected("report.json")


def test_two_independent_runs_are_byte_identical(tmp_path):
    ingest(_golden_paths(), tmp_path / "a")
    ingest(_golden_paths(), tmp_path / "b")

    first, second = Store(tmp_path / "a"), Store(tmp_path / "b")
    assert first.events_path.read_bytes() == second.events_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.dead_letter_path.read_bytes() == second.dead_letter_path.read_bytes()
    assert first.quarantine_path.read_bytes() == second.quarantine_path.read_bytes()
    assert first.report_path.read_bytes() == second.report_path.read_bytes()


def test_reingesting_the_same_inputs_adds_no_events(tmp_path):
    home = tmp_path / "store"
    ingest(_golden_paths(), home)
    before = Store(home).events_path.read_bytes()

    report = ingest(_golden_paths(), home)

    assert Store(home).events_path.read_bytes() == before
    assert report.totals["inserted"] == 0
    assert report.totals["duplicates"] == 0

    # Only applied files are marked as ingested, so the deliberately unparseable
    # file is retried and reported as failed again. Its dead-letter row is
    # deduplicated, so the dead-letter store does not grow.
    statuses = [outcome.status for outcome in report.files]
    assert statuses.count("duplicate_file_ignored") == len(GOLDEN_DELIVERIES) - 1
    assert statuses.count("failed") == 1
    failed = [outcome for outcome in report.files if outcome.status == "failed"]
    assert failed[0].file_name == "batch-007-invalid-json.json"
    assert failed[0].reason == "invalid_json"
    assert report.totals["dead_lettered"] == 1

    store = Store(home)
    assert len(read_jsonl(store.dead_letter_path)) == 10
    assert len(read_jsonl(store.quarantine_path)) == 2


def test_totals_recounted_independently_from_the_fixtures(tmp_path):
    """Arithmetic straight from the committed fixtures, not from ingest output."""
    batches = {
        name: json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for name in PARSEABLE_BATCHES
    }
    total_events = sum(len(batch["events"]) for batch in batches.values())
    malformed_events = len(batches["batch-005-malformed.json"]["events"])
    quarantined_events = len(batches["batch-003-changed-payload.json"]["events"])
    in_file_duplicates = 1  # batch-002 delivers one event twice

    report = ingest(_golden_paths(), tmp_path / "store")

    assert total_events == 31
    assert report.totals["received"] == total_events
    assert report.totals["dead_lettered"] == malformed_events + 1  # + the invalid-JSON file
    assert report.totals["validated"] == total_events - malformed_events
    assert report.totals["quarantined"] == quarantined_events
    assert report.totals["duplicates"] == in_file_duplicates
    assert report.totals["inserted"] == (
        report.totals["validated"] - report.totals["duplicates"] - report.totals["quarantined"]
    )
    assert len(read_jsonl(Store(tmp_path / "store").events_path)) == report.totals["inserted"]
