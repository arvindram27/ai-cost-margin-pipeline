"""Costing behaviour: exact arithmetic, the unpriced bucket, supersession."""

from __future__ import annotations

import hashlib

import pytest

from ai_cost_margin.costing import LineageAmbiguityError, cost_event, cost_events, resolve_superseded
from ai_cost_margin.money import DENOMINATOR, Money
from ai_cost_margin.pricing import parse_price_list

RATES = {"cached": "0.150000", "input": "0.750000", "output": "2.250000"}
RATES_LATER = {"cached": "0.187500", "input": "0.937500", "output": "2.812500"}


def _entry(version_id, rates, effective_from, effective_to):
    return {
        "currency": "USD",
        "effective_from": effective_from,
        "effective_to": effective_to,
        "model": "m",
        "price_version_id": version_id,
        "provider": "p",
        "rates_per_million": dict(rates),
    }


def _price_list():
    return parse_price_list(
        {
            "currency": "USD",
            "entries": [
                _entry("v1", RATES, "2026-10-01T00:00:00Z", "2026-10-16T00:00:00Z"),
                _entry("v2", RATES_LATER, "2026-10-16T00:00:00Z", None),
            ],
            "label": "synthetic",
            "price_list_id": "costing-test",
            "rate_unit": "per_1_000_000_tokens",
            "timezone": "UTC",
        }
    )


def _record(
    event_id: str = "evt-1",
    *,
    provider: str = "p",
    account_id: str = "acct-demo",
    model: str = "m",
    usage_time: str = "2026-10-05T00:00:00Z",
    tokens_input: int | None = 1000,
    tokens_output: int | None = 500,
    tokens_cached: int | None = 0,
    record_version: int = 1,
    supersedes: str | None = None,
) -> dict:
    return {
        "account_id": account_id,
        "arrival_time": "2026-11-01T00:00:00Z",
        "attempt_id": "1",
        "customer_id": "cust-001",
        "feature": "demo-assistant",
        "model": model,
        "payload_sha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
        "provider": provider,
        "provider_event_id": event_id,
        "record_version": record_version,
        "request_id": f"req-{event_id}",
        "supersedes": supersedes,
        "tokens_cached": tokens_cached,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "unavailable_fields": [],
        "usage_time": usage_time,
        "workload_type": "short_light",
    }


def test_three_rate_cost_is_exact():
    row = cost_event(_record(tokens_input=1000, tokens_output=500, tokens_cached=200), _price_list())
    assert row["cost_status"] == "costed"
    assert row["cost_input"] == "0.000600000000"
    assert row["cost_cached"] == "0.000030000000"
    assert row["cost_output"] == "0.001125000000"
    assert row["total_cost"] == "0.001755000000"
    assert row["cost_label"] == "estimated"
    assert row["price_list_label"] == "synthetic"
    assert row["currency"] == "USD"


def test_cached_subset_reduces_the_input_charge():
    with_cached = cost_event(_record(tokens_input=1000, tokens_cached=200), _price_list())
    without_cached = cost_event(_record(tokens_input=1000, tokens_cached=0), _price_list())

    # Hand-derived from _price_list: the "m" input rate is 0.750000 per million
    # tokens (input micro 750000), and the default usage_time 2026-10-05 selects
    # version v1, so input cost = (tokens_input - tokens_cached) * 750000 / 10^12.
    #   without cached: (1000 - 0)   * 750000 / 10^12 = 750000000 / 10^12 = 0.000750000000
    #   with cached:    (1000 - 200) * 750000 / 10^12 = 600000000 / 10^12 = 0.000600000000
    assert without_cached["cost_input"] == "0.000750000000"
    assert with_cached["cost_input"] == "0.000600000000"

    # The original intent, as a Decimal comparison rather than a Money-to-Money
    # ordering (Money defines only __add__, __sub__ and __str__): caching those
    # 200 tokens strictly lowers the input charge.
    assert (
        Money.from_str(with_cached["cost_input"], "USD").amount
        < Money.from_str(without_cached["cost_input"], "USD").amount
    )
    assert with_cached["cost_cached"] != "0.000000000000"


