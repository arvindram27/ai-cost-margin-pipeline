# Phase 2 pricing contract

Status: **synthetic prices only**. This document defines the pricing rules the
Phase 2 code enforces. It is not a description of any real provider's pricing:
no live provider has been contacted, no real rate has been used, and none is
assumed. The price list is labelled `synthetic`, and every cost it produces is
labelled `estimated` — it is **not** a billed amount.

Companion documents: the project brief and the implementation roadmap in this
directory, the Phase 1 ingestion contract, and the append-only build log at the
repo root.

## 1. Scope

Phase 2 costs the events that Phase 1 stored. It does not aggregate, attribute
margins, publish anything or talk to a network. Pricing is per event; period
aggregation is Phase 3.

## 2. Time

Everything is **UTC**. Timestamps are the strict Phase 1 shape
`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`: a fixed layout, up to six fraction digits, a
mandatory trailing `Z`, no offsets. Any future period boundary is
`[YYYY-MM-01T00:00:00Z, next month)`.

The price applied to an event is selected by the event's **`usage_time`**, never
by its arrival time, its record version or the order it was ingested.

Phase 2 parses timestamps with its own private helper in `pricing.py`. That is a
deliberate duplication of the check Phase 1 keeps private in
`schema._is_valid_timestamp`: the semantics are identical, but Phase 1 never
exported that helper and Phase 2 edits no existing file. The duplication is
pinned by a test that asserts the same accept/reject set.

## 3. Price-list file format

One JSON object per file.

| Field | Type | Rules |
| --- | --- | --- |
| `price_list_id` | string | non-empty, no whitespace |
| `label` | string | must be `"synthetic"`; a list that does not claim to be synthetic is rejected |
| `currency` | string | the list's single currency, `^[A-Z]{3}$` |
| `rate_unit` | string | must be `"per_1_000_000_tokens"` |
| `timezone` | string | must be `"UTC"` |
| `notes` | string | optional free text |
| `entries` | array | price entries; may be empty, in which case every event is unpriced |

Entry fields:

| Field | Type | Rules |
| --- | --- | --- |
| `price_version_id` | string | non-empty, unique across the file; recorded on every costed event |
| `provider` | string | non-empty; part of the series key |
| `model` | string | non-empty; part of the series key |
| `currency` | string | must equal the list currency |
| `effective_from` | string | strict UTC timestamp |
| `effective_to` | string or null | strict UTC timestamp strictly after `from`, or `null` for open-ended |
| `rates_per_million` | object | exactly the three keys `input`, `output`, `cached` |

Rate values are decimal **strings** matching `^\d+\.\d{6}$` — six fraction
digits, no sign, no exponent, no whitespace. That shape rejects `NaN`,
`Infinity`, `1E+6`, `-0.5` and ` 0.5 ` by construction.

Unknown keys anywhere in the file are rejected, following Phase 1's strict-schema
convention.

## 4. Series and intervals

A **series** is `(provider, model, unit)`. Each entry contributes one interval to
each of the three unit series.

- Intervals are half-open: `effective_from` is inclusive, `effective_to` is
  exclusive. An event exactly at `from` uses that interval; exactly at `to` uses
  the next one.
- `effective_to: null` means open-ended and is only allowed on the last interval
  of a series.
- The intervals of one series must tile contiguously and without overlap.

Because each entry carries all three unit rates, the three unit series of one
`(provider, model)` pair always share their boundaries; the validator checks
each unit series independently anyway, which keeps the rule local and testable.

## 5. Fatal errors (class A) — costing does not run

`PriceScheduleError` is raised during validation, before any event is read:

1. `effective_from >= effective_to`.
2. An overlap inside one series (`next.from < previous.to`).
3. An **internal** gap inside one series (`next.from > previous.to`).
4. More than one open-ended interval in a series, or an open-ended interval that
   is not the last.
5. A duplicate `price_version_id`, or a duplicate `(provider, model,
   effective_from)` — the provider is part of the duplicate key.
6. A unit outside `{input, output, cached}`, a missing unit rate, or a rate that
   does not match `^\d+\.\d{6}$`.
