r"""Independent standard-library hand-calculation for three Phase 3 customers.

This file deliberately imports nothing from ``ai_cost_margin``. It re-reads the
stored events, price list, and simulated plans, applies the cutoff and the two
supersession rules, and uses exact ``fractions.Fraction`` arithmetic (including
the synthetic cached-token assumption S1). It then compares cust-001, cust-002,
and cust-003 against the pipeline's customer-margin table.

Usage:
    python tools\handcalc_margins.py --home var\phase3-evidence `
      --prices fixtures\phase2\price-list-synthetic-v1.json `
      --plans fixtures\phase3\customer-plans-simulated-v1.json `
      --cutoff 2026-11-01T23:59:59Z --run-dir <run fingerprint directory>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

CUSTOMERS = ("cust-001", "cust-002", "cust-003")
SCALE = 10**12


def parse_time(value: str) -> datetime:
    layout = "%Y-%m-%dT%H:%M:%S" + (".%f" if "." in value else "") + "Z"
    return datetime.strptime(value, layout).replace(tzinfo=timezone.utc)


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def money(value: Fraction) -> str:
    """Render an exact rational as twelve decimals, refusing any rounding."""
    scaled = value * SCALE
    if scaled.denominator != 1:
        raise ValueError("value is not exact at twelve decimal places")
    units = scaled.numerator
    sign = "-" if units < 0 else ""
    whole, fraction = divmod(abs(units), SCALE)
    return f"{sign}{whole}.{fraction:012d}"


def _key(event: dict[str, Any]) -> tuple[str, str, str]:
    return (event["provider"], event["account_id"], event["provider_event_id"])


def current_events(events: Sequence[dict[str, Any]], cutoff: str) -> list[dict[str, Any]]:
    """Independent cutoff and two-rule supersession implementation."""
    moment = parse_time(cutoff)
    included = [event for event in events if parse_time(event["arrival_time"]) <= moment]
    positions: dict[tuple[str, str, str], list[int]] = {}
    for index, event in enumerate(included):
        positions.setdefault(_key(event), []).append(index)

    superseded: set[int] = set()
    for indices in positions.values():
        highest = max(included[index]["record_version"] for index in indices)
        current = [index for index in indices if included[index]["record_version"] == highest]
        if len(current) != 1:
            raise ValueError("duplicate current record version")
        for index in indices:
            if included[index]["record_version"] < highest:
                superseded.add(index)

    claims: dict[tuple[str, str, str], list[int]] = {}
    for index, event in enumerate(included):
        target = event["supersedes"]
        if target is None or target == event["provider_event_id"]:
            continue
        claims.setdefault((event["provider"], event["account_id"], target), []).append(index)

    edges: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for target_key, claimants in claims.items():
        if len(claimants) != 1:
            raise ValueError("more than one correction claims the same target")
        for index in positions.get(target_key, []):
            superseded.add(index)
        edges[_key(included[claimants[0]])] = target_key

    for start in sorted(edges):
        seen: set[tuple[str, str, str]] = set()
        node = start
        while node in edges:
            if node in seen:
                raise ValueError("supersedes cycle")
            seen.add(node)
            node = edges[node]

    return [event for index, event in enumerate(included) if index not in superseded]


def price_entry(document: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    usage = parse_time(event["usage_time"])
    for entry in document["entries"]:
        if entry["provider"] != event["provider"] or entry["model"] != event["model"]:
            continue
        start = parse_time(entry["effective_from"])
        end = parse_time(entry["effective_to"]) if entry["effective_to"] is not None else None
        if start <= usage and (end is None or usage < end):
            return entry
    return None


def plan_price(document: dict[str, Any], customer_id: str, period: str) -> Fraction:
    start = parse_time(f"{period}-01T00:00:00Z")
    for entry in document["entries"]:
        if entry["customer_id"] != customer_id:
            continue
        effective_from = parse_time(entry["effective_from"])
        effective_to = parse_time(entry["effective_to"]) if entry["effective_to"] is not None else None
        if effective_from <= start and (effective_to is None or start < effective_to):
            return Fraction(entry["monthly_price"])
    raise ValueError(f"no plan covers {customer_id} in {period}")


def compute_customer_rows(
    events: Sequence[dict[str, Any]],
    prices: dict[str, Any],
    plans: dict[str, Any],
    cutoff: str,
    customers: Sequence[str] = CUSTOMERS,
) -> list[dict[str, Any]]:
    current = current_events(events, cutoff)
    rows: list[dict[str, Any]] = []
    for customer_id in customers:
        periods = sorted({event["usage_time"][:7] for event in current if event["customer_id"] == customer_id})
        for period in periods:
            selected = [
                event for event in current
                if event["customer_id"] == customer_id and event["usage_time"][:7] == period
            ]
            revenue = plan_price(plans, customer_id, period)
            cost = Fraction(0)
            costed = 0
            unpriced = 0
            for event in selected:
                entry = price_entry(prices, event)
                tokens_input = event["tokens_input"]
                tokens_output = event["tokens_output"]
                tokens_cached = event["tokens_cached"]
                if (
                    entry is None
                    or tokens_input is None
                    or tokens_output is None
                    or tokens_cached is None
                    or tokens_cached > tokens_input
                ):
                    unpriced += 1
                    continue
                rates = entry["rates_per_million"]
                input_cost = (tokens_input - tokens_cached) * Fraction(rates["input"]) / 1_000_000
                cached_cost = tokens_cached * Fraction(rates["cached"]) / 1_000_000
                output_cost = tokens_output * Fraction(rates["output"]) / 1_000_000
                cost += input_cost + cached_cost + output_cost
                costed += 1
            estimated_cost = money(cost) if costed else None
            rows.append(
                {
                    "period": period,
                    "customer_id": customer_id,
                    "events_costed": costed,
                    "events_unpriced": unpriced,
                    "estimated_cost": estimated_cost,
                    "simulated_revenue": money(revenue),
                    "simulated_margin": money(revenue - cost) if costed else None,
                    "margin_status": "complete" if costed and not unpriced else "incomplete",
                }
            )
    return rows


def compare_rows(
    computed: Sequence[dict[str, Any]],
    table_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    fields = (
        "events_costed",
        "events_unpriced",
        "estimated_cost",
        "simulated_revenue",
        "simulated_margin",
        "margin_status",
    )
    indexed = {(row["period"], row["customer_id"]): row for row in table_rows}
    comparisons: list[dict[str, Any]] = []
    for row in computed:
        key = (row["period"], row["customer_id"])
        table = indexed.get(key)
        differences = [] if table is not None else ["missing customer-margin row"]
        if table is not None:
            differences = [field for field in fields if row[field] != table[field]]
        comparisons.append(
            {
                "computed": row,
                "table": table,
                "differences": differences,
                "match": not differences,
            }
        )
    return comparisons


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent hand-calculation for cust-001..003")
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--plans", required=True, type=Path)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        events = read_jsonl(args.home / "state" / "events.jsonl")
        prices = read_json(args.prices)
        plans = read_json(args.plans)
        table_rows = read_jsonl(args.run_dir / "customer-margins.jsonl")
        computed = compute_customer_rows(events, prices, plans, args.cutoff)
        comparisons = compare_rows(computed, table_rows)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("customer  period   costed  unpriced  estimated_cost     simulated_revenue  simulated_margin   status      match")
    for comparison in comparisons:
        row = comparison["computed"]
        print(
            f"{row['customer_id']}  {row['period']}  {row['events_costed']:>6}  "
            f"{row['events_unpriced']:>8}  {str(row['estimated_cost']):>16}  "
            f"{row['simulated_revenue']:>16}  {str(row['simulated_margin']):>16}  "
            f"{row['margin_status']:<10}  {'yes' if comparison['match'] else 'no'}"
        )
        if not comparison["match"]:
            print(f"  differences: {', '.join(comparison['differences'])}", file=sys.stderr)
    matched = sum(comparison["match"] for comparison in comparisons)
    print(f"hand-calc: {matched}/{len(comparisons)} customer-month rows match")
    return 0 if comparisons and matched == len(comparisons) else 2


if __name__ == "__main__":
    raise SystemExit(main())
