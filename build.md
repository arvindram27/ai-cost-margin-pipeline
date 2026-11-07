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
