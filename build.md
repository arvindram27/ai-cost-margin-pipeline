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