# Build Log - AI Cost & Margin App

Entirely append-only record of build progress, tests, provenance, and tool transitions. New entries go at the END of this file. Nothing above a new entry is ever edited: no deletions, no corrections in place, no backfilling placeholders. The latest complete entry is the authoritative current state.

Keep every entry sanitized: short summaries and references to evidence (command, pass/fail counts, paths), never raw output dumps, never secrets, tokens, or key material. This file may become public with the repo after an approval, so write it for a public reader.

---

## Entry template (copy for each new entry)

### Entry N - YYYY-MM-DD HH:MM IST

- Phase:
- Tool/model:
- Verified base (last commit verified by review):
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base):
- What changed:
- Tests run (command -> short result summary, author-reported until review):
- Provenance/data notes (which fixtures or inputs were touched):
- Commits produced (hashes pasted from `git log`):
- Log commit (hash of the commit that added the PREVIOUS entry):
- State after this entry: planned / in progress / pushed - awaiting review / changes requested / verified at <hash>
- Blockers:
- Next step:

Rules:

1. Append-only. A correction or a resolved pending item is a NEW entry; old entries stay untouched.
2. An entry records only hashes of commits that already exist when it is written. The commit carrying the entry itself is recorded in the NEXT entry's "Log commit" field. A commit cannot contain its own hash.
3. Hashes come from real git output only (`git log --oneline`, `git rev-parse HEAD`), never from memory or from a tool's claim alone.
4. Test results are short summaries of real output and are author-reported until a review of the exact pushed commit verifies them.
5. `verified` is granted only by review of the exact pushed commit.
6. Tool switches get their own entry: from-tool and model, to-tool and model, reason, verified base and actual HEAD, branch, the exact list of modified/staged/untracked file names (names only, no contents), done vs pending, the SHA-256 fingerprint hashes of the exported patches and untracked files (hashes only), and a backup label (a private label like "private backup folder, switch <date>", never an absolute home path).

---

### Entry 0 - 2026-11-07 (project not started)

- Phase: 0 - Repo and environment foundation
- Tool/model: (none yet)
- Verified base: (none; repo existence is not established yet)
- HEAD at work start: (none)
- What changed: build log created from the starter template, alongside the project brief and the implementation roadmap.
- Tests run: none yet.
- Provenance/data notes: none.
- Commits produced: (none yet; recorded in the next entry, never backfilled here)
- Log commit: (none)
- State after this entry: not started
- Blockers: Phase 0 has no approved scope yet.
- Next step: await the Phase 0 scoped brief. Repo creation, remote creation and visibility, dependency installs, and the first commit/push each need their own reviewed approval first; the roadmap alone approves none of them.

---

### Entry 1 - 2026-11-07 14:40 IST

- Phase: 0 - Repo and environment foundation
- Tool/model: DeepSeek Harness (deepseek-flash)
- Verified base (last commit verified by review): none (repo bootstrap)
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base): none (0 commits)
- What changed: Phase 0 scaffold committed on main in C1-C3 - repo hygiene (.gitattributes, .gitignore), pinned requirements.txt, pytest configuration and the empty package, project brief and roadmap under docs/, build log starter, smoke test and README. Then a clean clone was installed and tested to prove the committed tree is runnable from a fresh checkout.
- Tests run (command -> short result summary, author-reported until review): `py -3.13 -m venv .venv` -> exit 0 in the original repo and in the clean clone. `pip install --only-binary :all: -r requirements.txt` -> exit 0, 40 distributions, wheels only, in both environments. `python -m pytest` -> 5 passed, 0 failed, exit 0 in the original venv (0.47s) and in the clean clone (0.50s). `pip list --format=freeze` compared live between the two venvs -> identical, 41 name==version entries each. All results are author-reported; none has been independently verified by review. The clean-clone run is a second local environment check on this machine, not an external verification.
- Provenance/data notes (which fixtures or inputs were touched): no fixtures and no provider data. This phase touched dependency resolution and an in-memory DuckDB decimal check only; project code made no network or provider calls.
- Commits produced (hashes pasted from `git log`): 96cf338 (C1, scaffold, ignore rules and pinned dependencies), 0dafff8 (C2, project brief, roadmap and build log starter), 0ec1eaa (C3, smoke test and README).
- Log commit (hash of the commit that added the PREVIOUS entry): 0dafff8 (C2 added Entry 0).
- State after this entry: in progress
- Blockers: none
- Next step: await the push gate approval

---

### Entry 2 - 2026-11-07 15:36 IST