7. A currency that is not `^[A-Z]{3}$`, or an entry currency different from the
   list currency.
8. A `label` other than `"synthetic"`, a wrong `rate_unit`, or a wrong
   `timezone`.
9. Unknown keys in the file or an entry, a missing required key, or a JSON file
   that cannot be read or parsed.

The CLI prints the **sorted** violation list on stderr and exits **2**. Nothing
is written: no `costed-events.jsonl`, no `costing-report.json`.

A region **outside** a series' overall span (before the first `effective_from`)
is not a class-A error. It is simply not covered, and an event landing there is
unpriced. Only a gap *between* two intervals is fatal.

## 6. Unpriced events (class B) — the run completes

The cost is unknown, so the amounts are `null` and never `0`. One reason per
event, chosen by this fixed order so the result is deterministic:

1. `model_not_in_price_list` — no series for the event's `(provider, model)`
   pair at all.
2. `no_applicable_rate` — the pair is listed but a required unit's series does
   not cover `usage_time`.
3. `token_count_unavailable` — a required token count is `null` (Phase 1's
   explicit unavailability).
4. `inconsistent_token_counts` — `tokens_cached > tokens_input`, which
   contradicts assumption S1 below.

Unpriced is a reported outcome, not a failure: the run exits 0 and the report
counts the reasons.

## 7. Cached tokens — synthetic assumption S1

**S1: cached tokens are a subset of `tokens_input` and are billed at the cached
rate instead of the input rate.** This is a modelling assumption of this
synthetic price list. It is not a claim about any real provider's billing, and it
must be re-verified before any real data is costed.

Under S1:

- input cost = `(tokens_input − tokens_cached) × input_rate`
- cached cost = `tokens_cached × cached_rate`
- output cost = `tokens_output × output_rate`

Consequences: every model must publish all three rates (a missing one is class
A); `tokens_cached: 0` is a real value and a **costed** zero, not an unpriced
event; `tokens_cached: null` makes the split unknowable (`token_count_unavailable`);
`tokens_cached > tokens_input` is a contradiction (`inconsistent_token_counts`).

## 8. Exact arithmetic

Money is never a float and is never rounded.

- A rate becomes an integer **micro numerator**: `"0.750000"` → `750000`, meaning
  0.750000 per million tokens.
- A unit cost is `tokens × micro` exact 10^-12 currency units, i.e.
  `tokens × rate / 10^6`. That is integer multiplication followed by a power-of-
  ten division, so it is exact for **every** token count the Phase 1 schema can
  produce — and that schema places no upper bound on an integer token count.
- `Money` stores the integer 10^-12 units and performs addition and subtraction
  as integer arithmetic. No `decimal` rounding context participates in producing
  a value, so a large amount cannot round silently.
- The public `Money.amount` is a `Decimal` built from the exact digits, and
  `str(Money)` renders exactly twelve decimal places.
- The event total is the exact sum of the three unit costs; quantising to twelve
  decimal places is a formatting normalisation, never a value change.
- Phase 3 must sum the stored per-event totals without re-rounding.

`float` never enters: a `Money` built from a float unit count raises, and rates
are parsed from decimal strings.

## 9. Superseded records

The Phase 1 store is append-only and keeps superseded records. Costing them would
charge the same usage twice, so a stored record R is **superseded** when either:

(a) another stored record has the same `(provider, account_id,
    provider_event_id)` with a **higher** `record_version`; or
(b) another stored record under the same `(provider, account_id)` with a
    **different** `provider_event_id` carries `supersedes` equal to R's
    `provider_event_id`.

A record never supersedes itself: `supersedes` equal to the record's own
`provider_event_id` has no superseding effect, so a version-bumped record that
carries a self-referencing `supersedes` stays current and is costed.

Resolution is exactly these two rules. There is no general lineage engine and no
cycle detection beyond refusing ambiguity: if two different records both claim to
supersede the same id, or the references form a cycle, costing stops with exit 2
rather than guessing. Full lineage is Phase 3's job.

Superseded rows stay in `costed-events.jsonl`, in store order, with
`cost_status: "superseded"` and `null` currency, costs, `price_version_*` and
`unpriced_reason`. They are never priced and never enter the unpriced bucket.

