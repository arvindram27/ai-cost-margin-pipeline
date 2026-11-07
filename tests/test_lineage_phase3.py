"""As-of cutoff, corrections lineage, and deterministic conflict rules."""

from __future__ import annotations

import pytest

from ai_cost_margin.costing import LineageAmbiguityError
from ai_cost_margin.lineage import build_lineage

CUTOFF = "2026-11-01T23:59:59Z"


def _event(
    event_id="evt-1",
    *,
    version=1,
    supersedes=None,
    arrival="2026-11-01T00:00:00Z",
    usage="2026-10-05T00:00:00Z",
    request=None,
):
    return {
        "provider": "p",
        "account_id": "acct-demo",
        "provider_event_id": event_id,
        "record_version": version,
        "supersedes": supersedes,
        "arrival_time": arrival,
        "usage_time": usage,
        "request_id": request or f"req-{event_id}",
    }


def test_cutoff_is_inclusive_and_excludes_later_arrivals():
    events = [_event("evt-early"), _event("evt-late", arrival="2026-11-15T00:00:00Z")]
    result = build_lineage(events, CUTOFF)
    assert [event["provider_event_id"] for event in result.events] == ["evt-early"]
    assert result.excluded_by_cutoff == 1


def test_a_late_non_correction_needs_no_supersedes_and_appears_later():
    late = _event("evt-late", arrival="2026-11-15T00:00:00Z")
    assert build_lineage([late], CUTOFF).events == []
    result = build_lineage([late], "2026-11-20T00:00:00Z")
    assert len(result.events) == 1
    assert result.edges == []


def test_version_bump_edge_is_superseded_to_superseding():
    result = build_lineage([_event(version=1), _event(version=2)], CUTOFF)
    assert result.superseded_indexes == frozenset({0})
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge["rule"] == "record_version"
    assert edge["superseded_record_version"] == 1
    assert edge["superseding_record_version"] == 2
    assert edge["status"] == "resolved"


def test_explicit_correction_edge_uses_the_same_direction():
    result = build_lineage(
        [_event("evt-old"), _event("evt-new", supersedes="evt-old")],
        CUTOFF,
    )
    edge = result.edges[0]
    assert edge["superseded_event_id"] == "evt-old"
    assert edge["superseding_event_id"] == "evt-new"
    assert edge["rule"] == "supersedes"
    assert result.superseded_indexes == frozenset({0})


def test_dangling_correction_is_visible_and_does_not_suppress_the_claimant():
    result = build_lineage([_event("evt-new", supersedes="evt-missing")], CUTOFF)
    assert result.superseded_indexes == frozenset()
    assert result.dangling_supersedes == 1
    assert result.edges[0]["status"] == "dangling"
    assert result.edges[0]["superseded_event_id"] == "evt-missing"
    assert result.edges[0]["superseded_record_version"] is None


def test_two_claimants_for_one_target_are_fatal():
    events = [
        _event("evt-target"),
        _event("evt-a", supersedes="evt-target"),
        _event("evt-b", supersedes="evt-target"),
    ]
    with pytest.raises(LineageAmbiguityError):
        build_lineage(events, CUTOFF)


def test_a_supersedes_cycle_is_fatal():
    events = [
        _event("evt-a", supersedes="evt-b"),
        _event("evt-b", supersedes="evt-a"),
    ]
    with pytest.raises(LineageAmbiguityError):
        build_lineage(events, CUTOFF)


def test_self_reference_has_no_superseding_effect():
    result = build_lineage([_event("evt-a", supersedes="evt-a")], CUTOFF)
    assert result.superseded_indexes == frozenset()
    assert result.edges == []


def test_billable_retries_are_both_current():
    events = [
        _event("evt-attempt-1", request="req-retry"),
        _event("evt-attempt-2", request="req-retry"),
    ]
    result = build_lineage(events, CUTOFF)
    assert result.superseded_indexes == frozenset()
    assert len(result.events) == 2


def test_duplicate_current_record_version_is_fatal():
    events = [_event("evt-a", version=1), _event("evt-a", version=1)]
    with pytest.raises(LineageAmbiguityError, match="duplicate current record version"):
        build_lineage(events, CUTOFF)


def test_reordering_does_not_change_the_lineage_edges():
    events = [
        _event("evt-old"),
        _event("evt-new", supersedes="evt-old"),
        _event("evt-version", version=1),
        _event("evt-version", version=2),
    ]
    forward = build_lineage(events, CUTOFF)
    reverse = build_lineage(list(reversed(events)), CUTOFF)
    assert forward.edges == reverse.edges


def test_usage_time_and_arrival_time_remain_separate():
    event = _event("evt-late", arrival="2026-11-15T00:00:00Z", usage="2026-09-05T00:00:00Z")
    result = build_lineage([event], "2026-11-20T00:00:00Z")
    assert result.events[0]["usage_time"] == "2026-09-05T00:00:00Z"
    assert result.events[0]["arrival_time"] == "2026-11-15T00:00:00Z"
