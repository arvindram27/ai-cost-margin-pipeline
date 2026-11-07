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