def test_cached_zero_is_a_costed_zero_not_unpriced():
    row = cost_event(_record(tokens_cached=0), _price_list())
    assert row["cost_status"] == "costed"
    assert row["cost_cached"] == "0.000000000000"


def test_unavailable_cached_makes_the_cost_unknown():
    row = cost_event(_record(tokens_cached=None), _price_list())
    assert row["cost_status"] == "unpriced"
    assert row["unpriced_reason"] == "token_count_unavailable"
    assert row["total_cost"] is None


def test_unavailable_input_makes_the_cost_unknown():
    row = cost_event(_record(tokens_input=None), _price_list())
    assert row["cost_status"] == "unpriced"
    assert row["unpriced_reason"] == "token_count_unavailable"


def test_cached_greater_than_input_is_inconsistent():
    row = cost_event(_record(tokens_input=100, tokens_cached=200), _price_list())
    assert row["cost_status"] == "unpriced"
    assert row["unpriced_reason"] == "inconsistent_token_counts"


def test_unlisted_provider_model_pair_is_unpriced():
    row = cost_event(_record(model="unlisted"), _price_list())
    assert row["cost_status"] == "unpriced"
    assert row["unpriced_reason"] == "model_not_in_price_list"


def test_unlisted_provider_is_unpriced():
    row = cost_event(_record(provider="other-provider"), _price_list())
    assert row["unpriced_reason"] == "model_not_in_price_list"


def test_usage_time_outside_the_series_is_unpriced():
    row = cost_event(_record(usage_time="2026-09-01T00:00:00Z"), _price_list())
    assert row["cost_status"] == "unpriced"
    assert row["unpriced_reason"] == "no_applicable_rate"


def test_zero_tokens_are_costed_at_exact_zero():
    row = cost_event(_record(tokens_input=0, tokens_output=0, tokens_cached=0), _price_list())
    assert row["cost_status"] == "costed"
    assert row["total_cost"] == "0.000000000000"


def test_each_unit_records_the_price_version_that_costed_it():
    early = cost_event(_record(usage_time="2026-10-05T00:00:00Z"), _price_list())
    late = cost_event(_record(usage_time="2026-10-20T00:00:00Z"), _price_list())
    assert early["price_version_input"] == "v1"
    assert early["price_version_output"] == "v1"
    assert early["price_version_cached"] == "v1"
    assert late["price_version_input"] == "v2"
    assert late["price_version_output"] == "v2"
    assert late["price_version_cached"] == "v2"


def test_total_equals_the_exact_sum_of_the_units():
    row = cost_event(_record(tokens_input=4321, tokens_output=987, tokens_cached=123), _price_list())
    total = Money.from_str(row["total_cost"], "USD")
    summed = (
        Money.from_str(row["cost_input"], "USD")
        + Money.from_str(row["cost_output"], "USD")
        + Money.from_str(row["cost_cached"], "USD")
    )
    assert total == summed
    assert str(total) == row["total_cost"]


def test_unpriced_rows_carry_nulls_and_a_reason():
    row = cost_event(_record(tokens_input=None), _price_list())
    for field in (
        "currency",
        "cost_input",
        "cost_output",
        "cost_cached",
        "total_cost",
        "price_version_input",
        "price_version_output",
        "price_version_cached",
    ):
        assert row[field] is None, field
    assert row["unpriced_reason"] == "token_count_unavailable"


def test_huge_token_count_costs_exactly():
    tokens = 999999999999999999999999999
    row = cost_event(_record(tokens_input=tokens, tokens_output=0, tokens_cached=0), _price_list())
    assert row["cost_status"] == "costed"
    units = tokens * 750000
    whole, fraction = divmod(units, DENOMINATOR)
    assert row["cost_input"] == f"{whole}.{fraction:012d}"
    assert row["total_cost"] == f"{whole}.{fraction:012d}"


def test_higher_record_version_supersedes_the_lower_one():
    events = [_record(record_version=1), _record(record_version=2, tokens_input=111)]
    rows, report = cost_events(events, _price_list())
    assert rows[0]["cost_status"] == "superseded"
    assert rows[1]["cost_status"] == "costed"
    assert report["events_superseded"] == 1
    assert report["events_read"] == report["events_costed"] + report["events_unpriced"] + report["events_superseded"]


