"""Seeded generator for the committed Phase 1 synthetic fixtures.

The generator is the provenance of the committed fixture files: running it must
reproduce them byte for byte (see ``tests/test_fixture_generator.py``). Bump
``FIXTURE_SEED`` only together with a fully regenerated, re-reviewed fixture
set.

Everything here is synthetic. No live provider call, no recorded traffic and no
real customer data is involved.

Usage (from the repo root, with ``src`` on ``PYTHONPATH``)::

    $env:PYTHONPATH = "$PWD\\src"
    .venv\\Scripts\\python.exe -m ai_cost_margin.fixtures --out fixtures\\phase1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from random import Random

from .schema import SCHEMA_VERSION
from .store import pretty_json

__all__ = ["FIXTURE_SEED", "build_fixture_set", "main", "write_fixture_set"]

FIXTURE_SEED = 20261107

PROVIDER = "synthetic-token-provider"
ACCOUNT = "acct-demo"
ARRIVAL_TIME = "2026-11-01T00:00:00Z"
FEATURE = "demo-assistant"
WORKLOADS = ("short_light", "long_heavy", "spiky", "cache_heavy")
CUSTOMERS = tuple(f"cust-{index:03d}" for index in range(1, 7))
MODELS = ("synth-small", "synth-large")

# Deliberately unparseable: the file-level dead-letter path is a real path and
# needs a fixture that exercises it.
INVALID_JSON_TEXT = '{"batch_id": "batch-007", "events": [ }\n'


def _usage_time(index: int) -> str:
    day = 1 + (index * 2) % 28
    hour = 8 + index % 10
    minute = (index * 7) % 60
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def _event(
    *,
    event_id: str,
    request_id: str,
    customer_id: str | None,
    workload: str,
    model: str,
    usage_time: str,
    tokens_input: int | None,
    tokens_output: int | None,
    tokens_cached: int | None,
    unavailable: tuple[str, ...] = (),
    attempt_id: str = "1",
    record_version: int = 1,
    supersedes: str | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider_event_id": event_id,
        "attempt_id": attempt_id,
        "request_id": request_id,
        "model": model,
        "feature": FEATURE,
        "workload_type": workload,
        "customer_id": customer_id,
        "usage_time": usage_time,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_cached": tokens_cached,
        "unavailable_fields": list(unavailable),
        "record_version": record_version,
        "supersedes": supersedes,
    }


def _batch(batch_id: str, events: list[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "provider": PROVIDER,
        "account_id": ACCOUNT,
        "arrival_time": ARRIVAL_TIME,
        "events": events,
    }


def _render(batch: dict) -> str:
    return pretty_json(batch) + "\n"


def _build_clean_events(rng: Random) -> list[dict]:
    events: list[dict] = []
    for index in range(12):
        events.append(
            _event(
                event_id=f"evt-001-{index + 1:03d}",
                request_id=f"req-001-{index + 1:03d}",
                customer_id=CUSTOMERS[index % len(CUSTOMERS)],
                workload=WORKLOADS[index % len(WORKLOADS)],
                model=MODELS[index % 2],
                usage_time=_usage_time(index),
                tokens_input=rng.randint(200, 6000),
                tokens_output=rng.randint(50, 1500),
                tokens_cached=rng.randint(0, 400),
            )
        )
    # Index 4: the provider did not expose cached tokens. Null plus the marker,
    # never a zero.
    events[4]["tokens_cached"] = None
    events[4]["unavailable_fields"] = ["tokens_cached"]
    # Index 7: a real, billable zero that must not be read as unavailable.
    events[7]["tokens_cached"] = 0
    # Index 9: explicitly unattributable usage. Phase 3 sends it to the
    # unallocated bucket; the ingestion layer neither guesses nor drops it.
    events[9]["customer_id"] = None
    return events


def _build_retry_events() -> list[dict]:
    first = _event(
        event_id="evt-002-001",
        request_id="req-002-retry",
        customer_id="cust-001",
        workload="long_heavy",
        model="synth-large",
        usage_time="2026-10-09T10:00:00Z",
        tokens_input=3200,
        tokens_output=900,
        tokens_cached=0,
        attempt_id="1",
    )
    second = _event(
        event_id="evt-002-002",
        request_id="req-002-retry",
        customer_id="cust-001",
        workload="long_heavy",
        model="synth-large",
        usage_time="2026-10-09T10:05:00Z",
        tokens_input=3200,
        tokens_output=940,
        tokens_cached=0,
        attempt_id="2",
    )
    # Same event delivered twice inside one file: same key, same payload.
    duplicate = json.loads(json.dumps(first))
    return [first, second, duplicate]


def _build_changed_payload_events(clean: list[dict]) -> list[dict]:
    # Same key, same record_version, changed payload, no correction reference.
    changed = json.loads(json.dumps(clean[0]))
    changed["tokens_input"] = clean[0]["tokens_input"] + 1
    # Same key, same record_version, changed payload, and a self-referencing
    # supersedes. Amendment A1: still not a correction, so still quarantined.
    self_reference = json.loads(json.dumps(clean[1]))
    self_reference["tokens_input"] = clean[1]["tokens_input"] + 1
    self_reference["supersedes"] = self_reference["provider_event_id"]
    return [changed, self_reference]


def _build_correction_events(clean: list[dict]) -> list[dict]:
    # Accepted path one: same key, explicit record_version bump.
    bumped = json.loads(json.dumps(clean[2]))
    bumped["tokens_input"] = clean[2]["tokens_input"] + 10
    bumped["record_version"] = 2
    # Accepted path two: a new provider_event_id carrying an explicit
    # supersedes reference to an event that already exists.
    reissued = _event(
        event_id="evt-004-001",
        request_id="req-004-001",
        customer_id=clean[4]["customer_id"],
        workload="short_light",
        model="synth-small",
        usage_time="2026-10-21T12:00:00Z",
        tokens_input=800,
        tokens_output=120,
        tokens_cached=0,
        supersedes=clean[4]["provider_event_id"],
    )
    return [bumped, reissued]


def _build_malformed_events(clean: list[dict]) -> list[dict]:
    """Nine single-fault records; the expected reason mapping is asserted in
    ``tests/test_dead_letter.py`` and documented in the ingestion contract."""
    template = json.loads(json.dumps(clean[10]))

    def variant(event_id: str) -> dict:
        event = json.loads(json.dumps(template))
        event["provider_event_id"] = event_id
        event["request_id"] = f"req-005-{event_id[-3:]}"
        return event

    missing_field = variant("evt-005-001")
    del missing_field["feature"]

    unknown_field = variant("evt-005-002")
    unknown_field["prompt"] = "synthetic text that must never be needed"

    wrong_type = variant("evt-005-003")
    wrong_type["tokens_input"] = "1200"

    bool_token = variant("evt-005-004")
    bool_token["tokens_input"] = True

    negative = variant("evt-005-005")
    negative["tokens_input"] = -5

    bad_timestamp = variant("evt-005-006")
    bad_timestamp["usage_time"] = "2026-10-05T10:00:00"

    bad_version = variant("evt-005-007")
    bad_version["schema_version"] = "2.0"

    null_not_listed = variant("evt-005-008")
    null_not_listed["tokens_cached"] = None

    listed_not_null = variant("evt-005-009")
    listed_not_null["unavailable_fields"] = ["tokens_cached"]

    return [
        missing_field,
        unknown_field,
        wrong_type,
        bool_token,
        negative,
        bad_timestamp,
        bad_version,
        null_not_listed,
        listed_not_null,
    ]


def _build_unavailable_events() -> list[dict]:
    all_unavailable = _event(
        event_id="evt-006-001",
        request_id="req-006-001",
        customer_id="cust-002",
        workload="spiky",
        model="synth-large",
        usage_time="2026-10-23T09:30:00Z",
        tokens_input=None,
        tokens_output=None,
        tokens_cached=None,
        unavailable=("tokens_input", "tokens_output", "tokens_cached"),
    )
    partial = _event(
        event_id="evt-006-002",
        request_id="req-006-002",
        customer_id="cust-003",
        workload="cache_heavy",
        model="synth-small",
        usage_time="2026-10-23T11:15:00Z",
        tokens_input=None,
        tokens_output=None,
        tokens_cached=64,
        unavailable=("tokens_input", "tokens_output"),
    )
    zeros = _event(
        event_id="evt-006-003",
        request_id="req-006-003",
        customer_id="cust-004",
        workload="short_light",
        model="synth-small",
        usage_time="2026-10-23T13:45:00Z",
        tokens_input=0,
        tokens_output=0,
        tokens_cached=0,
    )
    return [all_unavailable, partial, zeros]


def build_fixture_set(seed: int = FIXTURE_SEED) -> dict[str, str]:
    """Return ``{file name: file text}`` for the whole committed fixture set."""
    rng = Random(seed)
    clean = _build_clean_events(rng)
    return {
        "batch-001-clean.json": _render(_batch("batch-001", clean)),
        "batch-002-retries.json": _render(_batch("batch-002", _build_retry_events())),
        "batch-003-changed-payload.json": _render(
            _batch("batch-003", _build_changed_payload_events(clean))
        ),
        "batch-004-correction.json": _render(
            _batch("batch-004", _build_correction_events(clean))
        ),
        "batch-005-malformed.json": _render(
            _batch("batch-005", _build_malformed_events(clean))
        ),
        "batch-006-unavailable-fields.json": _render(
            _batch("batch-006", _build_unavailable_events())
        ),
        "batch-007-invalid-json.json": INVALID_JSON_TEXT,
    }


def write_fixture_set(out_dir: Path, seed: int = FIXTURE_SEED) -> list[Path]:
    """Write the fixture set into ``out_dir`` (created if needed)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in build_fixture_set(seed).items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_cost_margin.fixtures",
        description="Write the seeded Phase 1 synthetic fixture set.",
    )
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--seed", type=int, default=FIXTURE_SEED, help="PRNG seed")
    args = parser.parse_args(argv)

    written = write_fixture_set(args.out, args.seed)
    for path in written:
        print(f"wrote {path}")
    print(f"seed {args.seed}: {len(written)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
