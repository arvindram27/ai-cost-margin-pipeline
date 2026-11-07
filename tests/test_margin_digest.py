"""Canonical financial-table ordering and result digest definition."""

from __future__ import annotations

import hashlib
import re

from ai_cost_margin.digest import (
    DIGEST_DEFINITION,
    compute_digest,
    digest_text,
    render_table,
    sort_rows,
)


def _customer(customer_id, margin="1.000000000000"):
    return {
        "period": "2026-10",
        "customer_id": customer_id,
        "simulated_margin": margin,
    }


def _workload(customer_id, workload):
    return {
        "period": "2026-10",
        "customer_id": customer_id,
        "workload_type": workload,
    }


def test_empty_digest_has_the_exact_defined_byte_stream():
    stream = (
        "ai-cost-margin/margin-digest/v1\n"
        "table customer_margins 0\n"
        "table workload_margins 0\n"
        "table unallocated 0\n"
    ).encode("utf-8")
    assert compute_digest([], [], []) == hashlib.sha256(stream).hexdigest()


def test_digest_is_invariant_under_input_row_reordering():
    customers = [_customer("cust-002"), _customer("cust-001")]
    workloads = [
        _workload("cust-001", "spiky"),
        _workload("cust-001", "cache_heavy"),
        _workload("cust-001", "short_light"),
    ]
    first = compute_digest(customers, workloads, [])
    second = compute_digest(list(reversed(customers)), list(reversed(workloads)), [])
    assert first == second


def test_a_changed_financial_value_changes_the_digest():
    first = compute_digest([_customer("cust-001", "1.000000000000")], [], [])
    second = compute_digest([_customer("cust-001", "1.000000000001")], [], [])
    assert first != second


def test_customer_and_workload_sort_orders_are_explicit():
    customers = sort_rows("customer_margins", [_customer("cust-002"), _customer("cust-001")])
    assert [row["customer_id"] for row in customers] == ["cust-001", "cust-002"]
    workloads = sort_rows(
        "workload_margins",
        [
            _workload("cust-001", "spiky"),
            _workload("cust-001", "short_light"),
            _workload("cust-001", "long_heavy"),
            _workload("cust-001", "cache_heavy"),
        ],
    )
    assert [row["workload_type"] for row in workloads] == [
        "cache_heavy",
        "long_heavy",
        "short_light",
        "spiky",
    ]


def test_rendered_rows_are_canonical_json_and_digest_file_is_two_lines():
    rendered = render_table(
        "customer_margins",
        [{"period": "2026-10", "customer_id": "cust-001", "z": 1, "a": 2}],
    )
    assert rendered == b'{"a":2,"customer_id":"cust-001","period":"2026-10","z":1}\n'
    digest = compute_digest([], [], [])
    assert digest_text(digest) == f"{DIGEST_DEFINITION}\n{digest}\n".encode("utf-8")
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
