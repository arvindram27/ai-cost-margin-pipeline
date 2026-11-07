"""Versioned synthetic event contract for Phase 1 (``schema_version "1.0"``).

Every event carries ``schema_version`` and the only version this module
understands is ``"1.0"``. A record that breaks one of these rules is
dead-lettered with a reason from the closed set below; it is never coerced
into a guessed shape and no field is ever defaulted to a value.

The contract describes *synthetic* fixtures. No real provider field set has
been verified and none is assumed here; the mapping to a real provider belongs
to the Phase 7 approval gate.

Unavailability is represented by ``null`` **plus** the field name in
``unavailable_fields``. That pair is what keeps "the provider did not tell us"
distinct from a real ``0``, which matters from Phase 2 onward: an unavailable
token count must make the cost unknown, never zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = [
    "ENVELOPE_FIELDS",
    "EVENT_FIELDS",
    "EVENT_FIELD_ORDER",
    "Issue",
    "SCHEMA_VERSION",
    "TOKEN_FIELDS",
    "WORKLOAD_TYPES",
    "validate_envelope",
    "validate_event",
]

SCHEMA_VERSION = "1.0"

TOKEN_FIELDS = ("tokens_input", "tokens_output", "tokens_cached")

WORKLOAD_TYPES = frozenset({"short_light", "long_heavy", "spiky", "cache_heavy"})

ENVELOPE_FIELDS = (
    "schema_version",
    "batch_id",
    "provider",
    "account_id",
    "arrival_time",
    "events",
)

# Order used when reporting missing fields, so messages are deterministic.
EVENT_FIELD_ORDER = (
    "schema_version",
    "provider_event_id",
    "attempt_id",
    "request_id",
    "model",
    "feature",
    "workload_type",
    "customer_id",
    "usage_time",
    "tokens_input",
    "tokens_output",
    "tokens_cached",
    "unavailable_fields",
    "record_version",
    "supersedes",
)

EVENT_FIELDS = frozenset(EVENT_FIELD_ORDER)

_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

_MAX_ID_LENGTH = 128


@dataclass(frozen=True)
class Issue:
    """One validation failure: a closed-set reason plus a human message."""

    reason: str
    message: str


def _is_int(value: Any) -> bool:
    """``bool`` is an ``int`` in Python; a token count must never be a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value != ""
        and len(value) <= _MAX_ID_LENGTH
        and not any(character.isspace() for character in value)
    )


def _is_valid_timestamp(value: Any) -> bool:
    """Strict UTC only: ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``, no offsets."""
    if not isinstance(value, str) or _TIMESTAMP_PATTERN.match(value) is None:
        return False
    has_fraction = "." in value
    layout = "%Y-%m-%dT%H:%M:%S" + (".%f" if has_fraction else "") + "Z"
    try:
        datetime.strptime(value, layout)
    except ValueError:
        return False
    return True


def validate_envelope(envelope: Any) -> list[Issue]:
    """Validate one batch envelope. Empty list means it may be processed."""
    if not isinstance(envelope, dict):
        return [Issue("invalid_envelope", "batch must be a JSON object")]

    issues: list[Issue] = []
    for name in sorted(set(envelope) - set(ENVELOPE_FIELDS)):
        issues.append(Issue("unknown_field", f"unknown batch field {name!r}"))
    for name in ENVELOPE_FIELDS:
        if name != "events" and name not in envelope:
            issues.append(Issue("invalid_envelope", f"missing required batch field {name!r}"))
    if issues:
        return issues

    if envelope["schema_version"] != SCHEMA_VERSION:
        issues.append(
            Issue(
                "invalid_envelope",
                f"batch schema_version must be {SCHEMA_VERSION!r}, got {envelope['schema_version']!r}",
            )
        )
    for name in ("batch_id", "provider", "account_id"):
        if not _is_valid_id(envelope[name]):
            issues.append(Issue("invalid_envelope", f"{name} must be a non-empty string without whitespace"))
    if not _is_valid_timestamp(envelope["arrival_time"]):
        issues.append(Issue("invalid_envelope", "arrival_time must be an ISO-8601 UTC timestamp ending in 'Z'"))

    if "events" not in envelope:
        issues.append(Issue("missing_events", "batch is missing 'events'"))
    elif not isinstance(envelope["events"], list):
        issues.append(Issue("missing_events", "'events' must be a list"))
    return issues


