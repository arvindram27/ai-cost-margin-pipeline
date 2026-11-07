"""Kill drill harness: evidence helpers, fresh-root guard, and a reduced drill.

The full three-point drill is a separate, slower command run in Step 3. These
tests keep the harness itself honest without invoking it: helper correctness,
the fresh-root guard, the X clean golden equality path, and one bounded
point 1 (kill, resume, verify) pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ai_cost_margin.pipeline import SEVEN_FILES

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"


def _load_drill():
    import importlib.util

    spec = importlib.util.spec_from_file_location("kill_drill", TOOLS_DIR / "kill_drill.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mk_run_dir(root: Path, payloads: dict[str, bytes]) -> Path:
    run_dir = root / "somefingerprint"
    run_dir.mkdir(parents=True)
    for name, data in payloads.items():
        (run_dir / name).write_bytes(data)
    return run_dir


def test_drill_seven_file_hashes_cover_the_published_set(tmp_path):
    drill = _load_drill()
    payloads = {name: (name + "\n").encode("utf-8") for name in SEVEN_FILES}
    run_dir = _mk_run_dir(tmp_path / "a", payloads)
    hashes = drill.seven_file_hashes(run_dir)
    assert set(hashes) == set(SEVEN_FILES)
    assert len(set(hashes.values())) == len(SEVEN_FILES)


def test_drill_compare_seven_files_reports_equality_and_difference(tmp_path):
    drill = _load_drill()
    left_payloads = {name: (name + "\n").encode("utf-8") for name in SEVEN_FILES}
    right_payloads = dict(left_payloads)
    right_payloads["margin-report.json"] = b"{}\n"
    left = _mk_run_dir(tmp_path / "left", left_payloads)
    right = _mk_run_dir(tmp_path / "right", right_payloads)
    result = drill.compare_seven_files(left, right)
    assert result["margin-report.json"]["equal"] is False
    assert result["margin-digest.txt"]["equal"] is True
    for name in SEVEN_FILES:
        if name != "margin-report.json":
            assert result[name]["equal"] is True, name
            assert result[name]["left_sha256"] == result[name]["right_sha256"]


def test_drill_rejects_a_run_dir_with_the_wrong_file_set(tmp_path):
    drill = _load_drill()
    run_dir = _mk_run_dir(
        tmp_path / "bad",
        {"customer-margins.jsonl": b"x\n", "extra.txt": b"y\n"},
    )
    with pytest.raises(drill.DrillError) as excinfo:
        drill.seven_file_hashes(run_dir)
    assert "wrong file set" in str(excinfo.value)


def test_drill_never_overwrites_an_existing_root(tmp_path):
    drill = _load_drill()
    root = tmp_path / "drill-root"
    root.mkdir()
    with pytest.raises(drill.DrillError) as excinfo:
        drill.ensure_fresh_root(root)
    assert "already exists" in str(excinfo.value)


def test_drill_reduced_point1_pass(tmp_path):
    """One bounded kill/resume point, driven through the harness internals."""
    drill = _load_drill()
    root = drill.ensure_fresh_root(tmp_path / "drill")
    clean_base = root / "x-clean"
    x_clean = drill.run_pipeline(
        ROOT, clean_base / "home", clean_base / "runs", drill.CUTOFF_X
    )
    x_clean_dir = clean_base / "runs" / x_clean["run_dir_name"]
    assert x_clean["digest"] == drill.GOLDEN_DIGEST
    assert drill.seven_file_hashes(x_clean_dir)

    point = drill._record_point_common(
        repo=ROOT,
        base=root / "x-before_raw_commit",
        cutoff=drill.CUTOFF_X,
        kill_point="before_raw_commit",
        clean_dir=x_clean_dir,
        clean_digest=x_clean["digest"],
        need_exact_replay=False,
    )
    assert point["kill_exit"] == 137
    assert point["interrupted_last_completed_stage"] is None
    assert point["resumed_digest"] == drill.GOLDEN_DIGEST
    assert point["golden_equal"] is True
    assert point["files_all_equal"] is True


def test_drill_cli_is_subprocess_based_and_imports_no_package(tmp_path):
    """The harness drives the CLI in subprocesses; it never imports the package."""
    text = (TOOLS_DIR / "kill_drill.py").read_text(encoding="utf-8")
    assert "subprocess" in text
    assert "import ai_cost_margin" not in text
    assert "from ai_cost_margin" not in text
    assert "sys.executable" in text


def test_drill_cli_help_and_bad_root_behave(tmp_path):
    assert TOOLS_DIR.is_dir()
    completed = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "kill_drill.py"), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0
    assert "--root" in completed.stdout
    bad = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "kill_drill.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert bad.returncode != 0
