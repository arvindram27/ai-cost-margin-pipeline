"""Costing engine: Phase 1 events x a synthetic price list -> costed events.

Three outcomes exist and they are deliberately different things:

* ``costed`` - a rate covered the event's usage time and every token count was
  available; the cost is exact and labelled ``estimated``.
* ``unpriced`` - the cost is unknown (no applicable rate, the model is not
  listed, a token count is unavailable, or the counts contradict the cached-token
  assumption). The amounts are null, never zero, and the run continues.
* ``superseded`` - the stored record was replaced by a later record, so charging
  it would charge the same usage twice. It is neither costed nor unpriced.

A fatal price schedule never reaches this module: ``pricing`` raises before any
event is read.

Cached-token assumption S1 (synthetic, not a claim about any real provider):
cached tokens are a subset of ``tokens_input`` and are billed at the cached rate
instead of the input rate. Input cost therefore uses
``tokens_input - tokens_cached``, and ``tokens_cached > tokens_input`` is a
contradiction that leaves the cost unknown.

Supersession is resolved by exactly two rules and nothing else: a lower
``record_version`` for the same (provider, account, provider_event_id), or a
different event id under the same (provider, account) naming this id in
``supersedes``. A record never supersedes itself. Anything those two rules
cannot resolve unambiguously is an error, not a guess.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .money import Money
from .pricing import UNITS, PriceList, PriceScheduleError, load_price_list, parse_utc
from .store import Store, StoreError, canonical_json, pretty_json, read_jsonl

__all__ = [
    "COST_LABEL",
    "CostingError",
    "LineageAmbiguityError",
    "OutputError",
    "UNPRICED_REASONS",
    "cost_event",
    "cost_events",
    "main",
    "resolve_superseded",
    "write_outputs",
]

COST_LABEL = "estimated"

STATUS_COSTED = "costed"
STATUS_UNPRICED = "unpriced"
STATUS_SUPERSEDED = "superseded"

UNPRICED_REASONS = (
    "inconsistent_token_counts",
    "model_not_in_price_list",
    "no_applicable_rate",
    "token_count_unavailable",
)


class CostingError(Exception):
    """A stored record cannot be costed because the store itself is unusable."""


class LineageAmbiguityError(Exception):
    """The store's supersession references are ambiguous; costing must stop."""


class OutputError(Exception):
    """The output directory cannot be written safely."""


def resolve_superseded(events: Sequence[dict]) -> set[int]:
    """Indexes of stored records that a later record replaced."""
    if not events:
        return set()

    def key_of(event: dict) -> tuple[str, str, str]:
        return (event["provider"], event["account_id"], event["provider_event_id"])

    positions: dict[tuple[str, str, str], list[int]] = {}
    for index, event in enumerate(events):
        positions.setdefault(key_of(event), []).append(index)

    superseded: set[int] = set()
    for indices in positions.values():
        highest = max(events[index]["record_version"] for index in indices)
        for index in indices:
            if events[index]["record_version"] < highest:
                superseded.add(index)

    claims: dict[tuple[str, str, str], list[int]] = {}
    for index, event in enumerate(events):
        target = event["supersedes"]
        if target is None:
            continue
        if target == event["provider_event_id"]:
            # A record never supersedes itself.
            continue
        claims.setdefault((event["provider"], event["account_id"], target), []).append(index)

    for target_key in sorted(claims):
        claimants = claims[target_key]
        if len(claimants) > 1:
            raise LineageAmbiguityError(
                f"more than one stored record claims to supersede {target_key[2]!r}"
            )
        for index in positions.get(target_key, []):
            superseded.add(index)

    edges: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for target_key, claimants in claims.items():
        edges[key_of(events[claimants[0]])] = target_key
    for start in sorted(edges):
        seen: set[tuple[str, str, str]] = set()
        node = start
        while node in edges:
            if node in seen:
                raise LineageAmbiguityError(f"supersedes cycle involving {node[2]!r}")
            seen.add(node)
            node = edges[node]

    return superseded


def _base_row(record: dict, price_list: PriceList) -> dict[str, Any]:
    return {
        "account_id": record["account_id"],
        "attempt_id": record["attempt_id"],
        "cost_cached": None,
        "cost_input": None,
        "cost_label": COST_LABEL,
        "cost_output": None,
        "cost_status": STATUS_UNPRICED,
        "currency": None,
        "customer_id": record["customer_id"],
        "feature": record["feature"],
        "model": record["model"],
        "payload_sha256": record["payload_sha256"],
        "price_list_id": price_list.price_list_id,
        "price_list_label": price_list.label,
        "price_version_cached": None,
        "price_version_input": None,
        "price_version_output": None,
        "provider": record["provider"],
        "provider_event_id": record["provider_event_id"],
        "record_version": record["record_version"],
        "request_id": record["request_id"],
        "supersedes": record["supersedes"],
        "total_cost": None,
        "unpriced_reason": None,
        "usage_time": record["usage_time"],
        "workload_type": record["workload_type"],
    }


