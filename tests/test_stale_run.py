"""Interrupted-attempt detection, prefix adoption, and resume correctness.

Kills are produced by a real subprocess at an audited kill point; resumes run in
this process against the same scratch home. Nothing here touches the repo's
``var/`` store.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_cost_margin.pipeline import SEVEN_FILES, run_pipeline
from ai_cost_margin.runstore import (
    PIPELINE_DB_NAME,
    RunStore,
    connect_store,
    read_status,
)
from ai_cost_margin.store import canonical_json

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PRICES = ROOT / "fixtures" / "phase2" / "price-list-synthetic-v1.json"
PLANS = ROOT / "fixtures" / "phase3" / "customer-plans-simulated-v1.json"
CUTOFF = "2026-11-01T23:59:59Z"
GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"


def _pipeline_argv(home: Path, out: Path, cutoff: str = CUTOFF) -> list[str]:
    return [
        "--home",
        str(home),
        "--batches",
        str(PHASE1),
        "--batches",
        str(PHASE1 / "batch-001-clean.json"),
        "--prices",
        str(PRICES),
        "--plans",
        str(PLANS),
        "--cutoff",
        cutoff,
        "--out",
        str(out),
    ]


def _kill(home: Path, out: Path, point: str) -> None:
    env = dict(os.environ)
    src = str(ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    env["AI_COST_MARGIN_KILL_POINT"] = point
    completed = subprocess.run(
        [sys.executable, "-m", "ai_cost_margin.pipeline", *_pipeline_argv(home, out)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 137, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _resume(home: Path, out: Path, **overrides):
    kwargs = dict(
        home=home,
        batches=[PHASE1, PHASE1 / "batch-001-clean.json"],
        prices=PRICES,
        plans=PLANS,
        cutoff=CUTOFF,
        out_root=out,
        repo_root=ROOT,
    )
    kwargs.update(overrides)
    return run_pipeline(**kwargs)


def _clean(tmp_path: Path):
    home = tmp_path / "clean" / "home"
    out = tmp_path / "clean" / "runs"
    return _resume(home, out), home, out


def _attempt_row(home: Path, attempt_id: str) -> dict:
    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        store = RunStore(conn, home, home / PIPELINE_DB_NAME)
        return store.attempt_row(attempt_id)
    finally:
        conn.close()


def _interrupted(home: Path) -> list[dict]:
    payload = read_status(home / PIPELINE_DB_NAME)
    return [row for row in payload["attempts"] if row["status"] == "interrupted"]


def test_kill_before_raw_commit_leaves_no_partial_stage(tmp_path):
    home = tmp_path / "kill1" / "home"
    out = tmp_path / "kill1" / "runs"
    _kill(home, out, "before_raw_commit")
    resumed = _resume(home, out)
    assert resumed["digest"] == GOLDEN_DIGEST

    interrupted = _interrupted(home)
    assert len(interrupted) == 1
    row = interrupted[0]
    assert row["last_completed_stage"] is None
    assert row["stages"] == {}
    assert row["digest"] is None
    assert row["artifact_dir"] is None


def test_kill_before_costed_commit_leaves_only_committed_stages(tmp_path):
    home = tmp_path / "kill2" / "home"
    out = tmp_path / "kill2" / "runs"
    _kill(home, out, "before_costed_commit")
    resumed = _resume(home, out)
    assert resumed["digest"] == GOLDEN_DIGEST

    interrupted = _interrupted(home)
    assert len(interrupted) == 1
    row = interrupted[0]
    assert row["last_completed_stage"] == "validated"
    assert sorted(row["stages"]) == ["raw", "validated"]
    assert row["stages"]["raw"]["status"] == "committed"
    assert row["stages"]["validated"]["status"] == "committed"
    assert "costed" not in row["stages"]


def test_resume_after_costed_kill_adopts_the_verified_prefix(tmp_path):
    home = tmp_path / "adopt" / "home"
    out = tmp_path / "adopt" / "runs"
    _kill(home, out, "before_costed_commit")
    resumed = _resume(home, out)
    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        store = RunStore(conn, home, home / PIPELINE_DB_NAME)
        statuses = {
            stage: store.stage_record(resumed["attempt_id"], stage)["status"]
            for stage in ("raw", "validated", "costed", "aggregated")
        }
        row = store.attempt_row(resumed["attempt_id"])
    finally:
        conn.close()
    assert statuses == {
        "raw": "adopted",
        "validated": "adopted",
        "costed": "committed",
        "aggregated": "committed",
    }
    assert row["status"] == "published"
    assert row["received"] == 31
    assert row["inserted"] == 19
    assert row["records_validated"] == 22
    assert row["dead_lettered"] == 10
    assert row["last_completed_stage"] == "aggregated"


def test_resumed_seven_files_match_a_clean_run_byte_for_byte(tmp_path):
    clean, clean_home, clean_out = _clean(tmp_path)
    home = tmp_path / "resume" / "home"
    out = tmp_path / "resume" / "runs"
    _kill(home, out, "before_costed_commit")
    resumed = _resume(home, out)
    assert resumed["digest"] == clean["digest"] == GOLDEN_DIGEST
    for name in SEVEN_FILES:
        assert (resumed["run_dir"] / name).read_bytes() == (
            clean["run_dir"] / name
        ).read_bytes(), name


def test_adopted_counters_live_in_history_not_in_the_published_files(tmp_path):
    home = tmp_path / "counters" / "home"
    out = tmp_path / "counters" / "runs"
    _kill(home, out, "before_costed_commit")
    resumed = _resume(home, out)
    row = _attempt_row(home, resumed["attempt_id"])
    assert row["received"] == 31
    assert row["records_validated"] == 22
    assert row["dead_lettered"] == 10
    report = json.loads(
        (resumed["run_dir"] / "margin-report.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (resumed["run_dir"] / "run-manifest.json").read_text(encoding="utf-8")
    )
    for banned in ("received", "inserted", "duplicates", "quarantined", "dead_lettered"):
        assert banned not in report
        assert banned not in manifest["inputs"]


def test_no_resume_runs_the_raw_stage_again(tmp_path):
    home = tmp_path / "noresume" / "home"
    out = tmp_path / "noresume" / "runs"
    _kill(home, out, "before_costed_commit")
    before = read_status(home / PIPELINE_DB_NAME)
    unfinished = [
        row for row in before["attempts"] if row["status"] in ("running", "interrupted")
    ]
    assert len(unfinished) == 1
    result = _resume(home, out, no_resume=True)
    assert result["digest"] == GOLDEN_DIGEST
    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        store = RunStore(conn, home, home / PIPELINE_DB_NAME)
        raw = store.stage_record(result["attempt_id"], "raw")
    finally:
        conn.close()
    assert raw["status"] == "committed"
    interrupted_after = _interrupted(home)
    assert [row["attempt_id"] for row in interrupted_after] == [
        unfinished[0]["attempt_id"]
    ]


def test_a_different_invocation_does_not_adopt_the_interrupted_prefix(tmp_path):
    home = tmp_path / "cutoff" / "home"
    out = tmp_path / "cutoff" / "runs"
    _kill(home, out, "before_costed_commit")
    result = _resume(home, out, cutoff="2026-11-02T00:00:00Z")
    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        store = RunStore(conn, home, home / PIPELINE_DB_NAME)
        raw = store.stage_record(result["attempt_id"], "raw")
        stages = store.stage_record(result["attempt_id"], "validated")
    finally:
        conn.close()
    assert raw["status"] == "committed"
    assert stages["status"] == "committed"
    assert len(_interrupted(home)) == 1


def test_resume_does_not_adopt_when_the_live_store_changed(tmp_path):
    home = tmp_path / "changed" / "home"
    out = tmp_path / "changed" / "runs"
    _kill(home, out, "before_costed_commit")

    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        row = conn.execute(
            "SELECT event_json FROM validated_events "
            "WHERE json_extract_string(event_json, '$.supersedes') IS NULL "
            "ORDER BY provider_event_id LIMIT 1"
        ).fetchone()
        assert row is not None
        event = json.loads(row[0])
        event["provider_event_id"] = "evt-phase4-extra"
        event["payload_sha256"] = hashlib.sha256(b"phase4-extra").hexdigest()
        conn.execute(
            "INSERT INTO validated_events (provider, account_id, provider_event_id, "
            "record_version, event_json, payload_sha256, arrival_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                event["provider"],
                event["account_id"],
                event["provider_event_id"],
                event["record_version"],
                canonical_json(event),
                event["payload_sha256"],
                event["arrival_time"],
            ],
        )
    finally:
        conn.close()

    result = _resume(home, out)
    conn = connect_store(home / PIPELINE_DB_NAME)
    try:
        store = RunStore(conn, home, home / PIPELINE_DB_NAME)
        raw = store.stage_record(result["attempt_id"], "raw")
        validated = store.stage_record(result["attempt_id"], "validated")
    finally:
        conn.close()
    assert raw["status"] == "committed"
    assert validated["status"] == "committed"
    assert result["digest"] != GOLDEN_DIGEST


def test_run_log_records_each_interruption(tmp_path):
    home = tmp_path / "log" / "home"
    out = tmp_path / "log" / "runs"
    _kill(home, out, "before_costed_commit")
    before = read_status(home / PIPELINE_DB_NAME)
    running = [row for row in before["attempts"] if row["status"] == "running"]
    assert len(running) == 1
    killed_id = running[0]["attempt_id"]
    _resume(home, out)
    log_path = home / "logs" / "runs.jsonl"
    rows = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    detected = [row for row in rows if row["event"] == "run_interrupted_detected"]
    assert len(detected) == 1
    assert detected[0]["attempt_id"] == killed_id
    assert detected[0]["last_completed_stage"] == "validated"
    assert any(row["event"] == "run_completed" for row in rows)