- Phase: 1 - Schema, fixtures and ingestion foundation
- Tool/model: DeepSeek Harness (deepseek-flash)
- Verified base (last commit verified by review): ef1a153 (Phase 0 log commit; no Phase 1 commit is review-verified yet)
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base): ef1a153 at phase start; c9b9fca (P1-C3) at the start of the remediation gate
- What changed: Phase 1 built the ingestion foundation on synthetic fixtures only. C1 to C3 added the versioned event contract with an explicit unavailable-field marker, the seeded fixture generator and its seven committed fixture files, ingestion with a content-hashed manifest, dead-letter and quarantine, the ingestion contract document, the test suite with frozen golden outputs, and the README update. The first clean-clone check failed at the clone pytest step (2 of 55 tests, byte comparisons, CRLF versus LF on checkout) and nothing was fixed in that run. Remediation was a one-line .gitattributes change, `* text=auto` to `* text=auto eol=lf` (C4), so checkout is LF regardless of a client's git settings; a second clean-clone check then passed every step.
- Tests run (command -> short result summary, author-reported until review): original venv `python -m pytest` -> 55 passed, exit 0. First clean clone `python -m pytest` -> 2 failed, 53 passed, exit 1 (CRLF versus LF byte comparisons; no fix applied in that run). Second clean clone: `pip install --only-binary :all: -r requirements.txt` -> exit 0; `pip freeze` identical between the two venvs (40 entries each, 0 differences, all three direct pins exact); LF checkout proof -> 23 of 23 reviewed files matched; `python -m pytest` -> 55 passed, exit 0; fixture regeneration -> 7 of 7 matched; golden ingest -> 5 of 5 outputs matched. All results are author-reported and not independently verified by review; the clean-clone runs are second local environments on one machine, not external verification. An initial test assertion was corrected before review; future failed checks stop for review, with no cleanup.
- Provenance/data notes (which fixtures or inputs were touched): synthetic fixtures only; no provider, customer or invoice data and no live calls. The private store stays under var/ and is gitignored, and the seeded generator reproduces every committed fixture byte for byte.
- Commits produced (hashes pasted from `git log`): f8a3667018f292acb179ac1e76aa3c064fbfcb78 (C1, event schema, fixture generator and committed fixtures), a99c9cf8b89e6491710076909c2f132fa85cc2e8 (C2, ingestion with manifest, dead-letter and quarantine), c9b9fca0ab1702f34059ff89b3178abefca787f4 (C3, ingestion tests and golden outputs), 4e99fde2f6a19688ee2f04b229b01d96f0a3c281 (C4, enforce LF checkout in .gitattributes)
- Log commit (hash of the commit that added the PREVIOUS entry): ef1a15351fc8d2e40d048cb47455a3b2ee23f573 (the commit carrying Entry 1)
- State after this entry: in progress
- Blockers: none
- Next step: await the push gate approval

---

### Entry 3 - 2026-11-07 18:41 IST

- Phase: 2 - Pricing engine with synthetic prices
- Tool/model: DeepSeek Harness (deepseek-flash)
- Verified base (last commit verified by review): aa2495b (Phase 1 log commit; no Phase 2 commit is review-verified yet)
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base): aa2495b (P1-C5) at phase start
- What changed: Phase 2 added a synthetic pricing engine over the Phase 1 store. C1 added the exact decimal money type, the price-list model with fatal interval validation, the synthetic price list with its four negative fixtures, and the pricing contract document. C2 added the costing engine with the unpriced bucket and per-unit price-version provenance. C3 added the pricing test suite, the timestamp-parity and non-UTF-8 regression tests, the frozen golden costed outputs, and the README wording correction. Two scoped repair gates preceded commit. A command abort was followed by a corrected retry without approval; future command errors stop for review.
- Tests run (command -> short result summary, author-reported until review): original venv `python -m pytest` -> 172 passed, exit 0. Clean clone: `py -3.13 -m venv` then `pip install --only-binary :all: -r requirements.txt` -> install exit 0; `pip freeze` identical between the two venvs (40 entries each, resolved Python 3.13.3 in both) with all three direct pins exact; `python -m pytest` -> 172 passed, exit 0; golden ingest -> 19 rows, state/events.jsonl 10479 bytes and hash-equal to the committed expected file; golden costing -> 15 costed, 2 unpriced, 2 superseded, both outputs byte-identical to the committed goldens; five negative checks (four invalid price lists and one non-UTF-8 file) each exited 2 with no output directory created. All results are author-reported and not independently verified by review; the clean clone is a second local environment on one machine, not external verification.
- Provenance/data notes (which fixtures or inputs were touched): synthetic price list and synthetic fixtures only; no provider rates, no live calls and no customer data. The working store stays under var/ and is gitignored; every cost is labelled estimated and every price list is labelled synthetic.
- Commits produced (hashes pasted from `git log`): 5229dfd3ac2953f25ea132f3e84247eaae69625d (C1, decimal money and price-list model with interval validation), 2be7c7c5956a2badb1e36d9b91c70e2c9311a982 (C2, costing engine with unpriced bucket and price-version provenance), 6b4f1ff76d55a0f6b4e735d23e1b9be029f41201 (C3, pricing tests and golden costed outputs)
- Log commit (hash of the commit that added the PREVIOUS entry): aa2495b78b9380ed882f6b9e01c11e4a2c6bc749 (the commit carrying Entry 2)
- State after this entry: in progress
- Blockers: none
- Next step: await the push gate approval

