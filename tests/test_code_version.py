"""Git HEAD provenance, content-honest dirty markers, and golden override."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ai_cost_margin.margins import build_parser
from ai_cost_margin.runs import (
    CodeVersion,
    compute_run_fingerprint,
    dirty_status_sha256,
    fixture_code_version,
    probe_git,
    resolve_code_version,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40


def test_fixture_code_version_never_claims_git_provenance():
    version = fixture_code_version("fixtures-phase3-golden-v1")
    assert version.as_dict() == {
        "code_version": "fixtures-phase3-golden-v1",
        "code_version_source": "fixture",
        "working_tree_clean": None,
        "dirty_status_sha256": None,
    }


def test_clean_probe_records_head_and_a_clean_tree(tmp_path):
    version = resolve_code_version(tmp_path, probe=lambda root: (HEAD, []))
    assert version.code_version == HEAD
    assert version.code_version_source == "git_head"
    assert version.working_tree_clean is True
    assert version.dirty_status_sha256 is None


def test_dirty_probe_records_head_and_content_honest_marker(tmp_path):
    (tmp_path / "file.txt").write_text("new content", encoding="utf-8")
    version = resolve_code_version(
        tmp_path,
        probe=lambda root: (HEAD, [" M file.txt"]),
    )
    assert version.code_version == HEAD
    assert version.working_tree_clean is False
    assert version.dirty_status_sha256 is not None


def test_same_dirty_path_and_status_with_different_content_changes_hash_and_fingerprint(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("first", encoding="utf-8")
    first_hash = dirty_status_sha256([" M file.txt"], tmp_path)
    first_version = CodeVersion(HEAD, "git_head", False, first_hash)
    first_fingerprint = compute_run_fingerprint(
        included_events=[], cutoff="2026-11-01T00:00:00Z",
        price_list_sha256="prices", plans_sha256="plans", code_version=first_version,
    )

    path.write_text("second", encoding="utf-8")
    second_hash = dirty_status_sha256([" M file.txt"], tmp_path)
    second_version = CodeVersion(HEAD, "git_head", False, second_hash)
    second_fingerprint = compute_run_fingerprint(
        included_events=[], cutoff="2026-11-01T00:00:00Z",
        price_list_sha256="prices", plans_sha256="plans", code_version=second_version,
    )
    assert first_hash != second_hash
    assert first_fingerprint != second_fingerprint


def test_untracked_files_are_hashed_individually_by_content(tmp_path):
    path = tmp_path / "new.txt"
    path.write_text("one", encoding="utf-8")
    first = dirty_status_sha256(["?? new.txt"], tmp_path)
    path.write_text("two", encoding="utf-8")
    second = dirty_status_sha256(["?? new.txt"], tmp_path)
    assert first != second


def test_deleted_files_use_a_deterministic_fixed_marker(tmp_path):
    first = dirty_status_sha256([" D gone.txt"], tmp_path)
    second = dirty_status_sha256([" D gone.txt"], tmp_path)
    assert first == second


def test_rename_entries_contribute_both_paths_in_porcelain_order(tmp_path):
    (tmp_path / "new.txt").write_text("content", encoding="utf-8")
    combined = dirty_status_sha256(["R  old.txt -> new.txt"], tmp_path)
    separate = dirty_status_sha256(["R  old.txt", "R  new.txt"], tmp_path)
    assert combined == separate


def test_fingerprint_changes_when_code_version_changes():
    first = compute_run_fingerprint(
        included_events=[], cutoff="2026-11-01T00:00:00Z",
        price_list_sha256="prices", plans_sha256="plans",
        code_version=fixture_code_version("version-one"),
    )
    second = compute_run_fingerprint(
        included_events=[], cutoff="2026-11-01T00:00:00Z",
        price_list_sha256="prices", plans_sha256="plans",
        code_version=fixture_code_version("version-two"),
    )
    assert first != second


def test_live_git_probe_matches_the_real_head_without_a_hardcoded_future_hash():
    head, entries = probe_git(ROOT)
    expected = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()
    assert head == expected
    version = resolve_code_version(ROOT)
    assert version.code_version == expected
    assert version.code_version_source == "git_head"
    if entries:
        assert version.working_tree_clean is False
        assert version.dirty_status_sha256 is not None
    else:
        assert version.working_tree_clean is True
        assert version.dirty_status_sha256 is None


def test_production_cli_has_no_code_version_override_flag():
    options = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
    }
    assert "--code-version" not in options
    assert "--code-version-override" not in options
