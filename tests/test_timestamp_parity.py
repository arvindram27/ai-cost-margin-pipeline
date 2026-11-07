"""Pin the deliberate duplication: pricing.parse_utc and schema._is_valid_timestamp.

The Phase 2 pricing contract (section 2) states that the two validators have
identical accept/reject semantics. This test asserts that over a fixed corpus.
"""

from __future__ import annotations

import pytest

from ai_cost_margin.pricing import parse_utc
from ai_cost_margin.schema import _is_valid_timestamp

ACCEPT = [
    "2026-10-01T00:00:00Z",
    "2026-10-01T00:00:00.1Z",
    "2026-10-01T00:00:00.123456Z",
    "2024-02-29T23:59:59Z",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59Z",
    "2026-01-31T12:30:45Z",
    "2026-10-01T00:00:00.000000Z",
]

REJECT = [
    "2026-10-01T00:00:00.1234567Z",
    "2026-10-01T00:00:00+00:00",
    "2026-10-01T00:00:00+05:30",
    "2026-10-01T00:00:00",
    "2026-10-01t00:00:00z",
    "2026-10-01 00:00:00Z",
    "2026-10-01",
    "T00:00:00Z",
    "2026-02-29T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-00-10T00:00:00Z",
    "2026-10-32T00:00:00Z",
    "2026-10-01T24:00:00Z",
    "2026-10-01T23:60:00Z",
    "2026-10-01T23:59:60Z",
    "2026-1-1T00:00:00Z",
    "2026-10-01T0:00:00Z",
    "2026-10-01T00:00:00.Z",
    "",
    "Z",
    "20261001T000000Z",
    " 2026-10-01T00:00:00Z",
    "2026-10-01T00:00:00Z ",
    "2026-10-01T00:00:00ZZ",
    "2026-10-01T00:00:00,5Z",
    "2026-04-31T00:00:00Z",
    "999-12-31T23:59:59Z",
    "2026-10-01T00:00:00.-1Z",
    "2026-10-01T00:00:00.+5Z",
    "10000-01-01T00:00:00Z",
]

NON_STRINGS = [None, 0, 20261001, True, ["2026-10-01T00:00:00Z"]]


def _pricing_accepts(value):
    try:
        parse_utc(value)
    except (ValueError, TypeError):
        return False
    return True


@pytest.mark.parametrize("value", ACCEPT)
def test_both_validators_accept(value):
    assert _is_valid_timestamp(value) is True
    assert _pricing_accepts(value) is True


@pytest.mark.parametrize("value", REJECT)
def test_both_validators_reject(value):
    assert _is_valid_timestamp(value) is False
    assert _pricing_accepts(value) is False


@pytest.mark.parametrize("value", NON_STRINGS)
def test_both_validators_reject_non_strings(value):
    assert _is_valid_timestamp(value) is False
    assert _pricing_accepts(value) is False


def test_the_corpus_is_not_vacuous():
    assert len(ACCEPT) >= 8
    assert len(REJECT) >= 29
