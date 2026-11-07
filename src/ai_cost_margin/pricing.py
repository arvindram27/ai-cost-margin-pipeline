"""Price-list model, interval validation and rate selection (Phase 2).

Everything in the price list is synthetic and labelled ``synthetic``. No real
provider price appears here and none is assumed.

The rules in this module are fatal by design: a price schedule with an internal
gap, an overlap, a duplicate start or a second currency cannot say which rate
applies, so costing stops instead of choosing one. An event that simply falls
outside a valid schedule is *not* fatal - it is unpriced, which is
``costing.py``'s job.

Intervals are half-open ``[effective_from, effective_to)`` in UTC; ``None`` as
``effective_to`` means open-ended and is only allowed on the last interval of a
series. A gap is only a gap **between** two intervals of one series; a region
before a series' first interval, or after an open end, is simply not covered and
becomes an unpriced event.

Rate strings must match ``^\\d+\\.\\d{6}$`` exactly. That shape rejects ``NaN``,
``Infinity``, exponents, signs and surrounding whitespace by construction, and
each accepted rate is converted to an integer micro numerator so no decimal
rounding context is ever involved in a price.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "Interval",
    "PRICE_LIST_VERSION",
    "PriceList",
    "PriceScheduleError",
    "RATE_PATTERN",
    "RATE_UNIT",
    "RateSeries",
    "Selection",
    "TIMEZONE",
    "UNITS",
    "load_price_list",
    "parse_price_list",
    "parse_utc",
]

PRICE_LIST_VERSION = "1.0"
RATE_UNIT = "per_1_000_000_tokens"
TIMEZONE = "UTC"
UNITS = ("input", "output", "cached")

RATE_PATTERN = re.compile(r"^\d+\.\d{6}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")

ROOT_FIELDS = frozenset(
    {"price_list_id", "label", "currency", "rate_unit", "timezone", "notes", "entries"}
)
ENTRY_FIELDS = frozenset(
    {
        "price_version_id",
        "provider",
        "model",
        "currency",
        "effective_from",
        "effective_to",
        "rates_per_million",
    }
)


class PriceScheduleError(ValueError):
    """The price schedule itself is invalid; costing must not run."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = tuple(sorted(violations))
        super().__init__("; ".join(self.violations) if self.violations else "invalid price schedule")


