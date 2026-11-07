"""Store adapter seam for ingestion (Phase 4 amendment A6).

``ingest`` owns validation and dedup exactly once. Everything it needs from the
private store - the dedup-state reads and the appends - goes through the small
protocol in this module. Two implementations exist and nothing else:

* :class:`JsonlStoreAdapter` - the default. It delegates to ``store.py`` and
  therefore reproduces the Phase 1 JSONL behaviour byte for byte.
* :class:`ai_cost_margin.runstore.DuckDbStoreAdapter` - the Phase 4 runner's
  implementation, backed by the single DuckDB working database.

The adapter is deliberately narrow. It never validates an event, never decides
whether a row is a duplicate, and never serialises a row itself: it only reads
and appends the rows ``ingest`` has already built.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from .store import Store, append_jsonl, read_jsonl

__all__ = ["JsonlStoreAdapter", "StoreAdapter", "default_adapter"]


class StoreAdapter(Protocol):
    """The reads and appends ingestion requires from a private store."""

    def read_events(self) -> list[dict[str, Any]]:
        """Every stored event record, in a deterministic order."""

    def read_manifest(self) -> list[dict[str, Any]]:
        """Every stored ingest-manifest row, in a deterministic order."""

    def read_dead_letter(self) -> list[dict[str, Any]]:
        """Every stored dead-letter row, in a deterministic order."""

    def read_quarantine(self) -> list[dict[str, Any]]:
        """Every stored quarantine row, in a deterministic order."""

    def write_raw(self, digest: str, data: bytes) -> None:
        """Keep or verify the content-addressed raw copy of a delivered file."""

    def append_events(self, rows: Iterable[dict[str, Any]]) -> None:
        """Append validated event records."""

    def append_manifest(self, rows: Iterable[dict[str, Any]]) -> None:
        """Append ingest-manifest rows."""

    def append_dead_letter(self, rows: Iterable[dict[str, Any]]) -> None:
        """Append dead-letter rows."""

    def append_quarantine(self, rows: Iterable[dict[str, Any]]) -> None:
        """Append quarantine rows."""

    def write_report(self, report: dict[str, Any]) -> None:
        """Record the result of this ingest invocation."""


@dataclass(frozen=True)
class JsonlStoreAdapter:
    """The Phase 1 JSONL store, unchanged in behaviour, behind the seam."""

    store: Store

    def read_events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.store.events_path)

    def read_manifest(self) -> list[dict[str, Any]]:
        return read_jsonl(self.store.manifest_path)

    def read_dead_letter(self) -> list[dict[str, Any]]:
        return read_jsonl(self.store.dead_letter_path)

    def read_quarantine(self) -> list[dict[str, Any]]:
        return read_jsonl(self.store.quarantine_path)

    def write_raw(self, digest: str, data: bytes) -> None:
        self.store.write_raw(digest, data)

    def append_events(self, rows: Iterable[dict[str, Any]]) -> None:
        append_jsonl(self.store.events_path, rows)

    def append_manifest(self, rows: Iterable[dict[str, Any]]) -> None:
        append_jsonl(self.store.manifest_path, rows)

    def append_dead_letter(self, rows: Iterable[dict[str, Any]]) -> None:
        append_jsonl(self.store.dead_letter_path, rows)

    def append_quarantine(self, rows: Iterable[dict[str, Any]]) -> None:
        append_jsonl(self.store.quarantine_path, rows)

    def write_report(self, report: dict[str, Any]) -> None:
        self.store.write_report(report)


def default_adapter(home: Path | str) -> JsonlStoreAdapter:
    """The default JSONL adapter for a private store rooted at ``home``."""
    return JsonlStoreAdapter(Store(Path(home)))