---

### Entry 4 - 2026-11-07 21:55 IST

- Phase: 3 - Aggregation, margins and run provenance (M3)
- Tool/model: OpenCode (model not recorded)
- Verified base (last commit verified by review): e455738f4f7b887fbf54ac691b9b6ebca4f3bf1c (verified by the Phase 2 closeout review)
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base): e455738f4f7b887fbf54ac691b9b6ebca4f3bf1c at phase start
- What changed: C1 added usage aggregation per customer and workload type with an explicit unallocated bucket. C2 added run versioning, corrections lineage and code-version provenance, with late-arrival and late-correction fixtures. C3 added the simulated plan model with invalid-plan fixtures, the margin tables, the canonical digest and the golden expected outputs. C4 added the hand-calculation script, the margin contract document, the README test-count wording correction and re-rendered architecture and workflow diagrams showing M3 as built. Review caught a bare test count in the architecture diagram legend; the legend was corrected and re-rendered before commit. During placement of the four reviewed diagram files, a copy step was paired with an exit-code check that does not apply to that command; the check raised and the run continued instead of stopping for review. All four transferred files were hash-verified immediately afterwards and matched exactly; future command errors stop for review.
- Tests run (command -> short result summary, author-reported until review): original venv `python -m pytest` -> 248 passed, 1 skipped (the explicit golden-generation check), exit 0. The replay and hand-calculation comparisons are author-reported from the same local run; the suite was not re-run independently. Independent review of the exact pushed commit verified: every source file byte against the reviewed bundle and the published base; the golden margin digest recomputed byte-exact; allocation sums and the revenue and allocation rules against the margin contract; each commit's tree hash against independently computed trees from the reviewed bytes; the published file bytes on the remote; and the rendered README, diagrams and repository metadata on the published page. The original work-start working-tree byte map was not recoverable from the session record, so byte preservation during the build window is not claimed; the review verified the final bytes against the reviewed bundle and the published base.
- Provenance/data notes (which fixtures or inputs were touched): synthetic customer plans, synthetic late-arrival and late-correction batches, invalid plan fixtures and golden expected outputs only; no real customer data, no real provider rates and no live calls. Golden fixtures pin the fixed fixture code-version label fixtures-phase3-golden-v1; the production CLI always probes code version from git. Revenue is labelled simulated and every cost is labelled estimated; the working store stays under var/ and is gitignored.
- Commits produced (hashes pasted from `git log`): c9e90b01466542728e9036964660dffff758d678 (C1, aggregation and unallocated bucket), 88ddbe2825cc1ffa23aff0250e9446ef9eef9a07 (C2, run versioning, lineage and code-version provenance), b220443384eac4743b417311d2df582f2a650d8e (C3, margin tables, digest and golden fixtures), e3096562ff6d4bbc74cfef48ca0e52a0d0d97835 (C4, hand-calculation, contract, README and diagram updates)
- Log commit (hash of the commit that added the PREVIOUS entry): 7dfd531aa223936bbe62f8e762efd5cf62a13351 (the commit carrying Entry 3)
- State after this entry: verified at e3096562ff6d4bbc74cfef48ca0e52a0d0d97835
- Blockers: none
- Next step: Phase 4 planning (M4: DuckDB store, atomic publish, crash recovery) in Plan mode.

---

### Entry 5 - 2026-11-08 03:18 IST

