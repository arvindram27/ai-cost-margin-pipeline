# Phase 1 ingestion contract

Status: **synthetic fixtures only**. This document defines the contract the Phase 1
ingestion code enforces. It is not a description of any real provider's API: no live
provider has been contacted, no provider field set has been verified, and none is
assumed. Mapping this contract onto a real provider is a separate, approval-gated
step (the Phase 7 gate).

Companion documents: the project brief and the implementation roadmap in this
directory, and the append-only build log at the repo root.

## 1. Versions and scope

- `schema_version` is `"1.0"`. Every event carries it; the batch envelope carries it
  too. The reader understands exactly one version: anything else is dead-lettered as
  `unknown_schema_version`, never coerced.
- Phase 1 covers ingestion only: validation, dedup, quarantine, dead-letter, the
  content-hashed manifest and a run report. Pricing is Phase 2, aggregation and
  lineage-aware margins are Phase 3, DuckDB staging and recovery are Phase 4.
- Inputs are plain JSON files. No database, no network, no upload, no prompt or
  payload field.

## 2. Batch envelope

One JSON object per delivered file.

| Field | Type | Rules |
| --- | --- | --- |
| `schema_version` | string | must be `"1.0"` |
| `batch_id` | string | non-empty, no whitespace |
| `provider` | string | non-empty; part of the dedup key |
| `account_id` | string | non-empty; part of the dedup key |
| `arrival_time` | string | UTC timestamp, `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` |
| `events` | array | may be empty; each element is an event object |

Unknown envelope keys are rejected (`unknown_field`). `arrival_time` models when the
batch reached us; it is supplied by the batch rather than read from the wall clock so
that re-running the same inputs produces the same bytes.

## 3. Event

`provider` and `account_id` come from the envelope. An event that repeats them is
dead-lettered as an unknown field, which keeps the key single-sourced.

