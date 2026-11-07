"""Phase 5 export generator: allowlist, sanitization, digests, determinism.

Every test builds its own published Phase 4 run under ``tmp_path`` from the
committed fixtures; nothing here touches the repo's ``var/`` store.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from ai_cost_margin.digest import compute_digest
from ai_cost_margin.export import main as export_main
from ai_cost_margin.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PRICES = ROOT / "fixtures" / "phase2" / "price-list-synthetic-v1.json"
PLANS = ROOT / "fixtures" / "phase3" / "customer-plans-simulated-v1.json"
CUTOFF = "2026-11-01T23:59:59Z"
GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"
EXPORT_FILES = {
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "scenario-events.jsonl",
    "lineage.jsonl",
    "price-list.json",
    "plans.json",
    "export-manifest.json",
}
BANNED = (b"provider_event_id", b"account_id", b"request_id", b"attempt_id", b"payload_sha256")
CUSTOMER_FIELDS = {
    "cost_label",
    "currency",
    "customer_id",
    "estimated_cost",
    "events_costed",
    "events_superseded",
    "events_unpriced",
    "margin_label",
    "margin_status",
    "period",
    "plan_id",
    "plans_version_id",
    "revenue_label",
    "simulated_margin",
    "simulated_revenue",
    "unpriced_inconsistent_token_counts",
    "unpriced_model_not_in_price_list",
    "unpriced_no_applicable_rate",
    "unpriced_token_count_unavailable",
    "usage_label",
}


def _publish(base: Path):
    home = base / "home"
    out = base / "runs"
    result = run_pipeline(
        home=home,
        batches=[PHASE1],
        prices=PRICES,
        plans=PLANS,
        cutoff=CUTOFF,
        out_root=out,
        repo_root=ROOT,
    )
    return result, home, out


def _export(home: Path, out_root: Path, export_dir: Path) -> int:
    return export_main(
        [
            "--home",
            str(home),
            "--runs",
            str(out_root),
            "--prices",
            str(PRICES),
            "--plans",
            str(PLANS),
            "--out",
            str(export_dir),
        ]
    )


def _rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    base = tmp_path_factory.mktemp("export-source")
    result, home, out = _publish(base)
    export_dir = base / "export"
    assert _export(home, out, export_dir) == 0
    return {"result": result, "home": home, "runs": out, "export_dir": export_dir}


def test_export_allowlist_field_guard_and_banned_keys(exported):
    export_dir = exported["export_dir"]
    assert {path.name for path in export_dir.iterdir()} == EXPORT_FILES
    for path in export_dir.iterdir():
        data = path.read_bytes()
        for banned in BANNED:
            assert banned not in data, (path.name, banned)

    expected_fields = {
        "customer-margins.jsonl": CUSTOMER_FIELDS,
        "workload-margins.jsonl": CUSTOMER_FIELDS
        | {"revenue_allocation_basis", "revenue_allocation_status", "workload_type"},
        "unallocated.jsonl": {
            "bucket",
            "cost_label",
            "currency",
            "customer_id",
            "estimated_cost",
            "events_costed",
            "events_superseded",
            "events_unpriced",
            "period",
            "unpriced_inconsistent_token_counts",
            "unpriced_model_not_in_price_list",
            "unpriced_no_applicable_rate",
            "unpriced_token_count_unavailable",
            "usage_label",
            "workload_type",
        },
    }
    for name, fields in expected_fields.items():
        for row in _rows(export_dir / name):
            assert set(row) == fields, name
            for value in row.values():
                assert value is None or type(value) in (str, int)
            customer_id = row["customer_id"]
            assert customer_id is None or re.match(r"^cust-\d{3}$", customer_id)


def test_tampered_source_digest_exits_2_and_writes_nothing(tmp_path, capsys):
    result, home, out = _publish(tmp_path / "tampered")
    target = result["run_dir"] / "customer-margins.jsonl"
    target.write_bytes(target.read_bytes() + b"x")
    export_dir = tmp_path / "tampered" / "export"
    assert _export(home, out, export_dir) == 2
    assert "error:" in capsys.readouterr().err
    assert not export_dir.exists()


def test_double_generation_byte_identical(exported, tmp_path):
    second = tmp_path / "export-again"
    assert _export(exported["home"], exported["runs"], second) == 0
    first_dir = exported["export_dir"]
    assert {path.name for path in second.iterdir()} == EXPORT_FILES
    for name in EXPORT_FILES:
        assert (second / name).read_bytes() == (first_dir / name).read_bytes(), name


def test_integrity_digest_recomputation(exported):
    export_dir = exported["export_dir"]
    manifest = json.loads((export_dir / "export-manifest.json").read_text(encoding="utf-8"))
    chunks = ["ai-cost-margin/export-integrity/v1\n"]
    for name in sorted(manifest["files"]):
        data = (export_dir / name).read_bytes()
        entry = manifest["files"][name]
        assert entry["sha256"] == hashlib.sha256(data).hexdigest(), name
        chunks.append(f"file {name} {entry['rows']} {entry['sha256']}\n")
    recomputed = hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()
    assert recomputed == manifest["export_integrity_digest"]


def test_sanitized_lineage_fields_and_surrogates(exported):
    rows = _rows(exported["export_dir"] / "lineage.jsonl")
    assert rows
    for row in rows:
        assert set(row) == {
            "superseded_event_key",
            "superseding_event_key",
            "superseded_record_version",
            "superseding_record_version",
            "rule",
            "status",
            "usage_time",
            "arrival_time",
        }
        assert re.match(r"^evt-\d{4}$", row["superseded_event_key"])
        assert re.match(r"^evt-\d{4}$", row["superseding_event_key"])


def test_manifest_attestation_equals_source_margin_digest(exported):
    export_dir = exported["export_dir"]
    manifest = json.loads((export_dir / "export-manifest.json").read_text(encoding="utf-8"))
    digest_lines = (
        (exported["result"]["run_dir"] / "margin-digest.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert manifest["source"]["digest"] == digest_lines[1] == GOLDEN_DIGEST
    assert manifest["source"]["digest_definition"] == "ai-cost-margin/margin-digest/v1"
    assert manifest["source"]["run_fingerprint"] == exported["result"]["run_fingerprint"]


def test_genuine_financial_digest_recomputation_from_export(exported):
    export_dir = exported["export_dir"]
    manifest = json.loads((export_dir / "export-manifest.json").read_text(encoding="utf-8"))
    recomputed = compute_digest(
        _rows(export_dir / "customer-margins.jsonl"),
        _rows(export_dir / "workload-margins.jsonl"),
        _rows(export_dir / "unallocated.jsonl"),
    )
    assert recomputed == manifest["source"]["digest"] == GOLDEN_DIGEST


def test_scenario_events_fields_and_supersedes_mapping(exported):
    rows = _rows(exported["export_dir"] / "scenario-events.jsonl")
    assert len(rows) == 19
    keys = [row["event_key"] for row in rows]
    # Identity-keyed surrogates: 18 distinct event identities among 19 rows
    # (one event exists at record versions 1 and 2), contiguous from evt-0001
    # and assigned at first occurrence in stage order.
    distinct = list(dict.fromkeys(keys))
    assert distinct == [f"evt-{index:04d}" for index in range(1, 19)]
    counts = {key: keys.count(key) for key in distinct}
    assert sorted(counts.values()) == [1] * 17 + [2]
    doubled = next(key for key, count in counts.items() if count == 2)
    positions = [index for index, key in enumerate(keys) if key == doubled]
    assert [rows[index]["record_version"] for index in positions] == [1, 2]
    for row in rows:
        assert set(row) == {
            "event_key",
            "customer_id",
            "model",
            "feature",
            "workload_type",
            "usage_time",
            "tokens_input",
            "tokens_output",
            "tokens_cached",
            "unavailable_fields",
            "record_version",
            "supersedes_event_key",
        }
        assert row["supersedes_event_key"] is None or row["supersedes_event_key"] in keys
    assert sum(1 for row in rows if row["supersedes_event_key"] is not None) == 1

    # Both golden lineage edges survive sanitization with keys, record
    # versions and target links intact.
    lineage = _rows(exported["export_dir"] / "lineage.jsonl")
    version_edge = next(row for row in lineage if row["rule"] == "record_version")
    supersedes_edge = next(row for row in lineage if row["rule"] == "supersedes")
    assert (
        version_edge["superseded_event_key"]
        == version_edge["superseding_event_key"]
        == doubled
    )
    assert (
        version_edge["superseded_record_version"],
        version_edge["superseding_record_version"],
    ) == (1, 2)
    claim_row = next(row for row in rows if row["supersedes_event_key"] is not None)
    assert supersedes_edge["superseding_event_key"] == claim_row["event_key"]
    assert supersedes_edge["superseded_event_key"] == claim_row["supersedes_event_key"]
    assert (
        supersedes_edge["superseded_record_version"],
        supersedes_edge["superseding_record_version"],
    ) == (1, 1)
