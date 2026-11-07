"""Golden costing: committed Phase 1 batches in, committed Phase 2 outputs out.

The input store is built from the committed Phase 1 batch files, never from the
gitignored ``var/`` store, so this test is reproducible from a clean checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_cost_margin import costing
from ai_cost_margin.costing import cost_events
from ai_cost_margin.ingest import ingest
from ai_cost_margin.pricing import load_price_list
from ai_cost_margin.store import Store, canonical_json, pretty_json, read_jsonl

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PHASE2 = ROOT / "fixtures" / "phase2"

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


def _golden_home(tmp_path: Path) -> Path:
    home = tmp_path / "store"
    ingest([PHASE1 / name for name in GOLDEN_DELIVERIES], home)
    return home


def _rendered(tmp_path: Path) -> tuple[bytes, bytes]:
    home = _golden_home(tmp_path)
    events = read_jsonl(Store(home).events_path)
    price_list = load_price_list(PHASE2 / "price-list-synthetic-v1.json")
    rows, report = cost_events(events, price_list)
    rendered_rows = "".join(canonical_json(row) + "\n" for row in rows)
    rendered_report = pretty_json(report) + "\n"
    return rendered_rows.encode("utf-8"), rendered_report.encode("utf-8")


def test_golden_costed_events_match_committed_expected(tmp_path):
    rows, _ = _rendered(tmp_path)
    assert rows == (PHASE2 / "expected" / "costed-events.jsonl").read_bytes()


def test_golden_report_matches_committed_expected(tmp_path):
    _, report = _rendered(tmp_path)
    assert report == (PHASE2 / "expected" / "costing-report.json").read_bytes()


def test_two_independent_runs_are_byte_identical(tmp_path):
    first_rows, first_report = _rendered(tmp_path / "a")
    second_rows, second_report = _rendered(tmp_path / "b")
    assert first_rows == second_rows
    assert first_report == second_report


def test_the_committed_golden_composition_is_reported(tmp_path):
    home = _golden_home(tmp_path)
    events = read_jsonl(Store(home).events_path)
    price_list = load_price_list(PHASE2 / "price-list-synthetic-v1.json")
    _, report = cost_events(events, price_list)
    assert report["events_read"] == len(events)
    assert report["events_read"] == (
        report["events_costed"] + report["events_unpriced"] + report["events_superseded"]
    )


@pytest.mark.parametrize(
    "fixture_name",
    [
        "price-list-gap.json",
        "price-list-overlap.json",
        "price-list-mixed-currency.json",
        "price-list-bad-interval.json",
    ],
)
def test_an_invalid_schedule_exits_two_and_writes_nothing(tmp_path, capsys, fixture_name):
    out_dir = tmp_path / "out"
    code = costing.main(
        [
            "--home",
            str(tmp_path / "store"),
            "--prices",
            str(PHASE2 / "invalid" / fixture_name),
            "--out",
            str(out_dir),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "price schedule is invalid" in captured.err
    assert not out_dir.exists()


def test_an_existing_output_directory_is_rejected(tmp_path, capsys):
    home = _golden_home(tmp_path)
    out_dir = tmp_path / "existing-output"
    out_dir.mkdir()
    code = costing.main(
        [
            "--home",
            str(home),
            "--prices",
            str(PHASE2 / "price-list-synthetic-v1.json"),
            "--out",
            str(out_dir),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "already exists" in captured.err
    assert list(out_dir.iterdir()) == []


def test_a_successful_cli_run_writes_both_outputs(tmp_path):
    home = _golden_home(tmp_path)
    out_dir = tmp_path / "fresh-output"
    code = costing.main(
        [
            "--home",
            str(home),
            "--prices",
            str(PHASE2 / "price-list-synthetic-v1.json"),
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    assert (out_dir / "costed-events.jsonl").is_file()
    assert (out_dir / "costing-report.json").is_file()
    assert sorted(path.name for path in out_dir.iterdir()) == [
        "costed-events.jsonl",
        "costing-report.json",
    ]
