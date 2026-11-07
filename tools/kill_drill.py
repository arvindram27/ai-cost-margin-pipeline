#!/usr/bin/env python
"""Phase 4 kill drill: three audited kill points, captured as JSON evidence.

Standard library only. The harness never imports the package; it drives the
installed CLI through ``sys.executable -m ai_cost_margin.pipeline`` in
subprocesses and observes the DuckDB database through the runner's own
``--status-json`` subcommand. Each run uses a fresh root under ``--root``; the
root is created once and never deleted.

Evidence produced, per kill point:

* the kill exit code (137, the audited ``os._exit``);
* the interrupted attempt row and its ``last_completed_stage``;
* the absence of partial stage rows for the killed stage;
* the resumed run's digest and all seven published files, byte-compared with an
  uninterrupted clean run produced in a sibling fresh root;
* for points 1 and 2, digest equality with the pinned golden digest;
* for point 3, the published pointer still naming the earlier X attempt during
  the kill window, and Y's resumed digest/files identical to an uninterrupted Y
  clean run while Y's digest differs from X's.

Exit code is 0 only on a full PASS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"
CUTOFF_X = "2026-11-01T23:59:59Z"
# Every Phase 1 arrival_time is exactly 2026-11-01T00:00:00Z, so this earlier
# cutoff excludes all 19 stored events and gives Y a different fingerprint.
CUTOFF_Y = "2026-10-31T23:59:59Z"
Y_CUTOFF_REASON = (
    "every Phase 1 arrival_time is 2026-11-01T00:00:00Z, so this cutoff "
    "excludes all stored events"
)
PIPELINE_VERSION = "0.4.0"
SEVEN_FILES = (
    "customer-margins.jsonl",
    "workload-margins.jsonl",
    "unallocated.jsonl",
    "lineage.jsonl",
    "margin-report.json",
    "margin-digest.txt",
    "run-manifest.json",
)
KILL_POINTS = ("before_raw_commit", "before_costed_commit", "after_publish_rename")
EXPECTED_LAST_COMPLETED = {
    "before_raw_commit": None,
    "before_costed_commit": "validated",
    "after_publish_rename": "aggregated",
}


class DrillError(RuntimeError):
    """A drill precondition or assertion failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seven_file_hashes(run_dir: Path) -> dict[str, str]:
    """SHA-256 of every published file, failing loudly if one is missing."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise DrillError(f"published run directory is missing: {run_dir}")
    names = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    if names != sorted(SEVEN_FILES):
        raise DrillError(
            f"published run directory has the wrong file set: {names!r}"
        )
    return {name: sha256_file(run_dir / name) for name in SEVEN_FILES}


def compare_seven_files(left_dir: Path, right_dir: Path) -> dict[str, dict[str, Any]]:
    """Per-file byte equality, with both hashes recorded."""
    left = seven_file_hashes(left_dir)
    right = seven_file_hashes(right_dir)
    return {
        name: {
            "left_sha256": left[name],
            "right_sha256": right[name],
            "equal": left[name] == right[name],
        }
        for name in SEVEN_FILES
    }


def _env(repo: Path, kill_point: str | None) -> dict[str, str]:
    env = dict(os.environ)
    src = str(repo / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    env.pop("AI_COST_MARGIN_KILL_POINT", None)
    if kill_point is not None:
        env["AI_COST_MARGIN_KILL_POINT"] = kill_point
    return env


def run_cli(
    repo: Path,
    args: Sequence[str],
    *,
    kill_point: str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run the pipeline CLI with the current interpreter, never the package."""
    command = [sys.executable, "-m", "ai_cost_margin.pipeline", *args]
    try:
        return subprocess.run(
            command,
            cwd=str(repo),
            env=_env(repo, kill_point),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except OSError as exc:  # pragma: no cover - environment failure
        raise DrillError(f"cannot start the pipeline CLI: {exc}") from exc


def parse_run_result(stdout: str) -> dict[str, Any] | None:
    for line in stdout.splitlines():
        if line.startswith("run_result "):
            return json.loads(line[len("run_result ") :])
    return None


def _pipeline_args(
    repo: Path, home: Path, out: Path, cutoff: str
) -> list[str]:
    phase1 = repo / "fixtures" / "phase1"
    return [
        "--home",
        str(home),
        "--batches",
        str(phase1),
        "--batches",
        str(phase1 / "batch-001-clean.json"),
        "--prices",
        str(repo / "fixtures" / "phase2" / "price-list-synthetic-v1.json"),
        "--plans",
        str(repo / "fixtures" / "phase3" / "customer-plans-simulated-v1.json"),
        "--cutoff",
        cutoff,
        "--out",
        str(out),
    ]


def run_pipeline(
    repo: Path, home: Path, out: Path, cutoff: str, *, kill_point: str | None = None
) -> dict[str, Any]:
    """Run one pipeline invocation and return the parsed run result."""
    completed = run_cli(
        repo, _pipeline_args(repo, home, out, cutoff), kill_point=kill_point
    )
    if completed.returncode != 0:
        raise DrillError(
            f"pipeline failed (exit {completed.returncode}) for {cutoff}: "
            f"{completed.stderr.strip()}"
        )
    result = parse_run_result(completed.stdout)
    if result is None:
        raise DrillError("pipeline printed no run_result line")
    return result


def run_until_killed(
    repo: Path, home: Path, out: Path, cutoff: str, kill_point: str
) -> int:
    completed = run_cli(
        repo, _pipeline_args(repo, home, out, cutoff), kill_point=kill_point
    )
    return completed.returncode


def status(repo: Path, home: Path) -> dict[str, Any]:
    completed = run_cli(repo, ["--home", str(home), "--status-json"])
    if completed.returncode != 0:
        raise DrillError(
            f"status read failed (exit {completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DrillError(f"status output is not JSON: {exc}") from exc


def ensure_fresh_root(root: Path) -> Path:
    root = Path(root)
    if root.exists():
        raise DrillError(f"drill root already exists and is never overwritten: {root}")
    root.mkdir(parents=True, exist_ok=False)
    return root


def _attempts_with(status_payload: dict[str, Any], attempt_status: str) -> list[dict[str, Any]]:
    return [
        attempt
        for attempt in status_payload.get("attempts", [])
        if attempt.get("status") == attempt_status
    ]


def _record_point_common(
    *,
    repo: Path,
    base: Path,
    cutoff: str,
    kill_point: str,
    clean_dir: Path,
    clean_digest: str,
    need_exact_replay: bool,
) -> dict[str, Any]:
    home = base / "home"
    out = base / "runs"
    kill_exit = run_until_killed(repo, home, out, cutoff, kill_point)
    if kill_exit != 137:
        raise DrillError(f"{kill_point}: kill exit was {kill_exit}, expected 137")

    resumed = run_pipeline(repo, home, out, cutoff)
    resumed_dir = out / resumed["run_dir_name"]
    files = compare_seven_files(clean_dir, resumed_dir)

    payload = status(repo, home)
    interrupted = _attempts_with(payload, "interrupted")
    if len(interrupted) != 1:
        raise DrillError(
            f"{kill_point}: expected exactly one interrupted attempt, got {len(interrupted)}"
        )
    row = interrupted[0]
    expected_stage = EXPECTED_LAST_COMPLETED[kill_point]
    if row["last_completed_stage"] != expected_stage:
        raise DrillError(
            f"{kill_point}: last_completed_stage {row['last_completed_stage']!r} "
            f"!= {expected_stage!r}"
        )
    stages = row.get("stages", {})
    if kill_point == "before_raw_commit":
        if "raw" in stages:
            raise DrillError("before_raw_commit: partial raw stage row survived")
    elif kill_point == "before_costed_commit":
        if "costed" in stages or "aggregated" in stages:
            raise DrillError("before_costed_commit: partial costed stage row survived")
        if stages.get("raw", {}).get("status") != "committed":
            raise DrillError("before_costed_commit: raw stage was not committed")
        if stages.get("validated", {}).get("status") != "committed":
            raise DrillError("before_costed_commit: validated stage was not committed")

    if resumed["digest"] != clean_digest:
        raise DrillError(
            f"{kill_point}: resumed digest {resumed['digest']} != clean {clean_digest}"
        )
    if clean_digest != GOLDEN_DIGEST:
        raise DrillError(
            f"{kill_point}: clean digest {clean_digest} != golden {GOLDEN_DIGEST}"
        )
    if not all(item["equal"] for item in files.values()):
        raise DrillError(f"{kill_point}: resumed files differ from the clean run")
    if need_exact_replay and resumed.get("replayed") is not True:
        raise DrillError(f"{kill_point}: resumed run did not take the exact-replay path")

    return {
        "kill_point": kill_point,
        "kill_exit": kill_exit,
        "interrupted_attempt_id": row["attempt_id"],
        "interrupted_last_completed_stage": row["last_completed_stage"],
        "interrupted_committed_stages": sorted(stages),
        "resumed_attempt_id": resumed["attempt_id"],
        "resumed_digest": resumed["digest"],
        "clean_digest": clean_digest,
        "golden_digest": GOLDEN_DIGEST,
        "golden_equal": clean_digest == GOLDEN_DIGEST
        and resumed["digest"] == GOLDEN_DIGEST,
        "resumed_replayed": resumed.get("replayed"),
        "files": files,
        "files_all_equal": all(item["equal"] for item in files.values()),
    }


def drill(repo: Path, root: Path) -> dict[str, Any]:
    """Run the whole drill and return the evidence dictionary."""
    repo = Path(repo).resolve()
    root = ensure_fresh_root(root)

    evidence: dict[str, Any] = {
        "drill": "phase4-kill-drill",
        "pipeline_version": PIPELINE_VERSION,
        "golden_digest": GOLDEN_DIGEST,
        "cutoff_x": CUTOFF_X,
        "cutoff_y": CUTOFF_Y,
        "y_cutoff_reason": Y_CUTOFF_REASON,
        "points": {},
    }

    # X clean: the uninterrupted reference run.
    clean_base = root / "x-clean"
    x_clean = run_pipeline(repo, clean_base / "home", clean_base / "runs", CUTOFF_X)
    x_clean_dir = clean_base / "runs" / x_clean["run_dir_name"]
    x_clean_hashes = seven_file_hashes(x_clean_dir)
    evidence["x_clean"] = {
        "attempt_id": x_clean["attempt_id"],
        "run_fingerprint": x_clean["run_fingerprint"],
        "digest": x_clean["digest"],
        "golden_equal": x_clean["digest"] == GOLDEN_DIGEST,
        "files": x_clean_hashes,
    }
    if x_clean["digest"] != GOLDEN_DIGEST:
        raise DrillError(
            f"X clean digest {x_clean['digest']} != golden {GOLDEN_DIGEST}"
        )

    # Points 1 and 2: kill, resume, compare to the clean X run.
    for kill_point in ("before_raw_commit", "before_costed_commit"):
        base = root / f"x-{kill_point}"
        evidence["points"][kill_point] = _record_point_common(
            repo=repo,
            base=base,
            cutoff=CUTOFF_X,
            kill_point=kill_point,
            clean_dir=x_clean_dir,
            clean_digest=x_clean["digest"],
            need_exact_replay=False,
        )

    # Point 3: publish X first so the pointer has a meaningful previous run.
    base = root / "y-publish"
    home = base / "home"
    out = base / "runs"
    x_run = run_pipeline(repo, home, out, CUTOFF_X)
    before = status(repo, home)
    if before.get("published") is None or before["published"]["attempt_id"] != x_run["attempt_id"]:
        raise DrillError("point 3: X was not the published run before Y started")

    kill_exit = run_until_killed(repo, home, out, CUTOFF_Y, "after_publish_rename")
    if kill_exit != 137:
        raise DrillError(f"after_publish_rename: kill exit was {kill_exit}, expected 137")
    during = status(repo, home)
    pointer = during.get("published")
    pointer_names_x = pointer is not None and pointer["attempt_id"] == x_run["attempt_id"]
    if not pointer_names_x:
        raise DrillError(
            "after_publish_rename: published pointer no longer names the X attempt"
        )
    running = _attempts_with(during, "running")
    if len(running) != 1:
        raise DrillError(
            f"after_publish_rename: expected one running Y attempt, got {len(running)}"
        )
    y_running = running[0]
    if y_running["last_completed_stage"] != "aggregated":
        raise DrillError(
            "after_publish_rename: Y last_completed_stage is "
            f"{y_running['last_completed_stage']!r}, expected 'aggregated'"
        )
    if "artifacts" in y_running.get("stages", {}) and not all(
        stage in y_running["stages"]
        for stage in ("raw", "validated", "costed", "aggregated")
    ):
        raise DrillError("after_publish_rename: Y is missing a committed stage")

    y_resumed = run_pipeline(repo, home, out, CUTOFF_Y)
    y_resumed_dir = out / y_resumed["run_dir_name"]

    y_clean_base = root / "y-clean"
    y_clean = run_pipeline(
        repo, y_clean_base / "home", y_clean_base / "runs", CUTOFF_Y
    )
    y_clean_dir = y_clean_base / "runs" / y_clean["run_dir_name"]
    files = compare_seven_files(y_clean_dir, y_resumed_dir)

    after = status(repo, home)
    interrupted = _attempts_with(after, "interrupted")
    if len(interrupted) != 1:
        raise DrillError(
            f"after_publish_rename: expected one interrupted attempt, got {len(interrupted)}"
        )
    y_row = interrupted[0]
    if y_row["last_completed_stage"] != "aggregated":
        raise DrillError(
            "after_publish_rename: interrupted Y last_completed_stage is "
            f"{y_row['last_completed_stage']!r}, expected 'aggregated'"
        )
    if y_resumed["digest"] != y_clean["digest"]:
        raise DrillError(
            f"after_publish_rename: Y resumed digest {y_resumed['digest']} "
            f"!= Y clean digest {y_clean['digest']}"
        )
    if y_resumed["digest"] == x_clean["digest"]:
        raise DrillError("after_publish_rename: Y digest equals X digest; cutoff did not differ")
    if not all(item["equal"] for item in files.values()):
        raise DrillError("after_publish_rename: Y resumed files differ from Y clean")
    if y_resumed.get("replayed") is not True:
        raise DrillError("after_publish_rename: Y resumed run did not take the exact-replay path")
    final_pointer = after.get("published")
    if final_pointer is None or final_pointer["attempt_id"] != y_resumed["attempt_id"]:
        raise DrillError("after_publish_rename: Y was not published after the resume")

    evidence["points"]["after_publish_rename"] = {
        "kill_point": "after_publish_rename",
        "kill_exit": kill_exit,
        "x_attempt_id": x_run["attempt_id"],
        "x_digest": x_run["digest"],
        "pointer_during_kill_names_x": pointer_names_x,
        "pointer_during_kill_attempt_id": pointer["attempt_id"] if pointer else None,
        "y_running_attempt_id": y_running["attempt_id"],
        "y_running_last_completed_stage": y_running["last_completed_stage"],
        "interrupted_attempt_id": y_row["attempt_id"],
        "interrupted_last_completed_stage": y_row["last_completed_stage"],
        "interrupted_committed_stages": sorted(y_row.get("stages", {})),
        "y_resumed_attempt_id": y_resumed["attempt_id"],
        "y_resumed_digest": y_resumed["digest"],
        "y_clean_digest": y_clean["digest"],
        "y_digest_differs_from_x": y_resumed["digest"] != x_clean["digest"],
        "y_resumed_replayed": y_resumed.get("replayed"),
        "pointer_after_resume_attempt_id": final_pointer["attempt_id"],
        "files": files,
        "files_all_equal": all(item["equal"] for item in files.values()),
    }
    evidence["all_points_ok"] = all(
        point.get("files_all_equal")
        for point in evidence["points"].values()
        if "files_all_equal" in point
    )
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kill_drill.py",
        description="Phase 4 kill-point drill with JSON evidence.",
    )
    parser.add_argument("--root", required=True, type=Path, help="fresh drill root")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: the parent of tools/)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = drill(args.repo, args.root)
    except DrillError as exc:
        print(f"DRILL FAIL: {exc}", file=sys.stderr)
        return 1
    evidence_path = Path(args.root) / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    print(f"evidence written to {evidence_path}")
    print("DRILL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
