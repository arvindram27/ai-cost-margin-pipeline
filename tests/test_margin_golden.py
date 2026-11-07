"""Golden Phase 3 artifacts, replay safety, and production CLI provenance.

Ordinary pytest runs never regenerate goldens. The single generation test is
skipped unless AI_COST_MARGIN_GENERATE_PHASE3_GOLDENS=1 is set explicitly for
Step 1a; it uses the fixed fixture code-version override.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ai_cost_margin import margins, runs
from ai_cost_margin.ingest import ingest
from ai_cost_margin.margins import run_margins
from ai_cost_margin.runs import OutputError, ReplayMismatchError

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PHASE2 = ROOT / "fixtures" / "phase2"
PHASE3 = ROOT / "fixtures" / "phase3"
EXPECTED = PHASE3 / "expected"
PRICES = PHASE2 / "price-list-synthetic-v1.json"
PLANS = PHASE3 / "customer-plans-simulated-v1.json"
CUTOFF = "2026-11-01T23:59:59Z"
GOLDEN_VERSION = "fixtures-phase3-golden-v1"
EXPECTED_FILES = [
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "lineage.jsonl",
    "margin-report.json",
    "margin-digest.txt",
    "run-manifest.json",
]
DELIVERIES = [
    "batch-001-clean.json",
    "batch-002-retries.json",
    "batch-003-changed-payload.json",
    "batch-004-correction.json",
    "batch-005-malformed.json",
    "batch-006-unavailable-fields.json",
    "batch-007-invalid-json.json",
    "batch-001-clean.json",
]


def _arguments(tmp_path: Path) -> dict:
    home = tmp_path / "store"
    ingest([PHASE1 / name for name in DELIVERIES], home)
    return {
        "home": home,
        "prices": PRICES,
        "plans": PLANS,
        "cutoff": CUTOFF,
        "out_root": tmp_path / "runs",
        "code_version_override": GOLDEN_VERSION,
    }


def _run(tmp_path: Path) -> dict:
    return run_margins(**_arguments(tmp_path))


@pytest.mark.skipif(
    os.environ.get("AI_COST_MARGIN_GENERATE_PHASE3_GOLDENS") != "1",
    reason="golden generation is an explicit Step 1a invocation only",
)
def test_generate_phase3_goldens(tmp_path):
    result = _run(tmp_path)
    EXPECTED.mkdir(parents=True, exist_ok=True)
    for name in EXPECTED_FILES:
        (EXPECTED / name).write_bytes((result["run_dir"] / name).read_bytes())
    assert result["manifest"]["code_version"] == GOLDEN_VERSION
    assert sorted(path.name for path in EXPECTED.iterdir()) == sorted(EXPECTED_FILES)


@pytest.mark.parametrize("filename", EXPECTED_FILES)
def test_golden_output_matches_committed_expected(tmp_path, filename):
    result = _run(tmp_path)
    assert (result["run_dir"] / filename).read_bytes() == (EXPECTED / filename).read_bytes()


def test_two_independent_runs_are_byte_identical(tmp_path):
    first = _run(tmp_path / "a")
    second = _run(tmp_path / "b")
    assert first["digest"] == second["digest"]
    for name in EXPECTED_FILES:
        assert first["files"][name] == second["files"][name]


def test_exact_replay_verifies_without_overwriting(tmp_path):
    arguments = _arguments(tmp_path)
    first = run_margins(**arguments)
    before = {name: (first["run_dir"] / name).read_bytes() for name in EXPECTED_FILES}
    second = run_margins(**arguments)
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["run_dir"] == first["run_dir"]
    for name in EXPECTED_FILES:
        assert (second["run_dir"] / name).read_bytes() == before[name]


def test_a_changed_existing_run_is_a_loud_replay_mismatch(tmp_path):
    arguments = _arguments(tmp_path)
    first = run_margins(**arguments)
    target = first["run_dir"] / "customer-margins.jsonl"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ReplayMismatchError):
        run_margins(**arguments)


def test_mid_write_partial_output_is_detected_by_the_next_identical_run(tmp_path, monkeypatch):
    arguments = _arguments(tmp_path)
    original = runs._write_bytes
    calls = []

    def fail_on_second(path, data):
        calls.append(path.name)
        if len(calls) == 2:
            raise OSError("simulated mid-write failure")
        return original(path, data)

    monkeypatch.setattr(runs, "_write_bytes", fail_on_second)
    with pytest.raises(OutputError):
        run_margins(**arguments)
    assert len(calls) == 2

    monkeypatch.setattr(runs, "_write_bytes", original)
    with pytest.raises(ReplayMismatchError):
        run_margins(**arguments)


def test_a_late_correction_creates_a_new_run_and_preserves_the_prior_run(tmp_path):
    arguments = _arguments(tmp_path)
    first = run_margins(**arguments)
    prior_bytes = {name: (first["run_dir"] / name).read_bytes() for name in EXPECTED_FILES}

    ingest([PHASE3 / "late-correction-batch.json"], arguments["home"])
    later_arguments = dict(arguments)
    later_arguments["cutoff"] = "2026-11-20T00:00:00Z"
    second = run_margins(**later_arguments)

    assert second["run_fingerprint"] != first["run_fingerprint"]
    assert second["digest"] != first["digest"]
    for name in EXPECTED_FILES:
        assert (first["run_dir"] / name).read_bytes() == prior_bytes[name]
    lineage = [
        json.loads(line)
        for line in (second["run_dir"] / "lineage.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    correction = next(
        edge for edge in lineage if edge["superseding_event_id"] == "evt-p3-correction-001"
    )
    assert correction["superseded_event_id"] == "evt-001-007"
    assert correction["rule"] == "supersedes"
    assert correction["status"] == "resolved"


def test_golden_provenance_is_the_fixed_fixture_value_not_a_future_commit():
    manifest = json.loads((EXPECTED / "run-manifest.json").read_text(encoding="utf-8"))
    report = json.loads((EXPECTED / "margin-report.json").read_text(encoding="utf-8"))
    for document in (manifest, report):
        assert document["code_version"] == GOLDEN_VERSION
        assert document["code_version_source"] == "fixture"
        assert document["working_tree_clean"] is None
        assert document["dirty_status_sha256"] is None
    assert report["digest"] == (EXPECTED / "margin-digest.txt").read_text(encoding="utf-8").splitlines()[1]


def test_invalid_price_and_plan_inputs_exit_two_and_write_nothing(tmp_path, capsys):
    home = tmp_path / "store"
    ingest([PHASE1 / name for name in DELIVERIES], home)
    for prices, plans in [
        (PHASE2 / "invalid" / "price-list-gap.json", PLANS),
        (PRICES, PHASE3 / "invalid" / "plans-gap.json"),
    ]:
        out = tmp_path / f"out-{prices.stem}-{plans.stem}"
        code = margins.main([
            "--home", str(home), "--prices", str(prices), "--plans", str(plans),
            "--cutoff", CUTOFF, "--out", str(out),
        ])
        assert code == 2
        assert not out.exists()
    assert "invalid" in capsys.readouterr().err


def test_plan_currency_mismatch_is_fatal(tmp_path, capsys):
    arguments = _arguments(tmp_path)
    document = json.loads(PLANS.read_text(encoding="utf-8"))
    document["currency"] = "EUR"
    mismatch = tmp_path / "plans-eur.json"
    mismatch.write_text(json.dumps(document), encoding="utf-8")
    out = tmp_path / "currency-out"
    code = margins.main([
        "--home", str(arguments["home"]), "--prices", str(PRICES), "--plans", str(mismatch),
        "--cutoff", CUTOFF, "--out", str(out),
    ])
    assert code == 2
    assert "does not match" in capsys.readouterr().err
    assert not out.exists()


def test_unplanned_customer_is_fatal_and_writes_nothing(tmp_path, capsys):
    arguments = _arguments(tmp_path)
    document = json.loads(PLANS.read_text(encoding="utf-8"))
    document["entries"] = [entry for entry in document["entries"] if entry["customer_id"] != "cust-001"]
    incomplete = tmp_path / "plans-incomplete.json"
    incomplete.write_text(json.dumps(document), encoding="utf-8")
    out = tmp_path / "unplanned-out"
    code = margins.main([
        "--home", str(arguments["home"]), "--prices", str(PRICES), "--plans", str(incomplete),
        "--cutoff", CUTOFF, "--out", str(out),
    ])
    assert code == 2
    assert "no simulated plan covers" in capsys.readouterr().err
    assert not out.exists()


def test_production_cli_records_real_git_head_and_seven_files(tmp_path):
    arguments = _arguments(tmp_path)
    arguments.pop("code_version_override")
    code = margins.main([
        "--home", str(arguments["home"]), "--prices", str(PRICES), "--plans", str(PLANS),
        "--cutoff", CUTOFF, "--out", str(arguments["out_root"]),
    ])
    assert code == 0
    run_dirs = list(arguments["out_root"].iterdir())
    assert len(run_dirs) == 1
    assert sorted(path.name for path in run_dirs[0].iterdir()) == sorted(EXPECTED_FILES)
    manifest = json.loads((run_dirs[0] / "run-manifest.json").read_text(encoding="utf-8"))
    expected_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()
    assert manifest["code_version_source"] == "git_head"
    assert manifest["code_version"] == expected_head
    if manifest["working_tree_clean"]:
        assert manifest["dirty_status_sha256"] is None
    else:
        assert len(manifest["dirty_status_sha256"]) == 64
