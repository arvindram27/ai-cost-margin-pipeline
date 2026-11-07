"""Delivery-order permutations: one fingerprint, one digest, order-invariant files.

Permutations only reorder the conflict-free subset. Batches 003 and 004 depend on
state produced by batch 001, so batch 001 always precedes them and the duplicate
batch-001 delivery stays last.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_cost_margin.pipeline import SEVEN_FILES, run_pipeline
from ai_cost_margin.runstore import read_status

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PHASE3 = ROOT / "fixtures" / "phase3"
PRICES = ROOT / "fixtures" / "phase2" / "price-list-synthetic-v1.json"
PLANS = ROOT / "fixtures" / "phase3" / "customer-plans-simulated-v1.json"
CUTOFF = "2026-11-01T23:59:59Z"
GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"
FIVE_FILES = (
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "lineage.jsonl",
    "margin-digest.txt",
)


def _batch(name: str) -> Path:
    return PHASE1 / name


B1 = _batch("batch-001-clean.json")
B2 = _batch("batch-002-retries.json")
B3 = _batch("batch-003-changed-payload.json")
B4 = _batch("batch-004-correction.json")
B5 = _batch("batch-005-malformed.json")
B6 = _batch("batch-006-unavailable-fields.json")
B7 = _batch("batch-007-invalid-json.json")

PERMUTATIONS = {
    "base": [B1, B2, B3, B4, B5, B6, B7, B1],
    "p1": [B1, B5, B6, B2, B3, B4, B7, B1],
    "p2": [B1, B7, B2, B3, B4, B6, B5, B1],
}


def _run(tmp_path: Path, name: str, deliveries, cutoff: str = CUTOFF):
    home = tmp_path / name / "home"
    out = tmp_path / name / "runs"
    result = run_pipeline(
        home=home,
        batches=deliveries,
        prices=PRICES,
        plans=PLANS,
        cutoff=cutoff,
        out_root=out,
        repo_root=ROOT,
    )
    return result, home, out


def _report(result) -> dict:
    return json.loads(
        (result["run_dir"] / "margin-report.json").read_text(encoding="utf-8")
    )


def _manifest(result) -> dict:
    return json.loads(
        (result["run_dir"] / "run-manifest.json").read_text(encoding="utf-8")
    )


def _invocation(home: Path) -> str:
    payload = read_status(home / "pipeline.duckdb")
    assert len(payload["attempts"]) == 1
    return payload["attempts"][0]["invocation_sha256"]


def _invariant_fields(result) -> dict:
    report = _report(result)
    manifest = _manifest(result)
    inputs = manifest["inputs"]
    return {
        "run_fingerprint": report["run_fingerprint"],
        "manifest_run_fingerprint": manifest["run_fingerprint"],
        "store_events_sha256": inputs["store_events_sha256"],
        "events_in_store": inputs["events_in_store"],
        "events_included": inputs["events_included"],
        "events_excluded_by_cutoff": inputs["events_excluded_by_cutoff"],
        "periods": report["periods"],
        "price_versions_used": report["price_versions_used"],
    }


def test_permutations_share_one_run_fingerprint_and_digest(tmp_path):
    results = {}
    for name, deliveries in PERMUTATIONS.items():
        result, home, out = _run(tmp_path, name, deliveries)
        results[name] = result
        assert result["digest"] == GOLDEN_DIGEST
    fingerprints = {name: result["run_fingerprint"] for name, result in results.items()}
    assert len(set(fingerprints.values())) == 1
    assert len({result["digest"] for result in results.values()}) == 1


def test_permutations_produce_byte_identical_five_files(tmp_path):
    base, base_home, base_out = _run(tmp_path, "base", PERMUTATIONS["base"])
    for name in ("p1", "p2"):
        other, other_home, other_out = _run(tmp_path, name, PERMUTATIONS[name])
        for file_name in FIVE_FILES:
            assert (other["run_dir"] / file_name).read_bytes() == (
                base["run_dir"] / file_name
            ).read_bytes(), (name, file_name)


def test_permutations_have_order_sensitive_invocation_hashes(tmp_path):
    invocations = {}
    for name, deliveries in PERMUTATIONS.items():
        result, home, out = _run(tmp_path, name, deliveries)
        invocations[name] = _invocation(home)
    assert len(set(invocations.values())) == len(PERMUTATIONS)


def test_report_and_manifest_fields_are_order_invariant(tmp_path):
    base, base_home, base_out = _run(tmp_path, "base", PERMUTATIONS["base"])
    base_fields = _invariant_fields(base)
    assert base_fields["store_events_sha256"]
    assert base_fields["events_in_store"] == 19
    assert base_fields["events_included"] == 19
    assert base_fields["events_excluded_by_cutoff"] == 0
    for name in ("p1", "p2"):
        other, other_home, other_out = _run(tmp_path, name, PERMUTATIONS[name])
        assert _invariant_fields(other) == base_fields, name


def test_report_and_manifest_are_byte_identical_across_permutations(tmp_path):
    """R3: every named field is order-invariant, so full byte equality holds."""
    base, base_home, base_out = _run(tmp_path, "base", PERMUTATIONS["base"])
    for name in ("p1", "p2"):
        other, other_home, other_out = _run(tmp_path, name, PERMUTATIONS[name])
        assert (other["run_dir"] / "margin-report.json").read_bytes() == (
            base["run_dir"] / "margin-report.json"
        ).read_bytes(), name
        assert (other["run_dir"] / "run-manifest.json").read_bytes() == (
            base["run_dir"] / "run-manifest.json"
        ).read_bytes(), name


def test_same_root_replay_after_invariance_passes(tmp_path):
    first, home, out = _run(tmp_path, "replay", PERMUTATIONS["base"])
    second, _, _ = _run(tmp_path, "replay", PERMUTATIONS["base"])
    assert second["replayed"] is True
    assert second["digest"] == first["digest"]
    assert second["run_dir"] == first["run_dir"]


def test_delivering_the_same_batches_twice_does_not_change_the_digest(tmp_path):
    once = [B1, B2, B3, B4, B5, B6, B7]
    twice = once + once
    first, first_home, first_out = _run(tmp_path, "once", once)
    second, second_home, second_out = _run(tmp_path, "twice", twice)
    assert first["digest"] == second["digest"] == GOLDEN_DIGEST
    for file_name in FIVE_FILES:
        assert (second["run_dir"] / file_name).read_bytes() == (
            first["run_dir"] / file_name
        ).read_bytes(), file_name


def test_cutoff_is_as_of_arrival_time(tmp_path):
    deliveries = [B1, B2, B3, B4, B5, B6, B7, PHASE3 / "late-arrival-batch.json"]
    excluded, excluded_home, excluded_out = _run(
        tmp_path, "excluded", deliveries, CUTOFF
    )
    included, included_home, included_out = _run(
        tmp_path, "included", deliveries, "2026-12-01T00:00:00Z"
    )
    assert excluded["digest"] == GOLDEN_DIGEST
    assert _report(excluded)["events_excluded_by_cutoff"] == 1
    assert _report(excluded)["events_in_store"] == 20
    assert _report(included)["events_excluded_by_cutoff"] == 0
    assert included["digest"] != excluded["digest"]
    assert (included["run_dir"] / "customer-margins.jsonl").read_bytes() != (
        excluded["run_dir"] / "customer-margins.jsonl"
    ).read_bytes()
