"""Code-version provenance, run fingerprints, and replay-safe artifact writes.

Production provenance always comes from read-only git probes. A dirty tree is
recorded explicitly, never presented as clean HEAD provenance. The dirty hash
covers path, porcelain status, and the working-tree file content hash for every
entry (including individual untracked files); deleted paths use a fixed marker.

Golden generation uses the internal ``fixture_code_version`` path instead of a
live HEAD, so a committed golden can never depend on its own future commit.

Validation happens before this module is asked to write. A mid-write OS failure
can leave a partial run directory; the next identical run detects it through
the byte-compare replay check and exits 2. Nothing is deleted or repaired.
Atomic publish remains M4 scope.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .digest import AGGREGATION_DEFINITION_ID, ARTIFACT_SCHEMA_VERSION
from .store import canonical_json, content_sha256

__all__ = [
    "CodeVersion",
    "CodeVersionError",
    "OutputError",
    "ReplayMismatchError",
    "RunWriteResult",
    "compute_run_fingerprint",
    "dirty_status_sha256",
    "fixture_code_version",
    "hash_canonical",
    "probe_git",
    "resolve_code_version",
    "write_run_artifact",
]

_HEAD_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DELETED_MARKER = "<deleted>"


class CodeVersionError(ValueError):
    """Git HEAD or working-tree provenance cannot be resolved honestly."""


class OutputError(OSError):
    """A run artifact cannot be created or written."""


class ReplayMismatchError(ValueError):
    """An existing run directory is not an exact replay of the new result."""


@dataclass(frozen=True)
class CodeVersion:
    code_version: str
    code_version_source: str
    working_tree_clean: bool | None
    dirty_status_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code_version": self.code_version,
            "code_version_source": self.code_version_source,
            "working_tree_clean": self.working_tree_clean,
            "dirty_status_sha256": self.dirty_status_sha256,
        }


@dataclass(frozen=True)
class RunWriteResult:
    run_dir: Path
    replayed: bool


def hash_canonical(value: Any) -> str:
    """SHA-256 of the canonical JSON representation of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def fixture_code_version(value: str) -> CodeVersion:
    """Fixed test/golden provenance; never calls git."""
    if not isinstance(value, str) or value == "":
        raise CodeVersionError("fixture code version must be a non-empty string")
    return CodeVersion(value, "fixture", None, None)


def _git_output(repo_root: Path, arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise CodeVersionError(
            f"git {' '.join(arguments)} failed with exit {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def probe_git(repo_root: Path) -> tuple[str, list[str]]:
    """Return HEAD and the full porcelain-v1 status entry list, read-only."""
    head = _git_output(repo_root, ["rev-parse", "HEAD"]).strip()
    if _HEAD_PATTERN.match(head) is None:
        raise CodeVersionError("git HEAD is not a 40-character commit hash")
    status_text = _git_output(
        repo_root, ["status", "--porcelain=v1", "--untracked-files=all"]
    )
    entries = [line for line in status_text.splitlines() if line != ""]
    return head, entries


def _porcelain_path(text: str) -> str:
    if text.startswith('"') and text.endswith('"'):
        return json.loads(text)
    return text


def dirty_status_sha256(status_entries: Sequence[str], repo_root: Path) -> str:
    """Content-honest hash of every dirty path/status/content tuple.

    Rename entries contribute both paths in their porcelain-v1 order. The full
    tuple list is then canonically sorted before hashing. Missing/deleted paths
    contribute ``<deleted>`` instead of a content hash.
    """
    tuples: list[list[str]] = []
    for line in status_entries:
        if len(line) < 4:
            raise CodeVersionError(f"unrecognized porcelain entry: {line!r}")
        status = line[:2]
        payload = line[3:]
        paths = payload.split(" -> ") if " -> " in payload else [payload]
        for raw_path in paths:
            path_text = _porcelain_path(raw_path)
            path = repo_root / path_text
            if "D" in status or not path.is_file():
                file_hash = _DELETED_MARKER
            else:
                try:
                    file_hash = content_sha256(path.read_bytes())
                except OSError as exc:
                    raise CodeVersionError(f"cannot hash working-tree file {path_text!r}: {exc}") from exc
            tuples.append([path_text, status, file_hash])
    return hash_canonical(sorted(tuples))


def resolve_code_version(
    repo_root: Path | None = None,
    probe: Callable[[Path], tuple[str, list[str]]] | None = None,
) -> CodeVersion:
    """Resolve production git provenance, including an explicit dirty marker."""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    git_probe = probe_git if probe is None else probe
    head, entries = git_probe(root)
    if _HEAD_PATTERN.match(head) is None:
        raise CodeVersionError("git HEAD is not a 40-character commit hash")
    if not entries:
        return CodeVersion(head, "git_head", True, None)
    return CodeVersion(
        head,
        "git_head",
        False,
        dirty_status_sha256(entries, root),
    )


def compute_run_fingerprint(
    *,
    included_events: Sequence[dict[str, Any]],
    cutoff: str,
    price_list_sha256: str,
    plans_sha256: str,
    code_version: CodeVersion,
) -> str:
    """Content-addressed identity for one logical run and its code provenance."""
    event_rows = [
        {
            "provider": event["provider"],
            "account_id": event["account_id"],
            "provider_event_id": event["provider_event_id"],
            "record_version": event["record_version"],
            "payload_sha256": event["payload_sha256"],
            "arrival_time": event["arrival_time"],
            "usage_time": event["usage_time"],
        }
        for event in included_events
    ]
    # Dicts are not orderable; canonical JSON gives a deterministic total order.
    ordered_events = sorted(event_rows, key=canonical_json)
    events_hash = hash_canonical(ordered_events)
    fingerprint_input = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "aggregation_definition_id": AGGREGATION_DEFINITION_ID,
        "as_of_cutoff": cutoff,
        "code_version": code_version.code_version,
        "code_version_source": code_version.code_version_source,
        "working_tree_clean": code_version.working_tree_clean,
        "dirty_status_sha256": code_version.dirty_status_sha256,
        "price_list_sha256": price_list_sha256,
        "plans_sha256": plans_sha256,
        "events_sha256": events_hash,
    }
    return hash_canonical(fingerprint_input)


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def write_run_artifact(
    out_root: Path,
    fingerprint: str,
    files: dict[str, bytes],
) -> RunWriteResult:
    """Write a new run directory, or verify an exact replay without overwriting.

    ``files`` insertion order is the write order; the manifest is expected last.
    A pre-existing directory must contain exactly the same file names and bytes.
    Any missing, extra, or different file is a loud replay mismatch.
    """
    out_root = Path(out_root)
    run_dir = out_root / fingerprint
    if run_dir.exists():
        if not run_dir.is_dir():
            raise ReplayMismatchError(f"run path exists and is not a directory: {run_dir.name}")
        actual_names = sorted(path.name for path in run_dir.iterdir())
        if actual_names != sorted(files):
            raise ReplayMismatchError("existing run directory has a different file set")
        for name, expected in files.items():
            path = run_dir / name
            try:
                actual = path.read_bytes()
            except OSError as exc:
                raise ReplayMismatchError(f"cannot read existing run file {name}: {exc}") from exc
            if actual != expected:
                raise ReplayMismatchError(f"existing run file differs: {name}")
        return RunWriteResult(run_dir, True)

    try:
        out_root.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir()
        for name, data in files.items():
            _write_bytes(run_dir / name, data)
    except OSError as exc:
        raise OutputError(f"cannot write run artifact: {exc}") from exc
    return RunWriteResult(run_dir, False)