def cost_event(record: dict, price_list: PriceList) -> dict[str, Any]:
    """Cost one stored event, or classify it as unpriced with a reason."""
    provider = record["provider"]
    model = record["model"]
    row = _base_row(record, price_list)

    if not price_list.knows(provider, model):
        row["unpriced_reason"] = "model_not_in_price_list"
        return row

    try:
        selection = price_list.select(provider, model, record["usage_time"])
    except ValueError as exc:
        raise CostingError(
            f"stored event {record['provider_event_id']!r} has an unusable usage_time"
        ) from exc
    if selection is None:
        row["unpriced_reason"] = "no_applicable_rate"
        return row

    for field in ("tokens_input", "tokens_output", "tokens_cached"):
        if record[field] is None:
            row["unpriced_reason"] = "token_count_unavailable"
            return row

    if record["tokens_cached"] > record["tokens_input"]:
        row["unpriced_reason"] = "inconsistent_token_counts"
        return row

    currency = selection.currency
    cost_input = Money.from_tokens_and_micro_rate(
        record["tokens_input"] - record["tokens_cached"],
        selection.rates_micro["input"],
        currency,
    )
    cost_cached = Money.from_tokens_and_micro_rate(
        record["tokens_cached"], selection.rates_micro["cached"], currency
    )
    cost_output = Money.from_tokens_and_micro_rate(
        record["tokens_output"], selection.rates_micro["output"], currency
    )
    total = (cost_input + cost_cached + cost_output).quantise()

    row.update(
        {
            "cost_cached": str(cost_cached),
            "cost_input": str(cost_input),
            "cost_output": str(cost_output),
            "cost_status": STATUS_COSTED,
            "currency": currency,
            "price_version_cached": selection.price_version_ids["cached"],
            "price_version_input": selection.price_version_ids["input"],
            "price_version_output": selection.price_version_ids["output"],
            "total_cost": str(total),
        }
    )
    return row


def cost_events(events: Sequence[dict], price_list: PriceList) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Cost every stored event, in store order, and build the run report."""
    superseded = resolve_superseded(events)
    rows: list[dict[str, Any]] = []
    costed = 0
    unpriced = 0
    reasons = {reason: 0 for reason in UNPRICED_REASONS}
    versions: set[str] = set()

    for index, record in enumerate(events):
        if index in superseded:
            row = _base_row(record, price_list)
            row["cost_status"] = STATUS_SUPERSEDED
            rows.append(row)
            continue
        row = cost_event(record, price_list)
        rows.append(row)
        if row["cost_status"] == STATUS_COSTED:
            costed += 1
            for unit in UNITS:
                versions.add(row[f"price_version_{unit}"])
        else:
            unpriced += 1
            reasons[row["unpriced_reason"]] += 1

    report = {
        "cost_label": COST_LABEL,
        "currency": price_list.currency,
        "events_costed": costed,
        "events_read": len(events),
        "events_superseded": len(superseded),
        "events_unpriced": unpriced,
        "price_list_id": price_list.price_list_id,
        "price_list_label": price_list.label,
        "price_versions_used": sorted(versions),
        "unpriced_by_reason": reasons,
    }
    return rows, report


def write_outputs(out_dir: Path, rows: Sequence[dict], report: dict) -> None:
    """Write the pair of outputs, refusing to touch any pre-existing path.

    Everything is computed before this is called, so a schedule or store error
    leaves nothing behind. This is deliberately not a two-file atomic publish:
    on an I/O failure the directory may hold an incomplete pair, and the nonzero
    exit is what invalidates both files. Nothing is ever deleted or cleaned up;
    a rerun uses a fresh directory. True transactional publish arrives in
    Phase 4.
    """
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise OutputError(f"output directory already exists: {out_dir.name}")
    try:
        out_dir.mkdir(parents=True)
    except OSError as exc:
        raise OutputError(f"cannot create the output directory {out_dir.name}: {exc}") from exc

    final_events = out_dir / "costed-events.jsonl"
    final_report = out_dir / "costing-report.json"
    tmp_events = out_dir / "costed-events.jsonl.tmp"
    tmp_report = out_dir / "costing-report.json.tmp"
    for path in (final_events, final_report, tmp_events, tmp_report):
        if path.exists():
            raise OutputError(f"refusing to overwrite an existing output path: {path.name}")

    try:
        tmp_events.write_text(
            "".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8", newline="\n"
        )
        tmp_report.write_text(pretty_json(report) + "\n", encoding="utf-8", newline="\n")
        tmp_events.replace(final_events)
        tmp_report.replace(final_report)
    except OSError as exc:
        raise OutputError(f"cannot write the outputs: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_cost_margin.costing",
        description="Cost validated Phase 1 events against a synthetic price list.",
    )
    parser.add_argument("--home", required=True, type=Path, help="private store root, e.g. var")
    parser.add_argument("--prices", required=True, type=Path, help="price-list JSON file")
    parser.add_argument(
        "--out", required=True, type=Path, help="fresh output directory that must not exist yet"
    )
    args = parser.parse_args(argv)

    try:
        price_list = load_price_list(args.prices)
        events = read_jsonl(Store(args.home).events_path)
        rows, report = cost_events(events, price_list)
        write_outputs(args.out, rows, report)
    except PriceScheduleError as exc:
        print(
            f"error: price schedule is invalid ({len(exc.violations)} violation(s)):",
            file=sys.stderr,
        )
        for violation in exc.violations:
            print(f"  {violation}", file=sys.stderr)
        return 2
    except (LineageAmbiguityError, CostingError, StoreError, OutputError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"costed {report['events_read']} events -> {args.out.name}")
    print(
        f"  costed={report['events_costed']} unpriced={report['events_unpriced']} "
        f"superseded={report['events_superseded']} currency={report['currency']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
