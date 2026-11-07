"""Unit tests for the schema_version "1.0" synthetic event contract."""

from __future__ import annotations

from ai_cost_margin.schema import SCHEMA_VERSION, validate_envelope, validate_event

_DELETE = object()


def _event(**overrides):
    event = {
        "schema_version": SCHEMA_VERSION,
        "provider_event_id": "evt-001",
        "attempt_id": "1",
        "request_id": "req-001",
        "model": "synth-small",
        "feature": "demo-assistant",
        "workload_type": "short_light",
        "customer_id": "cust-001",
        "usage_time": "2026-10-01T08:00:00Z",
        "tokens_input": 100,
        "tokens_output": 20,
        "tokens_cached": 0,
        "unavailable_fields": [],
        "record_version": 1,
        "supersedes": None,
    }
    for key, value in overrides.items():
        if value is _DELETE:
            event.pop(key, None)
        else:
            event[key] = value
    return event


def _envelope(**overrides):
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": "batch-001",
        "provider": "synthetic-token-provider",
        "account_id": "acct-demo",
        "arrival_time": "2026-11-01T00:00:00Z",
        "events": [],
    }
    for key, value in overrides.items():
        if value is _DELETE:
            envelope.pop(key, None)
        else:
            envelope[key] = value
    return envelope


def _reasons(issues):
    return [issue.reason for issue in issues]


def test_valid_event_passes():
    assert validate_event(_event()) == []


def test_explicit_zero_tokens_is_valid_and_not_unavailable():
    event = _event(tokens_input=0, tokens_output=0, tokens_cached=0, unavailable_fields=[])
    assert validate_event(event) == []


def test_unavailable_tokens_are_explicit():
    event = _event(tokens_cached=None, unavailable_fields=["tokens_cached"])
    assert validate_event(event) == []


def test_unattributable_customer_is_valid():
    assert validate_event(_event(customer_id=None)) == []


def test_fractional_seconds_timestamp_is_valid():
    assert validate_event(_event(usage_time="2026-10-01T08:00:00.123456Z")) == []


def test_payload_not_object():
    assert _reasons(validate_event([])) == ["payload_not_object"]
    assert _reasons(validate_event("nope")) == ["payload_not_object"]


def test_missing_required_field():
    assert _reasons(validate_event(_event(feature=_DELETE))) == ["missing_required_field"]


def test_unknown_field():
    assert _reasons(validate_event(_event(prompt="hello"))) == ["unknown_field"]


def test_wrong_type_string_token():
    assert _reasons(validate_event(_event(tokens_input="1200"))) == ["wrong_type"]


def test_bool_is_not_an_integer_token_count():
    assert _reasons(validate_event(_event(tokens_input=True))) == ["wrong_type"]


def test_negative_token_count():
    assert _reasons(validate_event(_event(tokens_output=-1))) == ["negative_token_count"]


def test_timestamp_without_z():
    assert _reasons(validate_event(_event(usage_time="2026-10-05T10:00:00"))) == ["invalid_timestamp"]


def test_timestamp_with_offset_is_rejected():
    assert _reasons(validate_event(_event(usage_time="2026-10-05T10:00:00+05:30"))) == [
        "invalid_timestamp"
    ]


def test_unknown_schema_version_short_circuits():
    assert _reasons(validate_event(_event(schema_version="2.0"))) == ["unknown_schema_version"]


def test_unknown_workload_type():
    assert _reasons(validate_event(_event(workload_type="gigantic"))) == ["unknown_enum_value"]


def test_null_token_not_listed_is_inconsistent():
    assert _reasons(validate_event(_event(tokens_cached=None))) == [
        "unavailable_marker_inconsistent"
    ]


def test_listed_field_with_value_is_inconsistent():
    assert _reasons(validate_event(_event(unavailable_fields=["tokens_cached"]))) == [
        "unavailable_marker_inconsistent"
    ]


def test_record_version_zero():
    assert _reasons(validate_event(_event(record_version=0))) == ["invalid_record_version"]


def test_valid_envelope():
    assert validate_envelope(_envelope()) == []


def test_envelope_missing_events():
    assert _reasons(validate_envelope(_envelope(events=_DELETE))) == ["missing_events"]


def test_envelope_events_must_be_a_list():
    assert _reasons(validate_envelope(_envelope(events={}))) == ["missing_events"]


def test_envelope_unknown_field():
    assert "unknown_field" in _reasons(validate_envelope(_envelope(source_system="x")))


def test_envelope_missing_required_field():
    assert _reasons(validate_envelope(_envelope(provider=_DELETE))) == ["invalid_envelope"]


def test_envelope_bad_arrival_time():
    assert _reasons(validate_envelope(_envelope(arrival_time="2026-11-01"))) == ["invalid_envelope"]
