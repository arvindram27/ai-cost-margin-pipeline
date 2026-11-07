"""Price-list validation: every class-A rule is fatal, and only the right rule fires."""

from __future__ import annotations

import pytest

from ai_cost_margin.pricing import PriceScheduleError, parse_price_list

RATES = {"cached": "0.150000", "input": "0.750000", "output": "2.250000"}
RATES_LATER = {"cached": "0.187500", "input": "0.937500", "output": "2.812500"}


def _entry(
    version_id: str = "v1",
    *,
    provider: str = "p",
    model: str = "m",
    currency: str = "USD",
    effective_from: str = "2026-10-01T00:00:00Z",
    effective_to: str | None = "2026-10-16T00:00:00Z",
    rates: dict | None = None,
) -> dict:
    return {
        "currency": currency,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "model": model,
        "price_version_id": version_id,
        "provider": provider,
        "rates_per_million": dict(rates if rates is not None else RATES),
    }


def _document(entries: list[dict] | None = None, **overrides) -> dict:
    document = {
        "currency": "USD",
        "entries": entries
        if entries is not None
        else [
            _entry("v1", effective_to="2026-10-16T00:00:00Z", rates=RATES),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ],
        "label": "synthetic",
        "notes": "synthetic test prices",
        "price_list_id": "test-list",
        "rate_unit": "per_1_000_000_tokens",
        "timezone": "UTC",
    }
    document.update(overrides)
    return document


def _violations(entries: list[dict] | None = None, **overrides) -> list[str]:
    with pytest.raises(PriceScheduleError) as caught:
        parse_price_list(_document(entries, **overrides))
    return list(caught.value.violations)


def test_valid_list_loads():
    price_list = parse_price_list(_document())
    assert price_list.price_list_id == "test-list"
    assert price_list.label == "synthetic"
    assert price_list.currency == "USD"
    assert price_list.knows("p", "m")
    assert not price_list.knows("other", "m")
    for unit in ("input", "output", "cached"):
        assert ("p", "m", unit) in price_list.series


def test_internal_gap_is_fatal():
    violations = _violations(
        [
            _entry("v1", effective_to="2026-10-10T00:00:00Z"),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("gap between" in violation for violation in violations)


def test_overlap_is_fatal():
    violations = _violations(
        [
            _entry("v1", effective_to="2026-10-20T00:00:00Z"),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("overlap between" in violation for violation in violations)


def test_from_must_be_before_to():
    violations = _violations(
        [
            _entry("v1", effective_from="2026-10-01T00:00:00Z", effective_to="2026-10-16T00:00:00Z"),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to="2026-10-16T00:00:00Z", rates=RATES_LATER),
        ]
    )
    assert any("must be before effective_to" in violation for violation in violations)


def test_duplicate_interval_start_is_fatal():
    violations = _violations(
        [
            _entry("v1", effective_from="2026-10-01T00:00:00Z", effective_to="2026-10-16T00:00:00Z"),
            _entry("v2", effective_from="2026-10-01T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("duplicate interval start" in violation for violation in violations)


def test_entry_currency_must_equal_list_currency():
    violations = _violations(
        [
            _entry("v1", currency="EUR", effective_to="2026-10-16T00:00:00Z"),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("must equal the list currency" in violation for violation in violations)


def test_open_ended_interval_must_be_last():
    violations = _violations(
        [
            _entry("v1", effective_from="2026-10-01T00:00:00Z", effective_to=None),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to="2026-11-01T00:00:00Z", rates=RATES_LATER),
        ]
    )
    assert any("is not the last interval" in violation for violation in violations)


def test_negative_rate_is_fatal():
    violations = _violations(
        [
            _entry("v1", rates={"cached": "0.150000", "input": "-0.750000", "output": "2.250000"}),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("input rate must match" in violation for violation in violations)


def test_unknown_unit_is_fatal():
    violations = _violations(
        [
            _entry("v1", rates={"cached": "0.150000", "input": "0.750000", "output": "2.250000", "audio": "0.100000"}),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("unknown unit" in violation for violation in violations)


def test_missing_unit_is_fatal():
    violations = _violations(
        [
            _entry("v1", rates={"input": "0.750000", "output": "2.250000"}),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("missing the 'cached' rate" in violation for violation in violations)


@pytest.mark.parametrize("bad_rate", ["0.75", "0.75000", "NaN", "Infinity", "1E+6", " 0.750000", "0.750000 "])
def test_rate_shape_rejects_every_malformed_form(bad_rate):
    violations = _violations(
        [
            _entry("v1", rates={"cached": "0.150000", "input": bad_rate, "output": "2.250000"}),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("input rate must match" in violation for violation in violations)


def test_bad_currency_shape_is_fatal():
    violations = _violations(currency="usd")
    assert any("currency must match" in violation for violation in violations)


def test_label_must_be_synthetic():
    violations = _violations(label="actual")
    assert any("label must be 'synthetic'" in violation for violation in violations)


def test_timestamp_offset_is_rejected():
    violations = _violations(
        [
            _entry("v1", effective_from="2026-10-01T00:00:00+05:30"),
            _entry("v2", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("effective_from must be a strict UTC timestamp" in violation for violation in violations)


def test_duplicate_price_version_id_is_fatal():
    violations = _violations(
        [
            _entry("same", effective_from="2026-10-01T00:00:00Z", effective_to="2026-10-16T00:00:00Z"),
            _entry("same", effective_from="2026-10-16T00:00:00Z", effective_to=None, rates=RATES_LATER),
        ]
    )
    assert any("duplicate price_version_id" in violation for violation in violations)


def test_unknown_root_field_is_fatal():
    violations = _violations(surprise="value")
    assert any("unknown price list field 'surprise'" in violation for violation in violations)


def test_duplicate_start_is_keyed_including_provider():
    """Two providers listing the same model may both start on the same instant."""
    entries = [
        _entry("a1", provider="p1", effective_from="2026-10-01T00:00:00Z", effective_to=None),
        _entry("b1", provider="p2", effective_from="2026-10-01T00:00:00Z", effective_to=None, rates=RATES_LATER),
    ]
    price_list = parse_price_list(_document(entries))
    assert price_list.knows("p1", "m")
    assert price_list.knows("p2", "m")
    assert price_list.series[("p1", "m", "input")].intervals[0].price_version_id == "a1"
    assert price_list.series[("p2", "m", "input")].intervals[0].price_version_id == "b1"


def test_a_non_utf8_price_list_is_a_schedule_error_not_a_crash(tmp_path):
    from ai_cost_margin.pricing import load_price_list

    bad = tmp_path / "bad-encoding.json"
    bad.write_bytes(b'\xff\xfe\x00not utf-8')
    with pytest.raises(PriceScheduleError) as caught:
        load_price_list(bad)
    assert any("cannot read price list" in violation for violation in caught.value.violations)