- Phase: 4 - Determinism and recovery (M4)
- Tool/model: OpenCode (model not recorded)
- Verified base (last commit verified by review): 99ea22b554b4fa1785519d1c3d40514317688488 (verified by the Phase 3 closeout review)
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base): 99ea22b554b4fa1785519d1c3d40514317688488 at phase start
- What changed: the amended Phase 4 plan (A1-A10) added the one-command runner. Four source files were modified (ingest, margins, runs, store: the A6 adapter seam, the backend-neutral manifest shape and the attempt-scoped publish) and eleven files were added (the DuckDB working store with transactional stage checkpoints, resumable attempts, atomic publish and the publish pointer; the store adapter; the pipeline runner; the determinism and recovery contract; the three-point kill drill; six test files). Process record: the first build stopped at the lock probe when the planned lock phrases did not match the observed Windows runtime and a same-process contention assumption proved wrong; the mapping was corrected to the observed cross-process sharing-violation classification, and the contract now states the cross-process-only boundary plainly. A Plan-mode source check caught the same wrong assumption in the CLI busy test before the correction ran. The first full suite then surfaced five failures in the never-before-run Phase 4 tests (three test transaction setups, one test expectation contradicting the A2 resume rule, and a Windows path-length overflow in the attempt temp-directory name); a bounded four-file repair followed, a Plan check corrected one assertion sequence in that repair, and the final verification passed.
- Tests run (command -> short result summary): all runtime results below are author-reported from the gated local runs; the independent review did not rerun them. venv `python -m pytest` -> 311 passed, 1 skipped (the explicit golden-generation check), 312 collected, exit 0. The one-command run against a fresh evidence root printed the pinned golden digest 39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0, the five compared golden files matched byte for byte, an exact replay was reported not rewritten, and the three-point kill drill passed. Static checks by the independent review: the source export's eleven untouched files matched the pre-repair hashes byte for byte and its four v3 edits landed exactly; the final bundle's eleven untouched files matched the reviewed export byte for byte and its four v4 edits landed exactly, with every byte outside the scope preserved; the exported 86-file baseline and first stopped-state records matched their reported hashes. The base 82-file comparison and the second stopped-state record are author-reported local results only, not exported or reverified.
- Provenance/data notes (which fixtures or inputs were touched): synthetic Phase 1-3 fixtures only; no real customer data, no real provider rates and no live calls. The committed Phase 3 goldens stay byte-exact; the runner's manifest uses the backend-neutral store shape (`store_backend = "duckdb"`, `store_locator = "pipeline.duckdb"`) and is never compared to a golden. The working store, run history and evidence roots stay under var/ and are gitignored.
- Commits produced (hashes pasted from `git log`): 64fe19d964ac2a113d834ed86b073bda15a68cc5 (M4: one-command runner, determinism and recovery contract, README and diagram updates)
- Log commit (hash of the commit that added the PREVIOUS entry): 99ea22b554b4fa1785519d1c3d40514317688488 (the commit carrying Entry 4)
- State after this entry: committed at 64fe19d964ac2a113d834ed86b073bda15a68cc5; independent review pending
- Blockers: none
- Next step: Phase 5 planning (M5: local Streamlit app) in Plan mode.

### Entry 6 - 2026-11-08 (Phase 5 closeout record)

