"""Rate selection by the event's usage time, keyed by (provider, model)."""

from __future__ import annotations

from ai_cost_margin.pricing import parse_price_list

RATES_A = {"cached": "0.150000", "input": "0.750000", "output": "2.250000"}
RATES_B = {"cached": "0.187500", "input": "0.937500", "output": "2.812500"}


def _entry(version_id, provider, rates, effective_from, effective_to):
    return {
        "currency": "USD",
        "effective_from": effective_from,
        "effective_to": effective_to,
        "model": "m",
        "price_version_id": version_id,
        "provider": provider,
        "rates_per_million": dict(rates),
    }


def _price_list():
    entries = []
    for provider, prefix in (("p", "p"), ("q", "q")):
        entries.append(_entry(f"{prefix}-v1", provider, RATES_A, "2026-10-01T00:00:00Z", "2026-10-16T00:00:00Z"))
        entries.append(_entry(f"{prefix}-v2", provider, RATES_B, "2026-10-16T00:00:00Z", None))
    return parse_price_list(
        {
            "currency": "USD",
            "entries": entries,
            "label": "synthetic",
            "price_list_id": "selection-test",
            "rate_unit": "per_1_000_000_tokens",
            "timezone": "UTC",
        }
    )


def test_selects_the_covering_interval():
    selection = _price_list().select("p", "m", "2026-10-05T00:00:00Z")
    assert selection is not None
    assert selection.price_version_ids == {"input": "p-v1", "output": "p-v1", "cached": "p-v1"}
    assert selection.rates_micro["input"] == 750000


def test_effective_from_is_inclusive():
    selection = _price_list().select("p", "m", "2026-10-01T00:00:00Z")
    assert selection is not None
    assert selection.price_version_ids["input"] == "p-v1"


def test_effective_to_is_exclusive():
    selection = _price_list().select("p", "m", "2026-10-16T00:00:00Z")
    assert selection is not None
    assert selection.price_version_ids["input"] == "p-v2"


def test_price_change_mid_period_selects_the_later_version():
    price_list = _price_list()
    before = price_list.select("p", "m", "2026-10-15T23:59:59Z")
    after = price_list.select("p", "m", "2026-10-16T00:00:01Z")
    assert before is not None and after is not None
    assert before.price_version_ids["input"] == "p-v1"
    assert after.price_version_ids["input"] == "p-v2"
    assert after.rates_micro["input"] == 937500


def test_before_the_first_interval_is_not_covered():
    assert _price_list().select("p", "m", "2026-09-30T23:59:59Z") is None


def test_open_ended_interval_covers_the_far_future():
    selection = _price_list().select("p", "m", "2027-06-01T00:00:00Z")
    assert selection is not None
    assert selection.price_version_ids["input"] == "p-v2"


def test_two_providers_listing_the_same_model_select_their_own_rates():
    price_list = _price_list()
    first = price_list.select("p", "m", "2026-10-05T00:00:00Z")
    second = price_list.select("q", "m", "2026-10-05T00:00:00Z")
    assert first is not None and second is not None
    assert first.price_version_ids["input"] == "p-v1"
    assert second.price_version_ids["input"] == "q-v1"


def test_unlisted_provider_model_pair_is_not_covered():
    price_list = _price_list()
    assert price_list.select("unknown", "m", "2026-10-05T00:00:00Z") is None
    assert price_list.select("p", "unknown", "2026-10-05T00:00:00Z") is None
    assert price_list.knows("p", "m")
    assert not price_list.knows("p", "unknown")