def parse_utc(value: Any) -> datetime:
    """Parse a strict UTC timestamp, ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``.

    This deliberately duplicates the check Phase 1 keeps private in
    ``schema._is_valid_timestamp``. The semantics are identical: fixed layout,
    up to six fraction digits, a mandatory trailing ``Z``, no offsets, and real
    calendar validation. It is duplicated because Phase 2 edits no existing
    file and that helper is not part of ``schema``'s public API.
    """
    if not isinstance(value, str) or _TIMESTAMP_PATTERN.match(value) is None:
        raise ValueError("timestamp must be strict UTC matching YYYY-MM-DDTHH:MM:SS[.ffffff]Z")
    has_fraction = "." in value
    layout = "%Y-%m-%dT%H:%M:%S" + (".%f" if has_fraction else "") + "Z"
    try:
        parsed = datetime.strptime(value, layout)
    except ValueError as exc:
        raise ValueError(f"timestamp is not a real date: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _valid_timestamp(value: Any) -> bool:
    try:
        parse_utc(value)
    except ValueError:
        return False
    return True


def _valid_text(value: Any) -> bool:
    return isinstance(value, str) and value != "" and not any(character.isspace() for character in value)


def _parse_micro_rate(text: Any) -> int | None:
    if not isinstance(text, str) or RATE_PATTERN.match(text) is None:
        return None
    whole, fraction = text.split(".")
    return int(whole) * 1_000_000 + int(fraction)


@dataclass(frozen=True)
class Interval:
    """One priced version of one series, as a half-open UTC interval."""

    price_version_id: str
    effective_from: datetime
    effective_to: datetime | None
    rate_micro: int


@dataclass(frozen=True)
class RateSeries:
    """One (provider, model, unit) series: contiguous, non-overlapping intervals."""

    provider: str
    model: str
    unit: str
    intervals: tuple[Interval, ...]

    def covering(self, moment: datetime) -> Interval | None:
        for interval in self.intervals:
            if interval.effective_from <= moment:
                if interval.effective_to is None or moment < interval.effective_to:
                    return interval
        return None


@dataclass(frozen=True)
class Selection:
    """The rates and price versions that apply to one event."""

    provider: str
    model: str
    currency: str
    rates_micro: dict[str, int]
    price_version_ids: dict[str, str]


@dataclass(frozen=True)
class PriceList:
    """A validated synthetic price list."""

    price_list_id: str
    label: str
    currency: str
    series: dict[tuple[str, str, str], RateSeries]

    def knows(self, provider: str, model: str) -> bool:
        """True when the (provider, model) pair has at least one unit series."""
        return any((provider, model, unit) in self.series for unit in UNITS)

    def select(self, provider: str, model: str, usage_time: str) -> Selection | None:
        """Select the rates by the event's usage time; ``None`` if not covered.

        Raises ``ValueError`` only for an unparseable timestamp, which a stored
        Phase 1 record can never contain.
        """
        moment = parse_utc(usage_time)
        rates: dict[str, int] = {}
        versions: dict[str, str] = {}
        for unit in UNITS:
            series = self.series.get((provider, model, unit))
            if series is None:
                return None
            interval = series.covering(moment)
            if interval is None:
                return None
            rates[unit] = interval.rate_micro
            versions[unit] = interval.price_version_id
        return Selection(provider, model, self.currency, rates, versions)


def load_price_list(path: Path) -> PriceList:
    """Read and validate a price-list file. Any problem is a fatal schedule error."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PriceScheduleError([f"cannot read price list: {type(exc).__name__ if isinstance(exc, UnicodeDecodeError) else exc.strerror or type(exc).__name__}"]) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PriceScheduleError([f"price list is not valid JSON: {exc.msg}"]) from exc
    return parse_price_list(document)


def parse_price_list(document: Any) -> PriceList:
    """Validate a decoded price-list document and build the per-unit series."""
    if not isinstance(document, dict):
        raise PriceScheduleError(["price list must be a JSON object"])

    violations: list[str] = []
    for name in sorted(set(document) - ROOT_FIELDS):
        violations.append(f"unknown price list field {name!r}")
    for name in ("price_list_id", "label", "currency", "rate_unit", "timezone", "entries"):
        if name not in document:
            violations.append(f"missing price list field {name!r}")
    if violations:
        raise PriceScheduleError(violations)

    if not _valid_text(document["price_list_id"]):
        violations.append("price_list_id must be a non-empty string without whitespace")
    if document["label"] != "synthetic":
        violations.append(f"label must be 'synthetic', got {document['label']!r}")
    currency = document["currency"]
    if not isinstance(currency, str) or _CURRENCY_PATTERN.match(currency) is None:
        violations.append("currency must match '^[A-Z]{3}$'")
    if document["rate_unit"] != RATE_UNIT:
        violations.append(f"rate_unit must be {RATE_UNIT!r}")
    if document["timezone"] != TIMEZONE:
        violations.append(f"timezone must be {TIMEZONE!r}")
    if "notes" in document and not isinstance(document["notes"], str):
        violations.append("notes must be a string")
    entries = document["entries"]
    if not isinstance(entries, list):
        violations.append("entries must be a list")
        raise PriceScheduleError(violations)

    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            violations.append(f"{where} must be an object")
            continue
        unknown = sorted(set(entry) - ENTRY_FIELDS)
        for name in unknown:
            violations.append(f"{where} has unknown field {name!r}")
        missing = [name for name in sorted(ENTRY_FIELDS) if name not in entry]
        for name in missing:
            violations.append(f"{where} is missing field {name!r}")
        if unknown or missing:
            continue

        if not _valid_text(entry["price_version_id"]):
            violations.append(f"{where} price_version_id must be a non-empty string without whitespace")
        if not _valid_text(entry["provider"]):
            violations.append(f"{where} provider must be a non-empty string without whitespace")
        if not _valid_text(entry["model"]):
            violations.append(f"{where} model must be a non-empty string without whitespace")

        entry_currency = entry["currency"]
        if not isinstance(entry_currency, str) or _CURRENCY_PATTERN.match(entry_currency) is None:
            violations.append(f"{where} currency must match '^[A-Z]{3}$'")
        elif isinstance(currency, str) and entry_currency != currency:
            violations.append(
                f"{where} currency {entry_currency!r} must equal the list currency {currency!r}"
            )

        if not _valid_timestamp(entry["effective_from"]):
            violations.append(f"{where} effective_from must be a strict UTC timestamp")
            continue
        effective_to = entry["effective_to"]
        if effective_to is not None and not _valid_timestamp(effective_to):
            violations.append(f"{where} effective_to must be null or a strict UTC timestamp")
            continue
        start = parse_utc(entry["effective_from"])
        end = parse_utc(effective_to) if effective_to is not None else None
        if end is not None and start >= end:
            violations.append(f"{where} effective_from must be before effective_to")

        rates = entry["rates_per_million"]
        micro: dict[str, int] = {}
        if not isinstance(rates, dict):
            violations.append(f"{where} rates_per_million must be an object")
            continue
        unknown_units = sorted(set(rates) - set(UNITS))
        for name in unknown_units:
            violations.append(f"{where} has unknown unit {name!r}")
        for unit in UNITS:
            if unit not in rates:
                violations.append(f"{where} is missing the {unit!r} rate")
                continue
            parsed_rate = _parse_micro_rate(rates[unit])
            if parsed_rate is None:
                violations.append(
                    f"{where} {unit} rate must match '^\\d+\\.\\d{{6}}$', got {rates[unit]!r}"
                )
            else:
                micro[unit] = parsed_rate
        if unknown_units or len(micro) != len(UNITS):
            continue

        parsed.append(
            {
                "price_version_id": entry["price_version_id"],
                "provider": entry["provider"],
                "model": entry["model"],
                "effective_from": start,
                "effective_to": end,
                "micro": micro,
            }
        )

    version_ids = [item["price_version_id"] for item in parsed]
    for duplicated in sorted({value for value in version_ids if version_ids.count(value) > 1}):
        violations.append(f"duplicate price_version_id {duplicated!r}")

    starts = [(item["provider"], item["model"], item["effective_from"]) for item in parsed]
    for duplicated in sorted({value for value in starts if starts.count(value) > 1}):
        violations.append(f"duplicate interval start for {duplicated[0]}/{duplicated[1]} at {duplicated[2].isoformat()}")

    series: dict[tuple[str, str, str], RateSeries] = {}
    grouped: dict[tuple[str, str, str], list[Interval]] = {}
    for item in parsed:
        for unit in UNITS:
            grouped.setdefault((item["provider"], item["model"], unit), []).append(
                Interval(
                    price_version_id=item["price_version_id"],
                    effective_from=item["effective_from"],
                    effective_to=item["effective_to"],
                    rate_micro=item["micro"][unit],
                )
            )

    for key in sorted(grouped):
        provider, model, unit = key
        where = f"{provider}/{model}/{unit}"
        intervals = sorted(grouped[key], key=lambda value: value.effective_from)
        open_ended = 0
        for position, interval in enumerate(intervals):
            if interval.effective_to is not None:
                continue
            open_ended += 1
            if position != len(intervals) - 1:
                violations.append(
                    f"{where}: open-ended interval {interval.price_version_id!r} is not the last interval"
                )
        if open_ended > 1:
            violations.append(f"{where}: more than one open-ended interval")
        for previous, following in zip(intervals, intervals[1:]):
            if previous.effective_to is None:
                continue
            if following.effective_from < previous.effective_to:
                violations.append(
                    f"{where}: overlap between {previous.price_version_id!r} and {following.price_version_id!r}"
                )
            elif following.effective_from > previous.effective_to:
                violations.append(
                    f"{where}: gap between {previous.price_version_id!r} and {following.price_version_id!r}"
                )
        series[key] = RateSeries(provider, model, unit, tuple(intervals))

    if violations:
        raise PriceScheduleError(violations)

    return PriceList(
        price_list_id=document["price_list_id"],
        label=document["label"],
        currency=currency,
        series=series,
    )