def validate_event(event: Any) -> list[Issue]:
    """Validate one event. Empty list means it may be stored.

    Checks run in a fixed order so that a deliberately single-fault record
    reports exactly one reason.
    """
    if not isinstance(event, dict):
        return [Issue("payload_not_object", "event must be a JSON object")]

    if event.get("schema_version") != SCHEMA_VERSION:
        return [
            Issue(
                "unknown_schema_version",
                f"schema_version must be {SCHEMA_VERSION!r}, got {event.get('schema_version')!r}",
            )
        ]

    issues: list[Issue] = []
    for name in sorted(set(event) - EVENT_FIELDS):
        issues.append(Issue("unknown_field", f"unknown event field {name!r}"))
    for name in EVENT_FIELD_ORDER:
        if name not in event:
            issues.append(Issue("missing_required_field", f"missing required event field {name!r}"))
    if issues:
        return issues

    for name in ("provider_event_id", "attempt_id", "request_id", "model", "feature"):
        if not _is_valid_id(event[name]):
            issues.append(Issue("wrong_type", f"{name} must be a non-empty string without whitespace"))

    workload = event["workload_type"]
    if not isinstance(workload, str):
        issues.append(Issue("wrong_type", "workload_type must be a string"))
    elif workload not in WORKLOAD_TYPES:
        issues.append(
            Issue("unknown_enum_value", f"workload_type must be one of {sorted(WORKLOAD_TYPES)}")
        )

    customer = event["customer_id"]
    if customer is not None and not _is_valid_id(customer):
        issues.append(Issue("wrong_type", "customer_id must be null or a non-empty string without whitespace"))

    if not _is_valid_timestamp(event["usage_time"]):
        issues.append(Issue("invalid_timestamp", "usage_time must be an ISO-8601 UTC timestamp ending in 'Z'"))

    for name in TOKEN_FIELDS:
        value = event[name]
        if value is None:
            continue
        if not _is_int(value):
            issues.append(Issue("wrong_type", f"{name} must be an integer or null"))
        elif value < 0:
            issues.append(Issue("negative_token_count", f"{name} must be >= 0"))

    unavailable = event["unavailable_fields"]
    listed: set[str] | None = None
    if not isinstance(unavailable, list):
        issues.append(Issue("wrong_type", "unavailable_fields must be a list"))
    else:
        if any(not isinstance(name, str) for name in unavailable):
            issues.append(Issue("wrong_type", "unavailable_fields entries must be strings"))
        for name in unavailable:
            if isinstance(name, str) and name not in TOKEN_FIELDS:
                issues.append(
                    Issue(
                        "unavailable_marker_inconsistent",
                        f"unavailable_fields entry {name!r} is not a token field",
                    )
                )
        listed = {name for name in unavailable if isinstance(name, str)}
        if len(unavailable) != len(listed):
            issues.append(Issue("unavailable_marker_inconsistent", "unavailable_fields contains duplicates"))

    record_version = event["record_version"]
    if not _is_int(record_version) or record_version < 1:
        issues.append(Issue("invalid_record_version", "record_version must be an integer >= 1"))

    supersedes = event["supersedes"]
    if supersedes is not None and not _is_valid_id(supersedes):
        issues.append(Issue("wrong_type", "supersedes must be null or a non-empty event id"))

    if listed is not None:
        for name in TOKEN_FIELDS:
            is_null = event[name] is None
            if is_null and name not in listed:
                issues.append(
                    Issue(
                        "unavailable_marker_inconsistent",
                        f"{name} is null but not listed in unavailable_fields",
                    )
                )
            elif not is_null and name in listed:
                issues.append(
                    Issue(
                        "unavailable_marker_inconsistent",
                        f"{name} is listed in unavailable_fields but has a value",
                    )
                )
    return issues
