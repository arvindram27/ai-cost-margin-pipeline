"""Expected-negative controls: typed exit-2 refusals and R8 log isolation.

Every case asserts the typed error, the exit code, and that no partial publish
happened. The cases run against pytest ``tmp_path`` only.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from ai_cost_margin.pipeline import _kill, main, run_pipeline
from ai_cost_margin.runstore import (
    PIPELINE_DB_NAME,
    PipelineBusyError,
    RunLogError,
    RunStore,
    read_status,
)

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PRICES = ROOT / "fixtures" / "phase2" / "price-list-synthetic-v1.json"
PLANS = ROOT / "fixtures" / "phase3" / "customer-plans-simulated-v1.json"
CUTOFF = "2026-11-01T23:59:59Z"
GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"


def _argv(home: Path, out: Path, *, prices: Path = PRICES, plans: Path = PLANS, cutoff: str = CUTOFF, batches=None):
    return [
        "--home",
        str(home),
        "--batches",
        *(str(item) for item in (batches or [PHASE1])),
        "--prices",
        str(prices),
        "--plans",
        str(plans),
        "--cutoff",
        cutoff,
        "--out",
        str(out),
    ]


def _run(home: Path, out: Path, **overrides):
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
    return run_pipeline(**kwargs)


def test_busy_lock_is_exit_2_and_names_the_write_lock(tmp_path, capsys):
    import subprocess
    import sys
    import time

    home = tmp_path / "home"
    home.mkdir()
    db_path = home / PIPELINE_DB_NAME
    ready_marker = tmp_path / "holder-ready"
    release_marker = tmp_path / "holder-release"
    holder_script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "import duckdb\n"
        "conn = duckdb.connect(sys.argv[1])\n"
        "conn.execute('CREATE TABLE IF NOT EXISTS held (x INTEGER)')\n"
        "Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
        "while not Path(sys.argv[3]).exists():\n"
        "    time.sleep(0.05)\n"
        "conn.close()\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(db_path),
            str(ready_marker),
            str(release_marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready_marker.exists():
            if process.poll() is not None:
                raise AssertionError(
                    f"holder process exited early ({process.returncode}): "
                    f"{process.stderr.read()}"
                )
            if time.monotonic() > deadline:
                raise AssertionError("holder process never became ready")
            time.sleep(0.05)
        exit_code = main(_argv(home, tmp_path / "runs"))
        stderr = capsys.readouterr().err
        assert exit_code == 2
        assert "write lock" in stderr
        assert "Could not set lock" not in stderr
    finally:
        release_marker.write_text("release", encoding="utf-8")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_missing_required_arguments_are_exit_2(tmp_path, capsys):
    exit_code = main(["--home", str(tmp_path / "home")])
    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "required" in stderr
    assert not (tmp_path / "home").exists()


def test_invalid_cutoff_is_exit_2_before_any_write(tmp_path, capsys):
    home = tmp_path / "home"
    exit_code = main(_argv(home, tmp_path / "runs", cutoff="not-a-timestamp"))
    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "invalid cutoff" in stderr
    assert not home.exists()


def test_missing_batch_path_is_exit_2_before_any_write(tmp_path, capsys):
    home = tmp_path / "home"
    exit_code = main(
        _argv(home, tmp_path / "runs", batches=[tmp_path / "missing.json"])
    )
    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "batch path not found" in stderr
    assert not home.exists()


def test_corrupt_store_is_a_typed_open_error_not_busy(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / PIPELINE_DB_NAME).write_bytes(b"corrupt store bytes")
    exit_code = main(_argv(home, tmp_path / "runs"))
    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "cannot open store" in stderr
    assert "write lock" not in stderr


def test_replay_mismatch_is_exit_2_and_keeps_the_published_pointer(tmp_path, capsys):
    home = tmp_path / "home"
    out = tmp_path / "runs"
    first = _run(home, out)
    target = first["run_dir"] / "customer-margins.jsonl"
    target.write_bytes(target.read_bytes() + b"\n")
    exit_code = main(_argv(home, out))
    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "differs" in stderr
    payload = read_status(home / PIPELINE_DB_NAME)
    assert payload["published"]["attempt_id"] == first["attempt_id"]
    assert payload["published"]["digest"] == GOLDEN_DIGEST


def test_plan_currency_mismatch_is_rejected_before_writing(tmp_path, capsys):
    plans = json.loads(PLANS.read_text(encoding="utf-8"))
    plans["currency"] = "EUR"
    altered = tmp_path / "plans-eur.json"
    altered.write_text(json.dumps(plans), encoding="utf-8")
    home = tmp_path / "home"
    exit_code = main(_argv(home, tmp_path / "runs", plans=altered))
    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "currency" in stderr
    assert not home.exists()


def test_unknown_plan_customer_fails_the_attempt_without_publishing(tmp_path, capsys):
    plans = json.loads(PLANS.read_text(encoding="utf-8"))
    plans["entries"][0]["customer_id"] = "cust-unknown"
    altered = tmp_path / "plans-unknown.json"
    altered.write_text(json.dumps(plans), encoding="utf-8")
    home = tmp_path / "home"
    out = tmp_path / "runs"
    exit_code = main(_argv(home, out, plans=altered))
    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "plan" in stderr.lower()
    payload = read_status(home / PIPELINE_DB_NAME)
    assert payload["published"] is None
    statuses = [row["status"] for row in payload["attempts"]]
    assert statuses == ["failed"]
    if out.exists():
        assert not any(path.is_dir() for path in out.iterdir())


def test_log_io_failure_is_typed_and_never_rolls_back_the_publish(tmp_path, monkeypatch):
    home = tmp_path / "home"
    out = tmp_path / "runs"
    original = RunStore.append_log

    def failing(self, record):
        if record.get("event") == "run_completed":
            raise RunLogError("simulated run-log IO failure")
        return original(self, record)

    monkeypatch.setattr(RunStore, "append_log", failing)
    with pytest.raises(RunLogError):
        _run(home, out)
    payload = read_status(home / PIPELINE_DB_NAME)
    assert payload["published"] is not None
    assert payload["published"]["digest"] == GOLDEN_DIGEST
    published = [row for row in payload["attempts"] if row["status"] == "published"]
    assert len(published) == 1


def test_kill_hook_is_inert_without_the_environment_variable(monkeypatch):
    monkeypatch.delenv("AI_COST_MARGIN_KILL_POINT", raising=False)
    _kill("before_raw_commit")
    _kill("before_costed_commit")
    _kill("after_publish_rename")
    monkeypatch.setenv("AI_COST_MARGIN_KILL_POINT", "before_costed_commit")
    _kill("before_raw_commit")
    _kill("after_publish_rename")


def test_invalid_json_only_input_still_publishes_or_fails_typed(tmp_path, capsys):
    """A single invalid delivery must not publish a partial run or crash."""
    home = tmp_path / "home"
    out = tmp_path / "runs"
    exit_code = main(
        _argv(home, out, batches=[PHASE1 / "batch-007-invalid-json.json"])
    )
    assert exit_code == 0
    result = read_status(home / PIPELINE_DB_NAME)
    assert result["published"] is not None
    # The invalid JSON is dead-lettered; the margin tables are empty.
    assert result["published"]["digest"] != GOLDEN_DIGEST
