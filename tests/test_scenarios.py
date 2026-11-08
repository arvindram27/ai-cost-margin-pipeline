"""Phase 5 what-if scenario engine: baseline parity, token rule, levers, purity.

Every test works on the committed export under ``app/export/``. Nothing here
writes to the repository, starts a server or reads ``var/``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export_data  # noqa: E402
import scenarios  # noqa: E402
from ai_cost_margin import costing, pricing  # noqa: E402

GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"
EXPORT_DIR = ROOT / "app" / "export"
BASELINE = scenarios.ScenarioLevers(volume_tenths=10)


@pytest.fixture(scope="module")
def bundle():
    return export_data.load_export(EXPORT_DIR)


def _dicts(records):
    return [record.as_dict() for record in records]


def _cust001(result):
    row = next(r for r in result.customer_rows if r["customer_id"] == "cust-001")
    return {
        key: row[key]
        for key in (
            "estimated_cost",
            "simulated_revenue",
            "simulated_margin",
            "margin_status",
        )
    }


def _export_file_hashes():
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(EXPORT_DIR.iterdir())
        if path.is_file()
    }


def _bundle_snapshot(bundle):
    return {
        "customer_margins": _dicts(bundle.customer_margins),
        "workload_margins": _dicts(bundle.workload_margins),
        "unallocated": _dicts(bundle.unallocated),
        "scenario_events": _dicts(bundle.scenario_events),
        "lineage": _dicts(bundle.lineage),
        "price_list": bundle.price_list.as_dict(),
        "plans": bundle.plans.as_dict(),
        "manifest": bundle.manifest.as_dict(),
        "financial_digest": bundle.financial_digest,
        "integrity_digest": bundle.integrity_digest,
    }


def test_baseline_levers_reproduce_exported_tables_and_digest(bundle):
    result = scenarios.compute_scenario(bundle, BASELINE)
    assert _dicts(result.customer_rows) == _dicts(bundle.customer_margins)
    assert _dicts(result.workload_rows) == _dicts(bundle.workload_margins)
    assert _dicts(result.unallocated_rows) == _dicts(bundle.unallocated)
    assert result.digest == GOLDEN_DIGEST
    assert result.digest == bundle.financial_digest


def test_volume_scaling_uses_the_integer_token_rule(bundle):
    events = scenarios.build_engine_events(
        bundle, scenarios.ScenarioLevers(volume_tenths=13)
    )
    by_key = {event["provider_event_id"]: event for event in events}
    # (4825 * 13 + 5) // 10 == 6273; an unavailable count stays None.
    assert by_key["evt-0001"]["tokens_input"] == 6273
    assert by_key["evt-0005"]["tokens_cached"] is None

    baseline = scenarios.compute_scenario(bundle, BASELINE)
    assert _cust001(baseline) == {
        "estimated_cost": "0.026507850000",
        "simulated_revenue": "49.000000000000",
        "simulated_margin": "48.973492150000",
        "margin_status": "complete",
    }
    at_130 = scenarios.compute_scenario(
        bundle, scenarios.ScenarioLevers(volume_tenths=13)
    )
    # Per-event rounding: this is NOT 1.3 times the baseline (0.034460205...).
    assert _cust001(at_130) == {
        "estimated_cost": "0.034461150000",
        "simulated_revenue": "49.000000000000",
        "simulated_margin": "48.965538850000",
        "margin_status": "complete",
    }
    at_200 = scenarios.compute_scenario(
        bundle, scenarios.ScenarioLevers(volume_tenths=20)
    )
    assert _cust001(at_200) == {
        "estimated_cost": "0.053015700000",
        "simulated_revenue": "49.000000000000",
        "simulated_margin": "48.946984300000",
        "margin_status": "complete",
    }
    assert at_130.digest != baseline.digest
    assert at_200.digest not in (baseline.digest, at_130.digest)


def test_flat_rate_override_and_malformed_rate(bundle):
    flat = scenarios.ScenarioLevers(
        volume_tenths=10,
        flat_rates=(("synth-small", "1.000000", "1.000000", "1.000000"),),
    )
    result = scenarios.compute_scenario(bundle, flat)
    assert _cust001(result) == {
        "estimated_cost": "0.025551000000",
        "simulated_revenue": "49.000000000000",
        "simulated_margin": "48.974449000000",
        "margin_status": "complete",
    }
    assert result.digest != GOLDEN_DIGEST

    malformed = scenarios.ScenarioLevers(
        volume_tenths=10,
        flat_rates=(("synth-small", "abc", "1.000000", "1.000000"),),
    )
    with pytest.raises(pricing.PriceScheduleError):
        scenarios.compute_scenario(bundle, malformed)


def test_model_substitution_priced_and_unpriced(bundle):
    swapped = scenarios.ScenarioLevers(
        volume_tenths=10, substitutions=(("synth-small", "synth-large"),)
    )
    result = scenarios.compute_scenario(bundle, swapped)
    assert _cust001(result) == {
        "estimated_cost": "0.035135700000",
        "simulated_revenue": "49.000000000000",
        "simulated_margin": "48.964864300000",
        "margin_status": "complete",
    }
    assert result.digest != GOLDEN_DIGEST

    # Substituting an unlisted model must give unpriced events, never zero cost.
    base_events = scenarios.build_engine_events(bundle, BASELINE)
    base_rows, _ = costing.cost_events(
        base_events,
        pricing.parse_price_list(scenarios.build_price_list(bundle, BASELINE)),
    )
    expected_unpriced = sum(
        1
        for row in base_rows
        if row["model"] == "synth-small"
        and row["cost_status"] != costing.STATUS_SUPERSEDED
    )
    assert expected_unpriced > 0

    unlisted = scenarios.ScenarioLevers(
        volume_tenths=10, substitutions=(("synth-small", "synth-unlisted"),)
    )
    events = scenarios.build_engine_events(bundle, unlisted)
    price_list = pricing.parse_price_list(scenarios.build_price_list(bundle, unlisted))
    rows, report = costing.cost_events(events, price_list)
    substituted = [row for row in rows if row["model"] == "synth-unlisted"]
    live = [
        row for row in substituted if row["cost_status"] != costing.STATUS_SUPERSEDED
    ]
    assert len(live) == expected_unpriced
    for row in live:
        assert row["cost_status"] == costing.STATUS_UNPRICED
        assert row["unpriced_reason"] == "model_not_in_price_list"
        assert row["total_cost"] is None
    assert report["unpriced_by_reason"]["model_not_in_price_list"] == expected_unpriced

    result = scenarios.compute_scenario(bundle, unlisted)
    assert result.report["unpriced_by_reason"]["model_not_in_price_list"] == (
        expected_unpriced
    )
    assert _cust001(result)["margin_status"] == "incomplete"
    assert result.digest != GOLDEN_DIGEST


def test_scenario_purity_and_determinism(bundle):
    files_before = _export_file_hashes()
    assert len(files_before) == 8
    snapshot_before = _bundle_snapshot(bundle)

    levered = scenarios.ScenarioLevers(
        volume_tenths=20,
        flat_rates=(("synth-large", "2.000000", "2.000000", "2.000000"),),
        substitutions=(("synth-small", "synth-large"),),
    )
    first = scenarios.compute_scenario(bundle, levered)
    second = scenarios.compute_scenario(bundle, levered)
    scenarios.compute_scenario(bundle, BASELINE)

    assert first.digest == second.digest
    assert _dicts(first.customer_rows) == _dicts(second.customer_rows)
    assert _dicts(first.workload_rows) == _dicts(second.workload_rows)
    assert _dicts(first.unallocated_rows) == _dicts(second.unallocated_rows)
    assert _export_file_hashes() == files_before
    assert _bundle_snapshot(bundle) == snapshot_before
