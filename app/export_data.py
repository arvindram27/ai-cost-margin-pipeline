"""Read-only verified loader for the committed Phase 5 export.

The loader reads the eight export files under ``app/export/``, verifies the
export integrity digest over the exact bytes and the financial digest over
the three margin tables, and returns deeply immutable, pickle-compatible
records. Any verification failure raises :class:`ExportIntegrityError`; the
app must never render unverified data.

This module imports no Streamlit. Caching lives in the entrypoint: an
uncached read produces a tuple of (name, bytes) pairs, and verification and
parsing run inside the cached function on exactly those bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_cost_margin.digest import DIGEST_DEFINITION, compute_digest
from ai_cost_margin.export import EXPORT_INTEGRITY_DEFINITION, export_integrity_digest

__all__ = [
    "DATA_FILE_NAMES",
    "MANIFEST_NAME",
    "ExportBundle",
    "ExportIntegrityError",
    "Record",
    "default_export_dir",
    "load_export",
    "load_export_from_bytes",
    "read_export_files",
]

MANIFEST_NAME = "export-manifest.json"
DATA_FILE_NAMES = (
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "scenario-events.jsonl",
    "lineage.jsonl",
    "price-list.json",
    "plans.json",
)

MONEY_PATTERN = re.compile(r"^-?\d+\.\d{12}$")
MONEY_FIELDS = {
    "customer-margins.jsonl": ("estimated_cost", "simulated_revenue", "simulated_margin"),
    "workload-margins.jsonl": ("estimated_cost", "simulated_revenue", "simulated_margin"),
    "unallocated.jsonl": ("estimated_cost",),
}
_JSONL_TABLES = {
    "customer-margins.jsonl": "customer_margins",
    "workload-margins.jsonl": "workload_margins",
    "unallocated.jsonl": "unallocated",
}


class ExportIntegrityError(RuntimeError):
    """The committed export does not verify; it must not be rendered."""


@dataclass(frozen=True)
class Record:
    """One immutable export row (pickle-compatible: tuples and scalars only)."""

    items: tuple[tuple[str, Any], ...]

    def __getitem__(self, key: str) -> Any:
        for name, value in self.items:
            if name == key:
                return value
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.items)

    def as_dict(self) -> dict[str, Any]:
        """A thawed plain-dict copy of this record."""
        return {name: _thaw(value) for name, value in self.items}


@dataclass(frozen=True)
class ExportBundle:
    """The verified export: frozen rows, documents, and both digests."""

    manifest: Record
    customer_margins: tuple[Record, ...]
    workload_margins: tuple[Record, ...]
    unallocated: tuple[Record, ...]
    scenario_events: tuple[Record, ...]
    lineage: tuple[Record, ...]
    price_list: Record
    plans: Record
    financial_digest: str
    integrity_digest: str


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return Record(tuple((key, _freeze(value[key])) for key in sorted(value)))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Record):
        return {name: _thaw(item) for name, item in value.items}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def default_export_dir() -> Path:
    """The committed export directory, relative to this file (never the CWD)."""
    return Path(__file__).resolve().parent / "export"


def read_export_files(
    export_dir: Path | None = None,
) -> tuple[tuple[str, bytes], ...]:
    """Read all eight export files as (name, bytes) pairs sorted by name."""
    directory = default_export_dir() if export_dir is None else Path(export_dir)
    if not directory.is_dir():
        raise ExportIntegrityError(f"export directory not found: {directory.name}")
    names = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.suffix in (".json", ".jsonl")
    }
    expected = {MANIFEST_NAME, *DATA_FILE_NAMES}
    if names != expected:
        raise ExportIntegrityError(
            f"export file set mismatch: missing={sorted(expected - names)} "
            f"extra={sorted(names - expected)}"
        )
    pairs: list[tuple[str, bytes]] = []
    for name in sorted(expected):
        try:
            pairs.append((name, (directory / name).read_bytes()))
        except OSError as exc:
            raise ExportIntegrityError(f"cannot read export file {name}: {exc}") from exc
    return tuple(pairs)


def _parse_json(data: bytes, name: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportIntegrityError(f"{name} is not valid JSON: {exc}") from exc


def _parse_jsonl(data: bytes, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExportIntegrityError(f"{name} is not valid UTF-8: {exc}") from exc
    for line in text.splitlines():
        if line == "":
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExportIntegrityError(f"{name} contains an invalid JSON line: {exc}") from exc
        if not isinstance(row, dict):
            raise ExportIntegrityError(f"{name} contains a non-object JSON line")
        rows.append(row)
    return rows


def load_export_from_bytes(files: tuple[tuple[str, bytes], ...]) -> ExportBundle:
    """Verify the exact export bytes and return the frozen bundle."""
    names = [name for name, _ in files]
    if len(files) != 8 or len(set(names)) != len(names):
        raise ExportIntegrityError("export input must hold exactly 8 uniquely named files")
    if set(names) != {MANIFEST_NAME, *DATA_FILE_NAMES}:
        raise ExportIntegrityError(f"export file set mismatch: {sorted(names)}")
    by_name = dict(files)

    manifest = _parse_json(by_name[MANIFEST_NAME], MANIFEST_NAME)
    if not isinstance(manifest, dict):
        raise ExportIntegrityError("export manifest is not a JSON object")

    parsed_rows: dict[str, list[dict[str, Any]]] = {}
    parsed_docs: dict[str, dict[str, Any]] = {}
    for name in DATA_FILE_NAMES:
        if name.endswith(".jsonl"):
            parsed_rows[name] = _parse_jsonl(by_name[name], name)
        else:
            document = _parse_json(by_name[name], name)
            if not isinstance(document, dict):
                raise ExportIntegrityError(f"{name} is not a JSON object")
            parsed_docs[name] = document

    if manifest.get("export_integrity_definition") != EXPORT_INTEGRITY_DEFINITION:
        raise ExportIntegrityError("export integrity definition mismatch")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("digest_definition") != DIGEST_DEFINITION:
        raise ExportIntegrityError("source digest definition mismatch")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict):
        raise ExportIntegrityError("export manifest has no files block")

    counted: dict[str, dict[str, Any]] = {}
    for name in DATA_FILE_NAMES:
        data = by_name[name]
        digest = hashlib.sha256(data).hexdigest()
        entry = manifest_files.get(name)
        if not isinstance(entry, dict) or entry.get("sha256") != digest:
            raise ExportIntegrityError(f"{name} sha256 does not match the export manifest")
        if name.endswith(".jsonl"):
            rows = len(parsed_rows[name])
        else:
            entries = parsed_docs[name].get("entries")
            if not isinstance(entries, list):
                raise ExportIntegrityError(f"{name} has no entries list")
            rows = len(entries)
        if entry.get("rows") != rows:
            raise ExportIntegrityError(f"{name} row count does not match the export manifest")
        counted[name] = {"rows": rows, "sha256": digest}

    integrity_digest = export_integrity_digest(counted)
    if integrity_digest != manifest.get("export_integrity_digest"):
        raise ExportIntegrityError("export integrity digest mismatch")

    thawed_tables: dict[str, list[dict[str, Any]]] = {}
    for name, bundle_key in _JSONL_TABLES.items():
        rows = parsed_rows[name]
        for row in rows:
            for field in MONEY_FIELDS[name]:
                value = row.get(field)
                if value is not None and (
                    not isinstance(value, str) or MONEY_PATTERN.match(value) is None
                ):
                    raise ExportIntegrityError(
                        f"{name} field {field} is not a 12-decimal money string"
                    )
        thawed_tables[bundle_key] = rows

    financial_digest = compute_digest(
        thawed_tables["customer_margins"],
        thawed_tables["workload_margins"],
        thawed_tables["unallocated"],
    )
    if financial_digest != source.get("digest"):
        raise ExportIntegrityError("financial digest does not match the manifest attestation")

    return ExportBundle(
        manifest=_freeze(manifest),
        customer_margins=tuple(_freeze(row) for row in parsed_rows["customer-margins.jsonl"]),
        workload_margins=tuple(_freeze(row) for row in parsed_rows["workload-margins.jsonl"]),
        unallocated=tuple(_freeze(row) for row in parsed_rows["unallocated.jsonl"]),
        scenario_events=tuple(_freeze(row) for row in parsed_rows["scenario-events.jsonl"]),
        lineage=tuple(_freeze(row) for row in parsed_rows["lineage.jsonl"]),
        price_list=_freeze(parsed_docs["price-list.json"]),
        plans=_freeze(parsed_docs["plans.json"]),
        financial_digest=financial_digest,
        integrity_digest=integrity_digest,
    )


def load_export(export_dir: Path | None = None) -> ExportBundle:
    """Read and verify the committed export, returning the frozen bundle."""
    return load_export_from_bytes(read_export_files(export_dir))
