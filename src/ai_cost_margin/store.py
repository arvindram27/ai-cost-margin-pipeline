"""Canonical serialisation and append-only JSONL access for the private store.

Two serialisers exist, and nothing else may serialise store data:

* ``canonical_json`` - compact, sorted keys, ASCII only. Every JSONL row goes
  through it, so the same inputs produce byte-identical files on any machine.
* ``pretty_json`` - sorted keys, two-space indent. Used for the committed
  fixture files and the run report, where a human has to read them.

Neither serialiser emits a timestamp, a host name, a process id or an absolute
path, so no committed artifact can vary between machines or runs.

The store is append-only. A line that cannot be parsed is a hard stop
(``StoreError``); it is never skipped, truncated or repaired.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "Store",
    "StoreError",
    "append_jsonl",
    "canonical_json",
    "content_sha256",
    "payload_sha256",
    "pretty_json",
    "read_jsonl",
]


class StoreError(Exception):
    """The private store is missing, unreadable or corrupt."""


def canonical_json(record: dict[str, Any]) -> str:
    """Compact, deterministic JSON: sorted keys, no whitespace, ASCII only."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def pretty_json(record: dict[str, Any]) -> str:
    """Readable but still deterministic JSON: sorted keys, two-space indent."""
    return json.dumps(record, sort_keys=True, indent=2, ensure_ascii=True)


def payload_sha256(record: dict[str, Any]) -> str:
    """Hash of an event payload, independent of the key order it arrived in."""
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


def content_sha256(data: bytes) -> str:
    """Hash of a delivered file's exact bytes."""
    return hashlib.sha256(data).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of objects.

    A missing file is an empty store. A line that is not a JSON object raises
    ``StoreError`` with its line number: the store is never partially repaired.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read {path.name}: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        if line == "":
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path.name} line {lineno} is not valid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise StoreError(f"{path.name} line {lineno} is not a JSON object")
        rows.append(row)
    return rows


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Append canonical rows, one per line, with ``\\n`` line endings."""
    pending = list(rows)
    if not pending:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for row in pending:
                handle.write(canonical_json(row))
                handle.write("\n")
    except OSError as exc:
        raise StoreError(f"cannot append to {path.name}: {exc}") from exc


@dataclass(frozen=True)
class Store:
    """Paths and small helpers for one private store root (``var/``)."""

    home: Path

    @property
    def raw_dir(self) -> Path:
        return self.home / "raw"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def events_path(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / "ingest-manifest.jsonl"

    @property
    def dead_letter_path(self) -> Path:
        return self.state_dir / "dead-letter.jsonl"

    @property
    def quarantine_path(self) -> Path:
        return self.state_dir / "quarantine.jsonl"

    @property
    def report_path(self) -> Path:
        return self.state_dir / "last-ingest-report.json"

    def write_raw(self, digest: str, data: bytes) -> None:
        """Keep a content-addressed copy of a delivered file (idempotent)."""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        target = self.raw_dir / f"{digest}.json"
        if not target.exists():
            target.write_bytes(data)

    def write_report(self, report: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(pretty_json(report) + "\n", encoding="utf-8", newline="\n")
