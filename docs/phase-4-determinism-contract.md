# Phase 4 determinism and recovery contract

Status: implementation contract for the amended Phase 4 plan (A1-A10) and rulings
R1-R9 of the Phase 4 build gate. It describes the behaviour the code and the
tests must hold; it does not restate Phase 1-3 contracts, which are unchanged.

The Phase 4 additions are one-command execution, transactional stage
checkpoints, resumable attempts, atomic publish, and a single-writer store. The
financial result is not redefined: the digest definition, the three financial
tables, the lineage rows, and the as-of cutoff semantics are exactly Phase 3.

## 1. Published result and operational metadata

A run publishes exactly seven files into `<out>/<run_fingerprint>/`:

```
customer-margins.jsonl
workload-margins.jsonl
unallocated.jsonl
lineage.jsonl
margin-report.json
margin-digest.txt
run-manifest.json
```

The financial result digest still covers exactly `customer_margins`,
`workload_margins` and `unallocated` (`ai-cost-margin/margin-digest/v1`). The
five files compared against the Phase 3 goldens are the three financial tables,
`lineage.jsonl` and `margin-digest.txt` (R2). `margin-report.json` and
`run-manifest.json` carry live provenance, so they are compared runner-to-runner
only. All compared files are root-independent: they contain no absolute path,
no host name, and the store locator is the constant relative string
`pipeline.duckdb`.

Operational metadata is banned from the seven files (R9). That ban covers the
attempt id, `invocation_sha256`, attempt start/finish and publish timestamps,
stage durations, publish state, the store file bytes, and any hash whose value
depends on delivery order. It does **not** cover contractual data timestamps:
`usage_time`, `arrival_time`, and the as-of cutoff keep their Phase 1-3 meaning
everywhere, including inside the seven files.

Adopted-stage counters (received, inserted, duplicates, quarantined,
dead-lettered, dangling supersedes, validated) are operational too (R1). They
live in `run_history` and the JSON run log. The published report schema is
unchanged by adoption.

## 2. Stages and transactions

The runner is the only writer. It opens `<home>/pipeline.duckdb` once, takes the
DuckDB writer lock, and performs the stale-run scan while holding that lock.
Each stage writes its own attempt-scoped rows and its `run_stages` checkpoint in
one transaction:

| stage | writes | checkpoint |
| --- | --- | --- |
| `raw` | `validated_events`, `ingest_manifest`, `dead_letters`, `quarantined_records`, `ingest_reports`, `raw_batches` | `run_stages('raw')` + attempt counters |
| `validated` | `stage_validated`, `stage_lineage` | `run_stages('validated')` + fingerprint |
| `costed` | `stage_costed` | `run_stages('costed')` + costing report |
| `aggregated` | `stage_customer_margins`, `stage_workload_margins`, `stage_unallocated`, `stage_artifacts` | `run_stages('aggregated')` + digest |
| `publish` | `published_run`, attempt status | `run_stages` already complete |

A stage transaction either commits every stage row and its checkpoint, or
nothing. A killed process therefore leaves no partial stage rows. The global
`validated_events` append is never deleted or rewritten; adoption copies a
previous attempt's stage rows under the new attempt id instead of re-executing.

The publish flip (the `published_run` pointer plus the attempt's published
status) is one transaction. There is no intermediate pointer state: the pointer
names the previous published attempt until the publishing transaction commits
(R8).

## 3. Attempt lifecycle (A2)

`run_history` is the attempt authority. An attempt row is created and committed
at the start of every invocation, before any stage runs, with:

- `attempt_id` - operational identifier;
- `invocation_sha256` - content hash of the *ordered* delivery list, cutoff,
  price-list and plan hashes, pipeline version, and code provenance;
- `status = 'running'`;
- `pre_raw_store_sha256` - the canonical event-stream digest before raw.

Resume matches `invocation_sha256`, never the run fingerprint. The invocation
hash is order-sensitive by design (R6): two delivery permutations are two
invocations that share one run fingerprint.