| Field | Type | Rules |
| --- | --- | --- |
| `schema_version` | string | must be `"1.0"` |
| `provider_event_id` | string | non-empty, no whitespace, ≤128 chars |
| `attempt_id` | string | non-empty; distinguishes billable retries |
| `request_id` | string | non-empty; groups the attempts of one logical request |
| `model` | string | non-empty |
| `feature` | string | non-empty; open string, so multi-feature attribution can be added later |
| `workload_type` | string | one of `short_light`, `long_heavy`, `spiky`, `cache_heavy` |
| `customer_id` | string or null | `null` means explicitly unattributable (Phase 3's unallocated bucket); it is never invented |
| `usage_time` | string | UTC timestamp; the price applied in Phase 2 is selected by this field |
| `tokens_input` | integer or null | `>= 0`; bool is rejected; `null` only with the marker below |
| `tokens_output` | integer or null | same |
| `tokens_cached` | integer or null | same; an explicit `0` is a real value and is **not** unavailable |
| `unavailable_fields` | array of strings | subset of the three token fields; must match the nulls exactly |
| `record_version` | integer | `>= 1`; bool rejected |
| `supersedes` | string or null | `provider_event_id` of the record being replaced; resolved within the same provider and account |

### 3.1 The unavailability invariant

A token count is unavailable **only** when its value is `null` *and* its name appears
in `unavailable_fields`. Both halves are mandatory: a null without the marker is
`unavailable_marker_inconsistent`, and a marker on a non-null value is the same
reason. This is what keeps "the provider did not tell us" distinct from a billable
zero, and from Phase 2 onward an unavailable count must make the cost unknown rather
than zero.

## 4. Identity, dedup and versioning

The dedup key is **`(provider, account_id, provider_event_id)`**.

For an incoming, valid event:

| Case | Outcome |
| --- | --- |
| its `payload_sha256` equals **any** stored version of its key | `duplicate` — nothing is appended |
| no stored version of the key exists | `inserted` |
| payload differs, `record_version` **greater** than the current version | `inserted` (explicit version bump) |
| payload differs, `record_version` **less** than the current version | quarantined as `version_regression` |
| payload differs, `record_version` **equal** to the current version | quarantined as `same_key_changed_payload` |

Two accepted correction paths, and only two:

1. **Same key, version bump.** The record keeps its `provider_event_id` and increases
   `record_version`.
2. **New key, explicit reference.** The record arrives under a new
   `provider_event_id` and carries `supersedes` naming the record it replaces;
   lineage is Phase 3's to resolve.

A changed payload at the same version is *not* a correction, even if `supersedes`
points at the event's own id; it is quarantined. Payload comparison covers every
stored version of a key, so re-delivering an older version after a newer one was
accepted is recognised as a duplicate rather than mistaken for a regression.

Billable retries are modelled as distinct `provider_event_id` values (same
`request_id`, different `attempt_id`). They are distinct keys and are always kept;
dedup removes re-delivery of the same event, never a retry the provider charged for.

A `supersedes` target that has not been seen is accepted and counted as
`dangling_supersedes`; out-of-order arrival is expected and Phase 3 owns resolution.

## 5. Rejection and quarantine reasons

Dead-letter (a record or a whole file is rejected, nothing is coerced):

| Reason | Meaning |
| --- | --- |
| `invalid_json` | file-level: the bytes are not valid UTF-8 JSON |
| `invalid_envelope` | file-level: envelope is not an object, is missing a required field, or a field has the wrong type/value |
| `unknown_field` | an unrecognised key, in the envelope or the event |
| `missing_events` | the envelope has no `events` key, or it is not a list |
| `payload_not_object` | an event is not a JSON object |
| `missing_required_field` | a required event field is absent |
| `wrong_type` | a field has the wrong type (a bool is never an integer token count) |
| `negative_token_count` | a token count is below zero |
| `invalid_timestamp` | a timestamp is not strict UTC ending in `Z` |
| `unknown_schema_version` | `schema_version` is not `1.0` |
| `unknown_enum_value` | `workload_type` is not one of the four values |
| `unavailable_marker_inconsistent` | the null/marker pair does not match, or a marker names a non-token field |
| `invalid_record_version` | `record_version` is not an integer `>= 1` |

Quarantine (a syntactically valid record is held rather than applied):

| Reason | Meaning |
| --- | --- |
| `same_key_changed_payload` | known key, changed payload, same or unorderable version, no accepted correction path |
| `version_regression` | known key, changed payload, lower `record_version` |

### 5.1 The malformed fixture mapping

`fixtures/phase1/batch-005-malformed.json` holds exactly nine events, each with a
single fault, at these indices:

| Index | Reason |
| --- | --- |
| 0 | `missing_required_field` (`feature` absent) |
| 1 | `unknown_field` (`prompt`) |
| 2 | `wrong_type` (token count is a string) |
| 3 | `wrong_type` (token count is a bool) |
| 4 | `negative_token_count` |
| 5 | `invalid_timestamp` (no trailing `Z`) |
| 6 | `unknown_schema_version` (`"2.0"`) |
| 7 | `unavailable_marker_inconsistent` (null, not marked) |
| 8 | `unavailable_marker_inconsistent` (marked, not null) |

`batch-007-invalid-json.json` is deliberately unparseable and produces one file-level
`invalid_json` dead-letter row with `event_index: null`.

## 6. Store layout

Everything lives under `var/`, which is gitignored. Nothing here is ever committed.

```text
var/
  raw/<content_sha256>.json          content-addressed copy of every delivered file
  state/events.jsonl                 append-only normalised events
  state/ingest-manifest.jsonl        one row per file delivery
  state/dead-letter.jsonl            one row per rejected file or record
  state/quarantine.jsonl             one row per quarantined record
  state/last-ingest-report.json      the report of the most recent run
```

JSONL rows are canonical JSON: sorted keys, no whitespace, ASCII only. Each stored
event is the validated event plus `provider`, `account_id`, `arrival_time` and
`payload_sha256` (the hash of the event as delivered, excluding those stamps). The
manifest row for a file carries its `content_sha256`, `size_bytes`, envelope identity,
`ingest_id`, `status`, `reason` and the count block. `ingest_id` is content-addressed
over the ordered content hashes of the delivered files, so it is stable for the same
deliveries.

## 7. Convergence, and what it does not promise

The manifest row is written **last** for each file and is that file's commit marker.
If a process dies between the row appends and the marker, a later run reprocesses the
file. Convergence then rests on deduplication:

- events dedupe on the fixup key's payload history (section 4);
- dead-letter and quarantine rows dedupe on `(content_sha256, event_index, reason)`.

Together that makes a clean re-run converge: the same rows are not appended twice.
It is **not** atomicity. A crash that leaves a torn JSONL line is a hard stop: the
store raises and the command exits non-zero, leaving the file untouched. There is no
truncation, no repair and no silent skip. True per-stage transactions arrive in
Phase 4.

Only files whose status is `applied` are recorded as ingested. A file that failed
validation therefore fails again on every re-delivery and is reported again, so each
delivery attempt stays visible in the manifest; its dead-letter rows are deduplicated,
so the dead-letter store does not grow. The practical consequence: across re-runs
`events.jsonl`, `dead-letter.jsonl` and `quarantine.jsonl` stay byte-identical, while
`ingest-manifest.jsonl` grows by one audit row per delivered file per run.

## 8. Data safety

Dead-letter rows retain the rejected record verbatim, including any unknown fields
and their values. That is the deliberate trade-off of this phase: the record is kept
for diagnosis rather than dropped. It is safe **only** because Phase 1 ingests
synthetic fixtures — there are no prompts, payloads, credentials or personal data in
the repository, and the hosted demo never sees this store.

Before any real provider data is ever ingested (the Phase 7 gate), redaction or field
stripping must be added first. That is a documented prerequisite, not a follow-up
nice-to-have: the current dead-letter path would otherwise preserve whatever unknown
field arrived.

## 9. Determinism

- One canonical serialiser for JSONL rows and one pretty serialiser for fixtures and
  the report, both with sorted keys.
- Delivery order is the CLI order; event order is file order; store order is append
  order.
- No wall clock, process id, host name or absolute path appears in any committed
  artifact. `arrival_time` comes from the envelope and `ingest_id` is
  content-addressed.
- A second run over the same inputs appends nothing to `events.jsonl`.

## 10. Commands

The package uses a `src/` layout, and `pytest.ini`'s `pythonpath = src` applies to
pytest only. Every module command must set the path first:

```powershell
$env:PYTHONPATH = "$PWD\src"

# regenerate the committed fixtures (must reproduce them byte for byte)
.venv\Scripts\python.exe -m ai_cost_margin.fixtures --out fixtures\phase1

# ingest a directory of top-level *.json batches, in sorted order
.venv\Scripts\python.exe -m ai_cost_margin.ingest --home var --dir fixtures\phase1

# ingest explicit files in an explicit delivery order (repeat --file)
.venv\Scripts\python.exe -m ai_cost_margin.ingest --home var --file fixtures\phase1\batch-001-clean.json
```

`--dir` is not recursive: only top-level `*.json` files in that directory are
delivered, so `fixtures/phase1/expected/` is never ingested. `--file` arguments are
delivered first, in the order given, then directories in the order given.

Exit codes: `0` means the run completed — dead-letters and quarantines are reported
outcomes, not crashes; `2` means a pre-flight or store failure (missing input path,
unreadable input, corrupt store, unwritable store).

## 11. Tests

| File | Covers |
| --- | --- |
| `tests/test_schema.py` | every validation reason, valid edge cases (explicit zero, unavailability, unattributable customer) |
| `tests/test_ingest_dedup.py` | duplicate file/e­vent delivery, billable retries, version bump, regression, A1 self-reference quarantine, A2 old-version re-delivery, A3 dead-letter and quarantine convergence |
| `tests/test_dead_letter.py` | the batch-005 reason mapping, the file-level `invalid_json` path, envelope failures, verbatim unknown-field retention |
| `tests/test_golden_ingest.py` | committed golden outputs byte for byte, two independent runs identical, re-ingest adds nothing, totals recounted independently from the fixtures |
| `tests/test_fixture_generator.py` | the committed fixtures are exactly the seeded generator's output |

## 12. Known limits

- No transactions: see section 7.
- Dedup cost is linear in stored events because the per-key index is rebuilt per run.
  Fine at demo scale; Phase 4 revisits it with staged checkpoints.
- Token counts are bounded only by Python's integer range; no upper limit is enforced.
- One `(provider, account_id)` pair per fixture batch; multi-account batches are not
  modelled yet.
- Lineage (`supersedes`) is recorded but not resolved. Phase 3 decides which version
  is current for aggregation.

## 13. Open questions (recorded, not guessed)

These do not block Phase 1, which is fixture-only, and must be answered against real
documentation before any real ingestion:

1. Does a real provider issue a new `provider_event_id` for each billable retry, or
   reuse one id per request?
2. Are cached tokens a subset of input tokens for billing purposes?
3. What is the real source of receipt time, if not the delivered batch?
4. Can a correction cross accounts, or is `supersedes` always intra-account?