**Retries are not supersession.** Two attempts of one logical request (same
`request_id`, distinct `provider_event_id`, no `supersedes`) are separate real
usage in this synthetic model and are both costed.

## 10. Outputs

`costed-events.jsonl` — one canonical JSON line per stored event, in store order:

| Field | Notes |
| --- | --- |
| `provider`, `account_id`, `provider_event_id`, `attempt_id`, `request_id`, `record_version`, `supersedes`, `model`, `feature`, `workload_type`, `customer_id`, `usage_time`, `payload_sha256` | carried from the stored event so each cost is traceable to exactly one record |
| `cost_status` | `costed`, `unpriced` or `superseded` |
| `currency` | the list currency, `null` unless costed |
| `cost_input`, `cost_output`, `cost_cached`, `total_cost` | twelve-decimal strings, `null` unless costed |
| `cost_label` | always `"estimated"` |
| `price_list_id`, `price_list_label` | e.g. `"synthetic-prices-v1"`, `"synthetic"` |
| `price_version_input`, `price_version_output`, `price_version_cached` | the `price_version_id` that costed each unit; `null` unless costed |
| `unpriced_reason` | one of the four class-B reasons, `null` unless unpriced |

`costing-report.json` — `cost_label`, `currency`, `events_read`,
`events_costed`, `events_unpriced`, `events_superseded`, `price_list_id`,
`price_list_label`, `price_versions_used`, `unpriced_by_reason`. The identity
`events_read = events_costed + events_unpriced + events_superseded` always holds.

## 11. Labelling chain

Synthetic token counts × a synthetic price list produce an **estimated** cost.
The evidence for that chain is in every row: `cost_label: "estimated"`,
`price_list_label: "synthetic"`, and the exact `price_version_*` that applied.
`actual` would require an independent billed amount, which this phase does not
have — that is the Phase 8 reconciliation question. Any margin later derived from
these numbers is **simulated**, not actual profit.

## 12. Command

The package uses a `src/` layout, so module commands need `src` on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "$PWD\src"
.venv\Scripts\python.exe -m ai_cost_margin.costing --home var --prices fixtures\phase2\price-list-synthetic-v1.json --out <fresh directory>
```

`--out` must not exist yet; the engine creates it. Reads `var/state/events.jsonl`
(or whatever `--home` points at). Exit `0` = completed (unpriced and superseded
rows are reported outcomes); exit `2` = fatal schedule, store, lineage or output
error.

Write strategy: schedule validation, the full store read and all output content
happen before any write. The two files are written as `.tmp` then renamed onto
their final names, costed events first. This is **not** a two-file atomic
publish: if the second rename fails the directory may hold an incomplete pair,
and the nonzero exit is what invalidates both files. Nothing is ever deleted or
cleaned up; a rerun uses a fresh directory. True transactional publish is
Phase 4.

## 13. Determinism

Events are emitted in store order; amounts are exact strings; the report's
`price_versions_used` is sorted; every row is canonical JSON (sorted keys, ASCII,
no whitespace) and the report is pretty JSON with sorted keys. No wall clock,
host name or absolute path appears in any output.

## 14. Known limits

- No FX conversion. A price list cannot mix currencies, so cross-currency
  arithmetic is impossible rather than approximated. A future dated FX assumption
  would be an explicit, labelled addition.
- No aggregation, periods or margins yet (Phase 3).
- No transaction across the two output files (Phase 4).
- A price list with no entries is valid and makes every event unpriced.
- Rates are fixed at six fraction digits per million tokens; a smaller unit
  would need a different scale.

## 15. Open questions (recorded, not guessed)

These do not block Phase 2, which is synthetic, and must be answered against real
documentation before any real pricing is used:

1. Are real cached tokens a subset of input tokens, billed at a different rate,
   or a separate charge? S1 is a synthetic assumption only.
2. What are a real provider's billing precision and rounding rules? Phase 2 does
   exact arithmetic and claims no rounding behaviour of any provider.
3. Would a real price list ever need more than one currency, and therefore a
   dated FX assumption?