On the next invocation, the stale scan marks every still-`running` attempt as
`interrupted` and records the interruption time. The runner then selects the
most recent `interrupted` attempt with the same `invocation_sha256` and adopts
its longest verified committed prefix, in stage order:

1. **raw** is adoptable only when the recorded post-raw store digest still
   equals the live canonical event-stream digest. Adoption copies the previous
   attempt's `ingest_manifest`, `dead_letters`, `quarantined_records` and
   `ingest_reports` rows and its counters under the new attempt id, re-verifies
   the digest, and does **not** re-append events (R5). If the live store has
   changed, nothing is adopted and raw is recomputed against the current state.
2. **validated** is adoptable when its recorded input digest equals the live
   store digest; the copied stage rows must re-hash to the recorded output
   digest, or the attempt fails with `PipelineStateError`.
3. **costed** is adoptable when its recorded input digest equals the hash of the
   adopted validated output plus the price-list hash; rows are re-hashed.
4. **aggregated** is adoptable when its recorded input digest equals the hash of
   the adopted costed output plus the plans hash; table rows and artifact hashes
   are re-hashed.

`--from-stage` stops adoption before the named stage, so that stage and every
later stage are recomputed. `--no-resume` disables adoption entirely. Adoption
never changes the published bytes: an uninterrupted run, a resumed run, and a
run with recomputation produce the same seven files and the same digest for the
same invocation content and code provenance.

Counters adopted from a previous attempt are recorded in `run_history` only
(R1). The run log receives one `run_interrupted_detected` line per interrupted
attempt and one `run_completed` or `run_failed` line for the new attempt.

## 4. Atomic publish and exact replay (A3)

The runner stages the complete seven-file set in a DuckDB artifact table, then
writes it to an attempt-scoped temporary directory:

```
<out>/.tmp-<attempt_id>/
```

Only after every file exists is that directory renamed to
`<out>/<run_fingerprint>/`. The temp name is operational metadata and never
enters a run file. A leftover temp directory is inert evidence; nothing deletes
it.

A pre-existing final directory is never overwritten. If its file names and
bytes exactly match the new set, the write is an exact replay and the run
publishes normally. Any missing, extra, or different file is a
`ReplayMismatchError` before the publish pointer can change; nothing is repaired
and nothing is deleted.

The legacy `run_margins` path keeps its Phase 3 direct-write behaviour byte for
byte, including its partial-directory failure mode, because it never passes an
attempt token.

## 5. Kill points and assertion levels (A4/A5)

Three audited kill points exist, selected only by the drill environment variable
`AI_COST_MARGIN_KILL_POINT` and implemented as `os._exit(137)`:

| kill point | where | committed prefix | surviving side effect |
| --- | --- | --- | --- |
| `before_raw_commit` | raw transaction, after ingestion, before commit | none | inert content-addressed raw copies only |
| `before_costed_commit` | costed transaction, after costing, before commit | raw + validated | none beyond those commits |
| `after_publish_rename` | after the directory rename, before the publish transaction | raw + validated + costed + aggregated | the unreferenced run directory |

Each point asserts, at its own level:

1. no partial stage rows for the interrupted attempt;
2. one `interrupted` attempt row with the correct `last_completed_stage`
   (`null` for point 1, `validated` for point 2, `aggregated` for point 3);
3. the resumed run's digest and all seven files byte-identical to an
   uninterrupted clean run;
4. for points 1 and 2, digest equality with the pinned golden digest;
5. for point 3, the published pointer still names the earlier X attempt during
   the kill window, and the resumed Y digest and seven files are byte-identical
   to an uninterrupted Y run in a sibling fresh root, with Y's digest different
   from X's.

Reordering permutations are restricted to the conflict-free delivery subset.
Batches 003 and 004 depend on state produced by batch 001 (changed payloads and
a version correction), so every permutation keeps batch 001 before them. The
permutable subset is batch 002 and batches 005-007 (retry duplicates,
malformed events, unavailable fields, invalid JSON). Permuted runs use separate
`--out` roots for byte comparisons (R6).

