"""End-to-end Phase 4 runner stages: golden equality, publish, replay, checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_cost_margin.pipeline import SEVEN_FILES, main, run_pipeline
from ai_cost_margin.runstore import PIPELINE_DB_NAME, RunStore, connect_store
from ai_cost_margin.store import canonical_json

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PRICES = ROOT / "fixtures" / "phase2" / "price-list-synthetic-v1.json"
PLANS = ROOT / "fixtures" / "phase3" / "customer-plans-simulated-v1.json"
EXPECTED = ROOT / "fixtures" / "phase3" / "expected"
CUTOFF = "2026-11-01T23:59:59Z"
GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"
GOLDEN_FILES = (
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "lineage.jsonl",
    "margin-digest.txt",
)


def _run(tmp_path: Path, **overrides):
    home = tmp_path / "home"
    out = tmp_path / "runs"
    kwargs = dict(
        home=home,
        batches=[PHASE1],
        prices=PRICES,
        plans=PLANS,
        cutoff=CUTOFF,
        out_root=out,
        repo_root=ROOT,
    )
    kwargs.update(overrides)
    result = run_pipeline(**kwargs)
    return result, home, out


def test_pipeline_digest_matches_the_pinned_golden(tmp_path):
    result, home, out = _run(tmp_path)
    assert result["digest"] == GOLDEN_DIGEST
    assert result["replayed"] is False
    assert result["run_dir"].name == result["run_fingerprint"]


def test_pipeline_publishes_exactly_the_seven_files(tmp_path):
    result, home, out = _run(tmp_path)
    run_dir = result["run_dir"]
    assert sorted(path.name for path in run_dir.iterdir()) == sorted(SEVEN_FILES)
    assert not any(path.name.startswith(".tmp-") for path in out.iterdir())


def test_pipeline_five_files_are_byte_identical_to_the_golden(tmp_path):
    result, home, out = _run(tmp_path)
    for name in GOLDEN_FILES:
        assert (result["run_dir"] / name).read_bytes() == (EXPECTED / name).read_bytes(), name


def test_pipeline_manifest_uses_the_backend_neutral_store_shape(tmp_path):
    result, home, out = _run(tmp_path)
    manifest = json.loads((result["run_dir"] / "run-manifest.json").read_text(encoding="utf-8"))
    inputs = manifest["inputs"]
    assert inputs["store_backend"] == "duckdb"
    assert inputs["store_locator"] == "pipeline.duckdb"
    assert inputs["events_in_store"] == 19
    assert inputs["events_included"] == 19
    assert inputs["events_excluded_by_cutoff"] == 0
    assert "store_events_file" not in inputs
    golden_lines = [
        line
        for line in (PHASE1 / "expected" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    expected_stream = hashlib.sha256(
        "".join(row + "\n" for row in sorted(golden_lines)).encode("utf-8")
    ).hexdigest()
    assert inputs["store_events_sha256"] == expected_stream


def test_pipeline_manifest_carries_no_absolute_paths(tmp_path):
    result, home, out = _run(tmp_path)
    text = (result["run_dir"] / "run-manifest.json").read_text(encoding="utf-8")
    report_text = (result["run_dir"] / "margin-report.json").read_text(encoding="utf-8")
    assert str(ROOT) not in text
    assert str(ROOT) not in report_text
    assert "C:\\" not in text
    assert "C:\\" not in report_text
    assert "pipeline.duckdb" in text


def test_pipeline_report_contains_no_operational_metadata(tmp_path):
    result, home, out = _run(tmp_path)
    report = json.loads((result["run_dir"] / "margin-report.json").read_text(encoding="utf-8"))
    for banned in (
        "attempt_id",
        "invocation_sha256",
        "started_at_utc",
        "finished_at_utc",
        "published_at_utc",
        "duration_ms",
        "published",
        "store_backend",
        "store_locator",
        "received",
        "inserted",
        "duplicates",
    ):
        assert banned not in report, banned
    assert report["digest"] == GOLDEN_DIGEST
    assert report["run_fingerprint"] == result["run_fingerprint"]


def test_pipeline_exact_replay_is_reported_and_not_rewritten(tmp_path):
    first, home, out = _run(tmp_path)
    before = {name: (first["run_dir"] / name).read_bytes() for name in SEVEN_FILES}
    second = run_pipeline(
        home=home,
        batches=[PHASE1],
        prices=PRICES,
        plans=PLANS,
        cutoff=CUTOFF,
        out_root=out,
        repo_root=ROOT,
    )
    assert second["replayed"] is True
    assert second["digest"] == first["digest"]
    assert second["run_dir"] == first["run_dir"]
    after = {name: (first["run_dir"] / name).read_bytes() for name in SEVEN_FILES}
    assert before == after


def test_pipeline_stage_checkpoints_are_committed_in_order(tmp_path):
    result, home, out = _run(tmp_path)
    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        store = RunStore(conn, home, home / PIPELINE_DB_NAME)
        stages = {
            stage: store.stage_record(result["attempt_id"], stage)
            for stage in ("raw", "validated", "costed", "aggregated")
        }
    finally:
        conn.close()
    assert [stages[name]["status"] for name in ("raw", "validated", "costed", "aggregated")] == [
        "committed",
        "committed",
        "committed",
        "committed",
    ]
    assert stages["raw"]["output_digest"] == stages["validated"]["input_digest"]
    assert stages["costed"]["input_digest"]
    assert stages["aggregated"]["row_count"] > 0


def test_status_json_cli_reports_the_published_pointer(tmp_path, capsys):
    result, home, out = _run(tmp_path)
    assert main(["--home", str(home), "--status-json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["published"]["attempt_id"] == result["attempt_id"]
    assert payload["published"]["digest"] == GOLDEN_DIGEST
    published = [row for row in payload["attempts"] if row["status"] == "published"]
    assert len(published) == 1
    assert published[0]["attempt_id"] == result["attempt_id"]


def test_cli_prints_a_canonical_run_result_line(tmp_path, capsys):
    home = tmp_path / "home"
    out = tmp_path / "runs"
    exit_code = main(
        [
            "--home",
            str(home),
            "--batches",
            str(PHASE1),
            "--prices",
            str(PRICES),
            "--plans",
            str(PLANS),
            "--cutoff",
            CUTOFF,
            "--out",
            str(out),
        ]
    )
    assert exit_code == 0
    stdout = capsys.readouterr().out
    line = next(line for line in stdout.splitlines() if line.startswith("run_result "))
    payload = json.loads(line[len("run_result ") :])
    assert payload["digest"] == GOLDEN_DIGEST
    assert payload["replayed"] is False
    assert line == "run_result " + canonical_json(payload)


def test_from_stage_recomputes_the_named_and_later_stages(tmp_path):
    import os
    import subprocess
    import sys

    from ai_cost_margin.runstore import read_status

    home = tmp_path / "home"
    out = tmp_path / "runs"
    env = dict(os.environ)
    src = str(ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    env["AI_COST_MARGIN_KILL_POINT"] = "before_costed_commit"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_cost_margin.pipeline",
            "--home",
            str(home),
            "--batches",
            str(PHASE1),
            "--prices",
            str(PRICES),
            "--plans",
            str(PLANS),
            "--cutoff",
            CUTOFF,
            "--out",
            str(out),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 137, (completed.stdout, completed.stderr)

    # read_status never stale-scans: the killed attempt is still 'running'
    # until the next pipeline invocation marks it 'interrupted'.
    before = read_status(home / PIPELINE_DB_NAME)
    assert len(before["attempts"]) == 1
    killed = before["attempts"][0]
    assert killed["status"] == "running"
    assert killed["last_completed_stage"] == "validated"

    second = run_pipeline(
        home=home,
        batches=[PHASE1],
        prices=PRICES,
        plans=PLANS,
        cutoff=CUTOFF,
        out_root=out,
        from_stage="costed",
        repo_root=ROOT,
    )
    assert second["digest"] == GOLDEN_DIGEST

    after = read_status(home / PIPELINE_DB_NAME)
    by_id = {row["attempt_id"]: row for row in after["attempts"]}
    assert by_id[killed["attempt_id"]]["status"] == "interrupted"
    assert by_id[killed["attempt_id"]]["last_completed_stage"] == "validated"
    assert by_id[second["attempt_id"]]["status"] == "published"

    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        store = RunStore(conn, home, home / PIPELINE_DB_NAME)
        raw = store.stage_record(second["attempt_id"], "raw")
        validated = store.stage_record(second["attempt_id"], "validated")
        costed = store.stage_record(second["attempt_id"], "costed")
        aggregated = store.stage_record(second["attempt_id"], "aggregated")
    finally:
        conn.close()
    assert raw["status"] == "adopted"
    assert validated["status"] == "adopted"
    assert costed["status"] == "committed"
    assert aggregated["status"] == "committed"