- Phase: 5 - Interactive app, local (M5)
- Tool/model: OpenCode (model not recorded)
- Verified base (last commit verified by review): b02922e8beb15989c88fd21db78f7b3406cba5d7 (after the push, the public commit, its parent 9ae359543c2cb4b4b510583ed3b0e463366d51b3 and its two test blobs were independently checked against the reviewed bytes; the app, scenario and export files were byte-reviewed before their commits; this is an identity and bytes check, not a full closeout review of Phase 5)
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base): 06dcc8d59cff36be3b2ac08612fa6b042a3c7b51 at phase start (the Phase 4 log commit); b02922e8beb15989c88fd21db78f7b3406cba5d7 at the start of this documentation entry
- What changed: C1 added the export generator and the reviewed eight-file dataset under app/export/ (two distinct digests, surrogate evt-NNNN event keys). C2 added the review app and the verified export loader. C3 added the what-if scenarios, recomputed through the same engine, and the provenance panel. C4 added 12 persistent tests: 5 scenario-engine tests and 7 app and loader tests (6 AppTest page tests, including two-session isolation and the app's refusal of a tampered export, plus 1 direct test that the export loader refuses a tampered copy). Several gated steps stopped on instruction or harness defects and were reported and corrected under scoped continuations before the affected commits.
- Tests run (command -> short result summary): all results are author-reported; the independent review did not rerun them on the user's PC. venv `python -m pytest` -> 332 collected, 331 passed, 1 skipped (the explicit golden-generation check) on the user's Windows PC. In one Linux reviewer environment the 12 new tests passed and one existing test, tests/test_run_store.py::test_lock_real_second_writer_process_is_busy, failed; it passed on the user's PC. This is recorded as an observed baseline, not investigated and not changed here.
- Provenance/data notes (which fixtures or inputs were touched): synthetic fixtures only; the committed export is sanitized (surrogate event keys, no raw provider event IDs, account IDs, request IDs, provider attempt IDs or payload hashes); working stores stay under var/ and are gitignored; no screenshots or other image files are committed for Phase 5.
- Commits produced (hashes pasted from `git log`): 8424f0f9808e4b5b617b3a00701d0966efabbb5a (P5-C1: export generator, tests and reviewed export dataset), e30bf35489b3c116de2a7b7b812c6c494e365320 (P5-C2: export review app and verified bundle loader), 9ae359543c2cb4b4b510583ed3b0e463366d51b3 (P5-C3: what-if scenarios and provenance sections), b02922e8beb15989c88fd21db78f7b3406cba5d7 (P5-C4: tests for what-if scenarios and provenance)
- Log commit (hash of the commit that added the PREVIOUS entry): 06dcc8d59cff36be3b2ac08612fa6b042a3c7b51 (the commit carrying Entry 5)
- State after this entry: pushed - awaiting review (this refers to the four published Phase 5 commits C1 to C4; this entry and the matching README update are not yet committed, and the commit that carries them is recorded in the next entry's Log commit field)
- Blockers: none
- Next step: CI verification, then Phase 6 planning (M6: hosted deployment, audience decision and hosting terms first) in Plan mode. The license choice and screenshot publishing are optional separate user decisions and do not block either.

### Entry 7 - 2026-11-08 (CI, hosted demo and documentation catch-up)

- Phase: 5 follow-through - CI, the hosted demo (M6) and the README, diagram and screenshot update
- Tool/model: OpenCode (model not recorded)
- Verified base (last commit verified by review): 2a224106bf1fad0a0db1165d6f2fac6f32fa86d9 (public main, parent 667875b8df6764df0b063ed2cc210a30888f3548; the commit, its parent and the workflow blob 0e0bb81b7062cdc5cf444668cf503d7117e76042 were independently checked through the public GitHub API after the push; this is an identity and bytes check, not a full closeout review)
- HEAD at work start (may include unverified commits; a dirty mid-phase HEAD is not a verified base): 2a224106bf1fad0a0db1165d6f2fac6f32fa86d9
- What changed: the CI commit added .github/workflows/ci.yml, one Windows job on Python 3.13 that installs the pinned requirements and runs pytest (2a22410). Its first public run (https://github.com/arvindram27/ai-cost-margin-pipeline/actions/runs/42485113617, event push) completed with conclusion success and the job step "Run the test suite" succeeded; the pytest counts were not read because the job logs need repository admin rights. The hosted demo was deployed at https://ai-cost-margin-pipeline.streamlit.app/ on Streamlit Community Cloud (free tier, Python 3.13, built from main at 2a22410). Live checks saw all four pages render with data in the owner's session, the Volume scale slider at 160% recompute cust-001 cost from 0.02650785 to 0.04241205, and a fresh anonymous browser render the Customers page without a login. The documentation update in the commit that carries this entry replaces the stale "No CI" and "No hosted deployment" statements, marks M6 built, adds a Demo section with three screenshots of the hosted app and a sample-output section copied from committed files, condenses the Architecture, Workflow and money-rule text (the step-by-step rules stay in the phase contracts, which the README now links), and updates both diagrams and their PNG renders to show M5 and M6 as built.
- Tests run (command -> short result summary, author-reported until review): no tests were rerun for the documentation update and no code, test, workflow or requirements file changed. The CI run above is the only new test evidence: a Windows job that succeeded, counts not read. The local counts in Entry 6 remain author-reported.
- Provenance/data notes (which fixtures or inputs were touched): synthetic fixtures only. The README sample values are copied from fixtures/phase1/batch-001-clean.json, fixtures/phase2/expected/costed-events.jsonl and app/export/customer-margins.jsonl. The three screenshots in docs/screenshots/ come from the hosted app, cropped to app content, with no account, email, username or local path visible.
- Commits produced (hashes pasted from `git log`): 2a224106bf1fad0a0db1165d6f2fac6f32fa86d9 (CI: add Windows pytest workflow). The commit carrying the documentation update and this entry is recorded in the next entry's Log commit field.
- Log commit (hash of the commit that added the PREVIOUS entry): 667875b8df6764df0b063ed2cc210a30888f3548 (the commit carrying Entry 6)
- State after this entry: pushed - awaiting review (this refers to the CI commit 2a22410; the documentation update and this entry are not yet pushed as of this entry)
- Blockers: none
- Next step: the license choice remains a separate optional user decision. A Linux CI job is optional and not planned in this pass; the lock-wording test noted in Entry 6 would need a proper fix first.