## 6. Store provenance (A7)

The committed DuckDB working store has one canonical event stream: every stored
event serialised by `canonical_json`, sorted by serialised text, LF-terminated,
then SHA-256 hashed. That digest is `store_events_sha256` in the runner
manifest.

Two provenance shapes exist and they never mix:

- the legacy JSONL shape emitted by `run_margins`:
  `store_events_file` (`events.jsonl`), `store_events_sha256` (the file's byte
  hash), `events_in_store`, `events_included`, `events_excluded_by_cutoff`;
- the backend-neutral runner shape:
  `store_backend = "duckdb"`, `store_locator = "pipeline.duckdb"`,
  `store_events_sha256` (the canonical stream digest), `events_in_store`,
  `events_included`, `events_excluded_by_cutoff`.

The seven committed Phase 3 goldens stay byte-exact because the legacy shape is
untouched. The runner's `run-manifest.json` is never compared to a golden.

## 7. Query discipline (A8)

Only runner-internal queries are in scope. The publish pointer is the
visibility authority for the runner: a query issued by the runner sees the
previous published attempt, or none, never a staged attempt. No reader surface
outside the runner is defined or changed in Phase 4; Phase 3's CLI outputs are
unchanged.

## 8. Lock and open errors (A10/R4)

The single-writer guarantee is **cross-process**. It is enforced by the OS file
lock DuckDB takes when it opens the database file: a second pipeline runner in
another OS process cannot open the same store until the first process closes
it. Within one process, connections to the same file share one DuckDB instance,
so the lock does **not** serialize two runner invocations in the same process.
The supported usage is one pipeline runner invocation per OS process. A test
that opens a second same-process connection documents this boundary; it is not
enforcement.

`connect_store` classifies only errors raised while **opening** the database:

- a `duckdb.IOException` whose message contains the constant phrase
  `being used by another process` (the Windows sharing-violation form observed
  at runtime), or both `Could not set lock on file` and
  `Conflicting lock is held` (DuckDB's POSIX form, not observed on this
  machine, kept for portability), maps to `PipelineBusyError` (CLI exit 2,
  with a message naming the write lock);
- permission errors, corruption, a missing directory, and every other open
  failure map to `StoreOpenError`, preserving the original message. They are
  never reported as "busy".

Matching uses only those constant phrases. The variable holder detail is never
matched on; when present it is carried verbatim in the `PipelineBusyError`
detail. The observed contention literal has this shape (placeholders only; no
real path or process id ever appears in this contract):

```
IO Error: Cannot open file "<path>": The process cannot access the file because it is being used by another process.
File is already open in <executable> (PID <n>)
```

The verbatim observation from the build probe is recorded in the build
evidence report, not in this contract.

## 9. JSON run log (R8)

`<home>/logs/runs.jsonl` is observability, not correctness. If an append fails
after the `run_history` commit, the runner raises `RunLogError`, which the CLI
reports as exit 2. The committed run record is never rolled back because of a
log failure. The publish transaction itself commits or rolls back as one unit;
the log is not part of it.

## 10. Exit codes

| code | meaning |
| --- | --- |
| 0 | run published, or exact replay verified, or `--status-json` printed |
| 2 | typed input, store, lineage, replay, lock, or artifact error; no partial publish |
| 137 | drill-only immediate termination at an audited kill point |

## 11. Determinism statements

For the same delivery bytes, cutoff, price list, plan list and code
provenance:

- the run fingerprint is identical across delivery permutations;
- the digest and the five golden files are identical across delivery
  permutations and across interruption/resume;
- `margin-report.json` and `run-manifest.json` contain only ordered or sorted
  collections, so they are identical across delivery permutations as well;
- `invocation_sha256` is intentionally not identical across delivery
  permutations, because it identifies the exact ordered invocation;
- the seven published files never contain operational metadata, and the
  committed goldens are unaffected by this phase.
