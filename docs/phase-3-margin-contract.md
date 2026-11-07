# Phase 3 contract: aggregation, corrections lineage and simulated margins

Status: implemented by the Phase 3 build gate; not committed or pushed.

This contract fixes what a Phase 3 margin run means. It is deliberately narrow:
usage stays synthetic, cost stays estimated from the synthetic price list, and
revenue and margin are **simulated**. Nothing here is a real customer, a real
price or a real income claim.

## 1. Scope and non-goals

In scope:

- an as-of cutoff over the append-only Phase 1 store;
- the Phase 2 supersession rules, made visible as lineage edges;
- customer-month and customer-month-workload-type aggregation;
- flat monthly customer plans and exact proportional revenue allocation;
- an explicit unallocated bucket for usage with `customer_id: null`;
- deterministic run artifacts, a result digest and replay checking.

Out of scope, and deferred:

- DuckDB, atomic publish, checkpoints and crash recovery (M4);
- real revenue, invoicing, proration, taxes, discounts or refunds;
- per-feature, per-model or per-day margin tables;
- the local app and any hosted deployment;
- diagram regeneration (see section 12).

## 2. Inputs

A run reads exactly three inputs:

1. the Phase 1 store `state/events.jsonl` under `--home`;
2. a Phase 2 synthetic price list (`--prices`);
3. a Phase 3 simulated plan file (`--plans`).

The plan file is a JSON object with `plans_version_id`, `label`, `currency`,
`timezone`, optional `notes`, and `entries`. `label` must be `simulated`,
`timezone` must be `UTC`, and each entry has `customer_id`, `plan_id`,
`monthly_price`, `effective_from` and `effective_to`.

Plan validation is fatal before any event is read: unknown fields, a non-
`simulated` label, malformed money, a non-UTC timezone, duplicate interval
starts, overlapping intervals, gaps between intervals, or a non-final open-ended
interval all stop the run with exit code 2. The plan currency must equal the
price-list currency; a mismatch is fatal (ruling R3). A plan month is selected
by the **first instant of the UTC month**, and no proration is applied: an
active customer-month is charged the full monthly price once.

## 3. As-of cutoff and lineage

`--cutoff` is a strict UTC timestamp. A stored record is included exactly when
its envelope `arrival_time` is less than or equal to the cutoff. `usage_time`
remains the period and price-selection field; it never decides inclusion.

Supersession uses the two Phase 2 rules and nothing else:

- a lower `record_version` for the same `(provider, account_id,
  provider_event_id)` key;
- a different event id under the same `(provider, account_id)` that names a
  target in `supersedes`, unless the record names itself.

A record never supersedes itself. More than one current record at the same
highest `record_version`, more than one claimant for one `supersedes` target, or
a `supersedes` cycle is a fatal lineage ambiguity (exit 2). A `supersedes`
reference whose target is not stored is not fatal: it produces a visible
`dangling` lineage edge and the claimant remains current.

Every lineage edge is directed **superseded -> superseding**:

- `superseded_event_id` / `superseded_record_version` name the replaced record;
- `superseding_event_id` / `superseding_record_version` name its replacement.

A dangling edge has `status: "dangling"` and a null superseded record version.

## 4. Aggregation

Current records are `costed` or `unpriced` rows from the Phase 2 engine.
Superseded rows are never costed and never create a margin cell.

- A **customer-month** groups current records by `usage_time[:7]` and
  `customer_id`. Only periods with at least one current record produce a row.
- A **workload cell** groups one customer-month further by `workload_type`.
  Only workload cells with at least one current record produce a row.
- A customer-month with only superseded records produces no customer or
  workload row; its superseded count is reported in
  `superseded_events_outside_active_cells`.
- A current customer with no plan covering the month is fatal
  (`UnknownPlanError`, exit 2, ruling A3). There is no `unplanned_customer`
  bucket and no invented revenue.

Customer costs are exact sums of Phase 2 `total_cost` strings. A customer-month
is `complete` only when it has at least one costed row and no unpriced rows;
otherwise it is `incomplete`. A null estimated cost stays null; it is never
silently replaced by zero.

## 5. Simulated revenue allocation

A customer-month with current usage receives the full monthly plan price as
`simulated_revenue`. Revenue is allocated to its workload cells by the billable
token volume of **current costed events only**:

```text
weight(cell) = sum(tokens_input + tokens_output)
               over current costed events in that cell
```

Unpriced events never contribute a weight, even when their token counts are
known. Allocation is exact integer largest-remainder apportionment at the
10^-12 currency unit:

- integer quotient and remainder per cell over the total weight;
- leftover units are assigned one at a time by descending remainder;
- ties break in the fixed order `cache_heavy`, `long_heavy`, `short_light`,
  `spiky`, then by workload name for any unknown type.

The allocated amounts always sum to exactly the customer's simulated revenue.
A cell with zero weight receives an exact zero when total weight is positive.

### Allocation status and precedence (ruling R1)

`revenue_allocation_status` is one of:

- `allocated`: total weight is positive and no unpriced usage was excluded;
- `incomplete_unpriced_excluded`: total weight is positive, but the
  customer-month contains at least one unpriced event; **every** workload cell
  in that customer-month is marked incomplete because excluding an event shifts
  the whole proportional split;
- `basis_unavailable`: total costed token weight is zero. This takes precedence
  over `incomplete_unpriced_excluded`, and the cell has null revenue and null
  margin.

