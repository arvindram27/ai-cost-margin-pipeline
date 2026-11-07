"""Canonical Phase 3 financial-table serialization and result digest.

The digest covers exactly three financial tables, in this order:
``customer_margins``, ``workload_margins``, ``unallocated``. It excludes run
IDs, timestamps, code-version metadata, reports, manifests, and lineage rows.
Rows are sorted by their defined keys before canonical JSON serialization.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Sequence

from .revenue import workload_sort_key
from .store import canonical_json

__all__ = [
    "AGGREGATION_DEFINITION_ID",
    "ARTIFACT_SCHEMA_VERSION",
    "DIGEST_DEFINITION",
    "TABLE_FILES",
    "compute_digest",
    "digest_stream",
    "digest_text",
    "render_table",
    "sort_rows",
]

ARTIFACT_SCHEMA_VERSION = "1.0"
AGGREGATION_DEFINITION_ID = "customer-workload-margin/v1"
DIGEST_DEFINITION = "ai-cost-margin/margin-digest/v1"

TABLE_FILES = {
    "customer_margins": "customer-margins.jsonl",
    "workload_margins": "workload-margins.jsonl",
    "unallocated": "unallocated.jsonl",
    "lineage": "lineage.jsonl",
}

_SORT_KEYS: dict[str, Callable[[dict[str, Any]], tuple[Any, ...]]] = {
    "customer_margins": lambda row: (row["period"], row["customer_id"]),
    "workload_margins": lambda row: (
        row["period"],
        row["customer_id"],
        workload_sort_key(row["workload_type"]),
    ),
    "unallocated": lambda row: (row["period"], workload_sort_key(row["workload_type"])),
}


def sort_rows(table_name: str, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rows in the digest-defined order without mutating the input."""
    return sorted(rows, key=_SORT_KEYS[table_name])


def render_table(table_name: str, rows: Sequence[dict[str, Any]]) -> bytes:
    """Render one canonical JSONL table in its defined sort order."""
    return "".join(
        canonical_json(row) + "\n" for row in sort_rows(table_name, rows)
    ).encode("utf-8")


def digest_stream(
    customer_margins: Sequence[dict[str, Any]],
    workload_margins: Sequence[dict[str, Any]],
    unallocated: Sequence[dict[str, Any]],
) -> bytes:
    """Build the exact byte stream whose SHA-256 is the result digest."""
    tables = (
        ("customer_margins", customer_margins),
        ("workload_margins", workload_margins),
        ("unallocated", unallocated),
    )
    chunks = [f"{DIGEST_DEFINITION}\n".encode("utf-8")]
    for table_name, rows in tables:
        ordered = sort_rows(table_name, rows)
        chunks.append(f"table {table_name} {len(ordered)}\n".encode("utf-8"))
        for row in ordered:
            chunks.append((canonical_json(row) + "\n").encode("utf-8"))
    return b"".join(chunks)


def compute_digest(
    customer_margins: Sequence[dict[str, Any]],
    workload_margins: Sequence[dict[str, Any]],
    unallocated: Sequence[dict[str, Any]],
) -> str:
    """Return the lowercase SHA-256 hex digest of the financial tables."""
    return hashlib.sha256(digest_stream(customer_margins, workload_margins, unallocated)).hexdigest()


def digest_text(digest: str) -> bytes:
    """The two-line digest artifact: definition, then the 64-hex value."""
    return f"{DIGEST_DEFINITION}\n{digest}\n".encode("utf-8")