def test_a_different_event_id_with_supersedes_supersedes_its_target():
    events = [
        _record("evt-old"),
        _record("evt-new", supersedes="evt-old", tokens_input=222),
    ]
    rows, report = cost_events(events, _price_list())
    assert rows[0]["cost_status"] == "superseded"
    assert rows[1]["cost_status"] == "costed"
    assert rows[1]["supersedes"] == "evt-old"
    assert report["events_superseded"] == 1


def test_self_referencing_supersedes_has_no_superseding_effect():
    events = [
        _record(record_version=1),
        _record(record_version=2, supersedes="evt-1", tokens_input=333),
    ]
    rows, _ = cost_events(events, _price_list())
    assert rows[0]["cost_status"] == "superseded"
    assert rows[1]["cost_status"] == "costed"
    assert rows[1]["record_version"] == 2


def test_a_superseded_event_with_unavailable_tokens_is_superseded_not_unpriced():
    events = [
        _record(record_version=1, tokens_cached=None),
        _record(record_version=2, tokens_cached=None, tokens_input=444),
    ]
    rows, report = cost_events(events, _price_list())
    assert rows[0]["cost_status"] == "superseded"
    assert rows[0]["unpriced_reason"] is None
    assert report["events_superseded"] == 1

    # The superseded row is not unpriced, but the *current* record
    # (record_version 2) still is: the contract makes a null required token count
    # token_count_unavailable. The superseded row must not be counted there.
    assert rows[1]["cost_status"] == "unpriced"
    assert rows[1]["unpriced_reason"] == "token_count_unavailable"
    assert report["events_unpriced"] == 1
    assert report["events_costed"] == 0
    assert report["events_read"] == (
        report["events_costed"] + report["events_unpriced"] + report["events_superseded"]
    ) == 2


def test_a_superseded_row_does_not_leak_into_the_unpriced_bucket_when_the_current_record_is_priceable():
    events = [
        _record(record_version=1, tokens_cached=0),
        _record(record_version=2, tokens_cached=0, tokens_input=444),
    ]
    rows, report = cost_events(events, _price_list())

    assert rows[0]["cost_status"] == "superseded"
    assert rows[0]["unpriced_reason"] is None

    # Hand-derived from _price_list: usage_time 2026-10-05 selects version v1
    # (input 0.750000, output 2.250000, cached 0.150000 per million tokens).
    #   input:  (444 - 0) * 750000  / 10^12 = 333000000 / 10^12 = 0.000333000000
    #   cached: 0         * 150000  / 10^12 = 0                  = 0.000000000000
    #   output: 500       * 2250000 / 10^12 = 1125000000 / 10^12 = 0.001125000000
    #   total:                                                      0.001458000000
    assert rows[1]["cost_status"] == "costed"
    assert rows[1]["cost_input"] == "0.000333000000"
    assert rows[1]["cost_cached"] == "0.000000000000"
    assert rows[1]["cost_output"] == "0.001125000000"
    assert rows[1]["total_cost"] == "0.001458000000"

    assert report["events_costed"] == 1
    assert report["events_unpriced"] == 0
    assert report["events_superseded"] == 1
    assert report["events_read"] == (
        report["events_costed"] + report["events_unpriced"] + report["events_superseded"]
    ) == 2


def test_retry_attempts_are_both_costed():
    first = _record("evt-attempt-1", tokens_output=900)
    second = _record("evt-attempt-2", tokens_output=940)
    first["request_id"] = second["request_id"] = "req-retry"
    rows, report = cost_events([first, second], _price_list())
    assert [row["cost_status"] for row in rows] == ["costed", "costed"]
    assert report["events_superseded"] == 0


def test_two_records_claiming_the_same_target_are_ambiguous():
    events = [
        _record("evt-target"),
        _record("evt-a", supersedes="evt-target"),
        _record("evt-b", supersedes="evt-target"),
    ]
    with pytest.raises(LineageAmbiguityError):
        resolve_superseded(events)


def test_a_supersedes_cycle_is_ambiguous():
    events = [
        _record("evt-a", supersedes="evt-b"),
        _record("evt-b", supersedes="evt-a"),
    ]
    with pytest.raises(LineageAmbiguityError):
        resolve_superseded(events)