`margin_status` is `incomplete` for `incomplete_unpriced_excluded` and
`basis_unavailable`; it is `complete` only for an `allocated` cell that has no
unpriced events and a non-null cost.

## 6. Unallocated usage

Current records with `customer_id: null` are never charged to a plan. They are
reported in their own bucket keyed by period and workload type, with counts,
cost and unpriced reasons, and with no simulated revenue or margin.

The run reports, in exact integer units:

- `unallocated_cost_units` and `total_cost_units` (10^-12 currency units);
- `unallocated_events_costed` and `events_costed`;
- `unallocated_cost_share_bp` and `unallocated_event_share_bp`, each an exact
  round-half-up integer share in basis points.

No code-enforced threshold decides whether an unallocated share is acceptable
(ruling A2). The review gate judges the reported shares and their raw units.

## 7. Code-version provenance (ruling A4)

Production runs resolve provenance with read-only git probes:

- `git rev-parse HEAD` must return a 40-character commit hash;
- `git status --porcelain=v1 --untracked-files=all` must be captured.

A clean tree records `working_tree_clean: true`, a null
`dirty_status_sha256` and `code_version_source: "git_head"` with the real HEAD.
A dirty tree still records the real HEAD, plus `working_tree_clean: false` and a
content-honest `dirty_status_sha256`. It never refuses to run and never claims
clean provenance.

The dirty hash covers the canonically sorted list of `(path, status, content
SHA-256)` for every porcelain entry. Untracked files appear individually, never
as directories. A deleted path contributes a fixed marker instead of a content
hash. Rename entries contribute both paths deterministically in porcelain-v1
order. Identical dirty paths/status with different file contents therefore
produce different hashes and different run fingerprints.

Golden and test runs use the internal `code_version_override` path with the
fixed literal `fixtures-phase3-golden-v1` and `code_version_source: "fixture"`.
The production CLI has no override flag and always probes git.

## 8. Run fingerprint

The run fingerprint is a SHA-256 over the canonical JSON of:

- the artifact schema version and aggregation definition id;
- the as-of cutoff;
- the code-version fields above;
- the SHA-256 of the price-list file and the plan file;
- the SHA-256 of the canonical sorted included-event records (key, record
  version, payload hash, arrival time and usage time).

It identifies one logical run and its provenance. It does not include a wall
clock, a host name, a process id or an absolute path.

## 9. Artifact layout

A production run writes one directory:

```text
var/phase3-runs/<run_fingerprint>/
  customer-margins.jsonl
  workload-margins.jsonl
  unallocated.jsonl
  lineage.jsonl
  margin-report.json
  margin-digest.txt
  run-manifest.json
```

`--out` is the run root; the fingerprint subdirectory is created under it.
Table rows are canonical compact JSON, one per line, with `\n` line endings.
The manifest is written last in the write order.

## 10. Result digest

The digest definition is `ai-cost-margin/margin-digest/v1`. Its input is exactly
three financial tables, in this order, each sorted by its defined key:

1. `customer_margins`, sorted by `(period, customer_id)`;
2. `workload_margins`, sorted by `(period, customer_id, workload_type)` using
   the fixed workload order;
3. `unallocated`, sorted by `(period, workload_type)` using the fixed workload
   order.

The byte stream is:

```text
ai-cost-margin/margin-digest/v1\n
table customer_margins <n>\n
<canonical JSON row>\n
...
table workload_margins <n>\n
...
table unallocated <n>\n
...
```

The digest is the lowercase SHA-256 of that stream. It excludes run ids,
timestamps, code-version metadata, reports, manifests and lineage rows, so two
runs with the same financial result have the same digest even when their
provenance differs.

`margin-digest.txt` is two lines: the definition id, then the 64-hex digest.

## 11. Determinism, replay and partial output

Validation and every output byte are computed before the first write. A
validation error therefore writes nothing. Rows are emitted in the defined sort
order, so identical inputs produce byte-identical artifacts.

If the fingerprint directory already exists, the run does not overwrite it. It
reads every expected file and compares bytes. An exact replay exits 0 and
reports `exact replay verified; no files overwritten`; any missing, extra or
different file is a loud replay mismatch and exit 2.

A mid-write OS failure can leave a partial run directory. Nothing is deleted or
repaired. The next identical run detects the partial directory through the same
byte comparison and exits 2. Atomic publish remains M4 scope.

## 12. Diagrams and evidence

The architecture and workflow diagrams are **not** regenerated in this gate.
Their Excalidraw sources and PNG renders still describe through M2. Diagram
regeneration is explicitly deferred to a separate visual pass before any
commit/push gate, and this deferral is recorded here so the README is not read
as a claim that the images already show M3.

The Phase 3 build gate writes only the new source modules, fixtures, goldens,
tests, this contract and the README wording edit. It performs no staging,
commit, push, build-log edit, install, network call or submission.

## 13. Labels and honest limitations

Every margin row and report carries:

- `usage_label: "synthetic"`;
- `cost_label: "estimated"`;
- `revenue_label: "simulated"`;
- `margin_label: "simulated"`.

Known limitations:

- plans are synthetic flat monthly prices, not real contracts;
- no proration, discounts, taxes, credits or refunds;
- revenue is allocated by token volume, not by measured value or service cost;
- a customer-month with any unpriced usage cannot produce complete workload
  margins, because the excluded event can change the proportional split;
- the store remains JSONL with a torn-line hard stop until M4;
- golden provenance is a fixed fixture literal, not the future commit that
  contains it.
