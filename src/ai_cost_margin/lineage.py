"""As-of cutoff selection and corrections lineage for Phase 3.

Stored events are never edited. A run includes exactly the records whose
``arrival_time`` is at or before its stated cutoff; ``usage_time`` remains the
period and price-selection field.

Supersession reuses Phase 2's two rules through ``costing.resolve_superseded``.
Every lineage edge is directed **superseded -> superseding**: the
``superseded_*`` columns name the replaced record and the ``superseding_*``
columns name its replacement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .costing import LineageAmbiguityError, resolve_superseded
from .pricing import parse_utc

__all__ = ["LineageResult", "build_lineage"]


@dataclass(frozen=True)
class LineageResult:
    """The included records, their superseded indexes, and visible edges."""

    events: list[dict[str, Any]]
    superseded_indexes: frozenset[int]
    edges: list[dict[str, Any]]
    excluded_by_cutoff: int
    dangling_supersedes: int


def _key(event: dict[str, Any]) -> tuple[str, str, str]:
    return (event["provider"], event["account_id"], event["provider_event_id"])


def _edge(
    *,
    superseding: dict[str, Any],
    superseded: dict[str, Any] | None,
    target_id: str,
    rule: str,
    status: str,
) -> dict[str, Any]:
    return {
        "provider": superseding["provider"],
        "account_id": superseding["account_id"],
        "rule": rule,
        "status": status,
        "superseded_event_id": target_id if superseded is None else superseded["provider_event_id"],
        "superseded_record_version": None if superseded is None else superseded["record_version"],
        "superseding_event_id": superseding["provider_event_id"],
        "superseding_record_version": superseding["record_version"],
        "usage_time": None if superseded is None else superseded["usage_time"],
        "arrival_time": superseding["arrival_time"],
    }


def build_lineage(events: Sequence[dict[str, Any]], cutoff: str) -> LineageResult:
    """Filter by arrival cutoff and resolve lineage on the included set only."""
    cutoff_moment = parse_utc(cutoff)
    included: list[dict[str, Any]] = []
    excluded = 0
    for event in events:
        if parse_utc(event["arrival_time"]) <= cutoff_moment:
            included.append(event)
        else:
            excluded += 1

    positions: dict[tuple[str, str, str], list[int]] = {}
    for index, event in enumerate(included):
        positions.setdefault(_key(event), []).append(index)

    current_by_key: dict[tuple[str, str, str], int] = {}
    for key, indices in positions.items():
        highest = max(included[index]["record_version"] for index in indices)
        current = [index for index in indices if included[index]["record_version"] == highest]
        if len(current) != 1:
            raise LineageAmbiguityError(
                f"duplicate current record version for {key[2]!r} at version {highest}"
            )
        current_by_key[key] = current[0]

    # Phase 2 owns the two supersession rules and the ambiguity/cycle checks.
    superseded_indexes = frozenset(resolve_superseded(included))
    edges: list[dict[str, Any]] = []

    for key in sorted(positions):
        indices = positions[key]
        current_index = current_by_key[key]
        current = included[current_index]
        for index in indices:
            record = included[index]
            if record["record_version"] < current["record_version"]:
                edges.append(
                    _edge(
                        superseding=current,
                        superseded=record,
                        target_id=record["provider_event_id"],
                        rule="record_version",
                        status="resolved",
                    )
                )

    claims: dict[tuple[str, str, str], list[int]] = {}
    for index, event in enumerate(included):
        target = event["supersedes"]
        if target is None or target == event["provider_event_id"]:
            continue
        claims.setdefault((event["provider"], event["account_id"], target), []).append(index)

    dangling = 0
    for target_key in sorted(claims):
        claimants = claims[target_key]
        # resolve_superseded has already rejected multiple claimants. Keep the
        # guard local as well so this module never emits an ambiguous edge.
        if len(claimants) != 1:
            raise LineageAmbiguityError(
                f"more than one stored record claims to supersede {target_key[2]!r}"
            )
        claimant = included[claimants[0]]
        targets = positions.get(target_key, [])
        if not targets:
            dangling += 1
            edges.append(
                _edge(
                    superseding=claimant,
                    superseded=None,
                    target_id=target_key[2],
                    rule="supersedes",
                    status="dangling",
                )
            )
            continue
        for target_index in targets:
            target = included[target_index]
            edges.append(
                _edge(
                    superseding=claimant,
                    superseded=target,
                    target_id=target["provider_event_id"],
                    rule="supersedes",
                    status="resolved",
                )
            )

    edges.sort(
        key=lambda edge: (
            edge["superseding_event_id"],
            edge["superseded_event_id"],
            edge["rule"],
            -1 if edge["superseded_record_version"] is None else edge["superseded_record_version"],
            edge["status"],
        )
    )
    return LineageResult(
        events=included,
        superseded_indexes=superseded_indexes,
        edges=edges,
        excluded_by_cutoff=excluded,
        dangling_supersedes=dangling,
    )
