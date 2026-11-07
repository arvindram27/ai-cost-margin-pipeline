# AI Cost & Margin App - Implementation Roadmap

Build order, commit/push gates, the append-only build.md log, and the procedure for switching between OpenCode and DeepSeek Harness when usage limits hit.

Companion to the project brief (`ai-cost-margin-pipeline-brief.md`). The brief defines WHAT we build and WHY; this roadmap defines the ORDER and the working process. If they ever disagree, the brief wins on scope and this file wins on process.

## This roadmap is a plan, not permission

Nothing in this file authorizes execution by itself. Each of these is its own reviewed, scoped approval, asked for at the time with evidence:

- Creating the repo, creating a remote, and choosing its visibility (public or private). Pushing to a public remote publishes the code.
- Installing anything. Only the approved pinned dependencies, at the time they are approved.
- Starting a phase (each phase begins with its own scoped brief, reviewed plan-first).
- Every commit and every push. Commits happen only after the phase's evidence is reviewed and a specific commit/push instruction is approved.

## How to use this roadmap

- A phase is finished only when its pushed commit has been reviewed against its evidence. Code written, or tests passing locally, is progress, not done.
- Each phase carries one of these states, tracked in build.md:
  - `planned` - brief approved, plan not yet approved
  - `in progress` - plan approved, build running
  - `pushed - awaiting review` - commits pushed, review of the exact commit pending
  - `changes requested` - review found problems; fixes go in new commits
  - `verified at <commit hash>` - reviewed and accepted. Only this state completes a phase.
- Approval gates are unchanged by this roadmap: deployment and hosting (Phase 6), any live provider calls or credit spend (Phase 7), and publishing any export derived from real provider traces all still need explicit approval before they happen.

## The working loop (runs inside every phase)

1. The phase's scoped brief is approved (scope, file scope, tests, stop conditions).
2. The coding tool, in Plan mode, reads the brief plus the repo and returns a plan.
3. The plan comes back here for review before any Build. Corrections arrive as one consolidated message to paste back.
4. Bounded Build: only the approved scope. No installs, no new models, no API or runtime provider calls, no new dependencies, no commits unless the brief says so.
5. Run the phase's tests. Paste short real results into build.md (see its evidence rules).
6. Show the evidence and get the commit/push gate: a specific instruction naming the files, the branch, and the commit message set. This gate is separate from the phase brief and comes after the evidence, not before.
7. Commit and push per that instruction, then report with the real HEAD hash copied from git output. Review checks the exact pushed commit, not the tool's summary of it.
8. Review findings are fixed in NEW commits on top. Never amend, reset, or force-push anything already pushed.

## Repo and branch conventions

- One repo. Repo creation, remote creation, and visibility choice are each approved explicitly before they happen.
- Feature branches, not direct commits to `main`: each phase (or meaningful unit) gets a branch created from the last verified commit on main, e.g. `git switch -c phase-2-pricing <verified-base-hash>`. Creating a branch locally is harmless; pushing it needs the approved commit/push gate.
- Merging back to main is its own reviewed step after the phase verifies: the merge instruction names the branch and the verified base, the merge result commit is checked against expectations, and only fast-forward or a plain merge commit is used. No rebase of pushed work, no force push, ever.
- Stage only the files a commit is about: `git add <path> <path>`, never `git add -A` without first checking `git status` for unrelated or unexpected files.
- No secrets in the repo, ever: no `.env`, no API keys, no raw provider identifiers. Phase 0's `.gitignore` covers these; every commit gate re-checks.
- Every commit hash written into build.md or a report must be copied from real git output (`git log --oneline`, `git rev-parse HEAD`). Never typed from memory, never taken from the tool's claim alone.
- Commit along meaningful atomic boundaries, not a fixed count per phase. The suggested groupings below are starting points; the commit/push gate fixes the actual set from what the work produced. No artificial commit ceremony.
- Line endings: Phase 0 adds a `.gitattributes` (`* text=auto`) so Windows CRLF does not create noisy diffs. No frozen per-file SHA manifests in this project; the review gate is the pushed commit itself.

## build.md - the append-only build log

`build.md` lives at the repo root and is committed from Phase 0 on. It is the project's memory: any tool (OpenCode, DeepSeek Harness, a future tool) can rebuild full context from the repo alone by reading the brief, this roadmap, and build.md. That is what makes tool switches safe.

Rules:

1. Entirely append-only. Never edit or delete any old entry, including to fill in placeholders; a correction or a resolved pending item is a new entry. The latest complete entry is the authoritative current state.
2. An entry records only hashes of commits that already exist when the entry is written. The commit that carries the entry itself gets its hash recorded in the NEXT entry. (A commit cannot contain its own hash.)
3. Keep entries sanitized: short test summaries and references to evidence (command, pass/fail counts, file paths), never raw output dumps, never secrets, tokens, or key material. The repo, build.md included, may become public after an approval, so write every entry as if it will be read publicly.
4. `verified` state is granted only by review of the exact pushed commit, never by the tool saying it passed.
5. Keep "Verified base" and "HEAD at work start" distinct in every entry: a dirty mid-phase HEAD is not a verified base.
6. Every tool switch gets its own entry (from-tool, to-tool, reason, verified base and actual HEAD, branch, exact changed/untracked file names, fingerprint hashes, backup label).

The starter file with the entry template is delivered alongside this roadmap as `build.md`.

## Switching between OpenCode and DeepSeek Harness mid-build

When to use this: a tool hits its usage limit ("usage exceeded", rate limited, credits out) mid-phase, or a deliberate tool change.

The goal: the new tool starts from verified repo state, not from chat memory, and no half-finished work gets committed or pushed in the switch.

### Switch-out procedure (old tool)

1. **Stop the old tool.** A tool at its limit may not respond to a "stop" message, so do not rely on one: close or disable the session directly. Before starting any new tool, verify no writer is active: no session open on the repo, and no stale `.git\index.lock` file (if one exists, stop and ask; do not delete it as a reflex).
2. **Capture state read-only** (plain PowerShell):
   - `git status --porcelain=v1 --untracked-files=all` (the stable file list: staged, unstaged, and untracked names)
   - `git status -sb` (branch and upstream; `git rev-parse --abbrev-ref --symbolic-full-name @{u}` errors when there is no upstream - then record "none")
   - `git log --oneline -5`
   - `git rev-parse HEAD`
   Record file NAMES only in notes and build.md, never file contents. Note both the last VERIFIED base commit and the actual HEAD; mid-phase, HEAD can include unverified work.
3. **Scan before exporting:** look at what the diffs and untracked files contain. Anything that looks like a secret (`.env`, keys, tokens) is excluded from the backup and reported instead. Tracked diffs can contain secrets too, not just untracked files.
4. **Back up uncommitted work privately** (PowerShell; create a private folder outside the repo; COPY only, never move or delete the originals). Every export excludes build.md by pathspec, because the switch entry is about to change it and the fingerprint excludes it:
   ```powershell
   $stamp = Get-Date -Format "yyyyMMdd-HHmm"
   $backup = "$env:USERPROFILE\backups\ai-unit-econ-switch-$stamp"
   New-Item -ItemType Directory -Force $backup
   git diff --binary --full-index --output "$backup\unstaged.patch" -- . ':(exclude)build.md'
   git diff --cached --binary --full-index --output "$backup\staged.patch" -- . ':(exclude)build.md'
   git ls-files --others --exclude-standard -- . ':(exclude)build.md' | Out-File -Encoding utf8 "$backup\untracked-files.txt"
   Copy-Item build.md "$backup\build.pre-switch.md" -ErrorAction SilentlyContinue
   ```
   Then copy the needed untracked files into `$backup\untracked\`, recreating their directory structure inside it. The last line preserves the pre-switch build.md as the private reference for the receiving tool's log check (step 10): after the switch entry is appended, the repo's build.md must equal this copy plus exactly that entry. Notes on this block:
   - `$env:USERPROFILE` is the PowerShell form (`%USERPROFILE%` is CMD syntax and fails in PowerShell).
   - Use git's native `--output`, not PowerShell's `>` redirect: git then writes the exact patch bytes itself, while the redirect can re-encode the stream (Windows PowerShell 5.1 writes UTF-16 with a BOM), which corrupts the patch for `git apply`. `--binary --full-index` keeps binary content and full object IDs in the patch.
   - The untracked-files manifest is for humans, so its encoding does not matter.
   - The backup is never committed, and build.md refers to it by a private label only (e.g. "private backup folder, switch 2026-11-07 13:40"), never an absolute home path.
   - Never auto-apply these backups later. No `git stash`, `git apply`, `git checkout --`, or `git reset` without a reviewed instruction at the time.
5. **Write the fingerprint manifest (private, stays in the backup folder):** SHA-256 of each patch file and each backed-up untracked file, plus HEAD, branch, upstream, and the porcelain list (build.md excluded everywhere):
   ```powershell
   Get-FileHash "$backup\staged.patch", "$backup\unstaged.patch" -Algorithm SHA256
   Get-ChildItem -Recurse -File "$backup\untracked" | Get-FileHash -Algorithm SHA256
   ```
   Save that output as `$backup\fingerprint.txt`. Hashes are safe to quote into build.md and the handoff; the files, the manifest, and their paths stay private.
6. **Default: do not commit the partial work.** The backup plus the build.md entry is the record. Only commit mid-phase work if it is at a coherent, test-passing point and the commit/push gate approves it.
7. **Append a Switch entry to build.md:** from-tool and model, to-tool and model, reason, verified base and actual HEAD, branch, the exact list of modified/staged/untracked file names, what is done vs pending, the fingerprint hashes, and the backup label (which also covers the pre-switch build.md copy).
8. **Fingerprint scope:** the handoff fingerprint EXCLUDES build.md on purpose, because appending the switch entry changes it and including it would be self-referential. The log is checked separately, not exempted: the pre-switch private copy (step 4) is the reference, and no digest of the log ever goes into the log itself (step 10).

### Switch-in procedure (new tool)

9. Start the new tool in a FRESH session, in Plan/read-only mode. Paste the handoff block below.
10. The new tool verifies state read-only:
    - Runs `git status --porcelain=v1 --untracked-files=all`, `git status -sb`, `git log --oneline -5`, `git rev-parse HEAD` and compares branch, upstream, HEAD, and the exact porcelain file names against the fingerprint (build.md excluded).
    - Regenerates the patches with the same exclusion - `git diff --binary --full-index -- . ':(exclude)build.md'` and its `--cached` counterpart, written to temp files via `--output` - and compares SHA-256 hashes against the fingerprint; likewise hashes the listed untracked files. Same counts with different content is a mismatch; only identical hashes pass.
    - Checks the log against the pre-switch private copy of build.md (path given in the handoff): the repo's build.md must equal that copy plus exactly the appended switch entry. Earlier entries in the copy are the legitimate immutable prefix, not a problem. Any other log change, or any other mismatch (unexpected commits, different files, hash mismatches), is a stale handoff: STOP, report it, edit nothing. Reconcile first; no Build on a mismatched state.
    - Reads the current phase brief (exact path given in the handoff) and this roadmap from the repo itself.
11. Only after the state checks out: the new tool plans, the plan comes back here for review as usual, then bounded Build.

### Handoff paste block (fill in the <...> slots, paste into the new tool)

```
You are taking over an in-progress build. Work read-only until I approve a plan.

Read these from the repo, in this order:
1. build.md - the latest complete entry is the authoritative current state
2. <exact path of the current phase brief, e.g. docs/phase-2-pricing-engine-brief.md>
3. docs/ai-unit-economics-implementation-roadmap.md (this roadmap)

Context:
- Project: AI cost & margin pipeline with a hosted Streamlit demo app (Python + DuckDB + Streamlit, Windows).
- Current phase: <phase number and name>. State: <planned / in progress / pushed - awaiting review>.
- Branch: <branch>. Verified base: <hash>. Actual HEAD: <hash> (may include unverified work). Upstream: <upstream or none>.
- Fingerprint (build.md intentionally excluded): porcelain file list <exact names>; SHA-256 of staged patch <hash>; SHA-256 of unstaged patch <hash>; untracked files <names + hashes, or none>.
- Pre-switch copy of build.md for the log check: <private backup path>\build.pre-switch.md
- Done so far: <1-2 lines>. Next step: <from the latest build.md entry>.
- I switched tools because: <usage limits / other>. Previous tool: <tool + model>.

Rules:
- First, verify read-only: run git status --porcelain=v1 --untracked-files=all, git status -sb, git log --oneline -5, git rev-parse HEAD; regenerate the patches with git diff --binary --full-index -- . ':(exclude)build.md' (and the --cached counterpart) into temp files via --output and SHA-256 them; hash the listed untracked files. Compare everything against the fingerprint above. Then compare build.md against the pre-switch copy: the difference must be exactly the appended switch entry. Any other mismatch: STOP, report it, edit nothing.
- Then produce a plan only. No Build, no commits, no pushes until I approve the plan.
- Scope is only the current phase brief: no installs, no new models, no API or runtime provider calls, no new dependencies, no unrelated files.
- No force push, no amend, no resets, no stash or patch application. Secrets (.env, API keys) never enter the repo or this chat.
```

## Phase 0 - Repo and environment foundation

### Starting paste block (Phase 0 kickoff, paste into the coding tool)

```
You are starting a new project build. Plan/read-only mode only.

Read from the repo, if it exists: the project brief, docs/ai-unit-economics-implementation-roadmap.md, and build.md.

First report the repo state: whether the repo exists, and if so its branch, HEAD, and whether it matches the latest build.md entry. If there is no repo yet, say so.

Then produce a Phase 0 plan only: repo scaffold, .gitignore, .gitattributes, pinned requirements, smoke test, README. No execution: no installs, no repo or remote creation, no commits, no pushes until I approve the plan and a separate scoped instruction allows each step.
```

Proposed scope (each of these needs its own reviewed approval first: repo creation, remote creation and visibility, and installs limited to the approved pinned dependencies): create the local repo; folder layout (`src/`, `tests/`, `fixtures/`, `docs/`, `app/`); `.gitignore` (raw event store, run history, ingestion manifests, working DuckDB files, `.env`, `var/`, `__pycache__`, `.streamlit/secrets.toml`); `.gitattributes`; pinned `requirements.txt`; README with the documented Windows setup; `build.md` from the starter; the brief and this roadmap committed under `docs/`.

**Outputs:** an empty but runnable skeleton; a smoke test that imports the package; one command to set up from a clean clone.

**Tests:** `pip install -r requirements.txt` succeeds in a fresh venv; `python -m pytest` runs the smoke test green.

**Acceptance evidence:** clean clone in a fresh venv installs and the smoke test passes; summary pasted in build.md.

**Stop conditions:** any dependency or install problem - stop and report, do not improvise replacement packages.

**Suggested commit boundaries:** scaffold+gitignore+deps; docs (brief, roadmap, build.md); smoke test + README. The commit/push gate fixes the actual set.

**Push checkpoint:** only after the commit/push gate, and only to the approved remote. First review (light): repo hygiene, no secrets, pins present.

## Phase 1 - Schema, fixtures, and ingestion foundation (brief M1)

**Scope:** versioned event schema (with explicit handling for provider fields that turn out to be unavailable, and carrying version/correction references so later phases can use them); event identity and dedup key (provider + account + provider event ID); quarantine for same-key-changed-payload unless it carries an explicit version/correction reference; ingestion manifest with content hashes; dead-letter path with failure reasons; seeded deterministic fixture generator and the committed fixture files.

**Outputs:** ingestion module; fixture datasets; manifest + dead-letter written to the gitignored raw store.

**Tests:** golden fixtures with committed expected outputs; negative controls: duplicate file delivery, changed payload under a known event ID, malformed record; dedup keeps distinct billable retry attempt IDs.

**Acceptance evidence:** test summary; a sample manifest reference; dead-letter counts from the run report.

**Stop conditions:** anything needing a live provider call (out of scope); schema ambiguity - stop and ask, do not guess fields.

**Suggested commit boundaries:** schema+fixtures; ingestion+manifest+dead-letter; tests.

**Push checkpoint:** after the commit/push gate. Review: exact commit against fixture outputs.

## Phase 2 - Pricing engine (brief M2)

**Scope:** decimal money everywhere (no floats); price versions as validated effective-date intervals (gap or overlap is an error that stops costing); explicit currency per amount, no cross-currency sums without a dated FX assumption labelled as such; period boundaries in one stated timezone; price selected by the event's usage time; the unpriced bucket (unknown cost, never zero).

**Outputs:** price-list model with interval validation; costing over the fixture events.

**Tests:** price change mid-period; interval gap and overlap rejected; missing rate lands in unpriced; currency mixing rejected; decimal precision checks.

**Acceptance evidence:** test summary plus a costed-events sample reference with price-version provenance.

**Stop conditions:** real provider pricing needed - fixtures carry a synthetic labelled price list, so no live rates are required; do not fetch live prices in this phase.

**Suggested commit boundaries:** price-list model + interval validation; costing engine; tests + golden outputs.

**Push checkpoint:** after the commit/push gate.

## Phase 3 - Aggregation, corrections lineage, and margins (brief M3)

**Scope:** the unallocated bucket; per-customer and per-workload-type margin tables; the canonical result digest definition (financial tables in a defined sort order, excluding run IDs, timestamps, logs); simulated revenue labelled simulated. Corrections and time lineage are explicit here: stored events are never edited; a corrected record arrives as a new event with an explicit supersedes reference (a merely late, non-correction record needs none); usage time, arrival time, and each run's as-of cutoff are three separate fields; a re-run states its cutoff, includes corrections and late arrivals received by then, and produces a NEW versioned run with the prior run's output preserved, never overwritten; tie and conflicting-correction rules are documented and deterministic.

**Outputs:** margin tables from a local run; a second independent hand-calc script for three customers.

**Tests:** unattributable usage lands in unallocated and its size is reported; digest is stable across re-runs of the same inputs; hand-calc matches pipeline output for the three checked customers; a correction superseding an earlier event produces a new run version with the prior run preserved and the lineage visible; golden margin gate: any margin that includes unpriced usage is marked incomplete in the tables, never silently treated as zero-cost.

**Acceptance evidence:** margin table reference; digest value; hand-calc comparison summary (author-reported until review).

**Stop conditions:** a clean-looking margin with a large unallocated share is a fail - stop and fix attribution, do not present it as passing.

**Suggested commit boundaries:** aggregation + unallocated; run versioning + corrections lineage; margin tables + digest; tests + hand-calc script.

**Push checkpoint:** after the commit/push gate.

## Phase 4 - Determinism and recovery (brief M4)

**Scope:** staged checkpointed pipeline (raw -> validated -> costed -> aggregated), each stage transactional and run-scoped; atomic publish of aggregated results only on completed runs; single-writer rule; stale-run detection on restart with a stated resume/discard rule; structured run records (run history table + JSON log); one command to run the full pipeline; the full negative-control and invariance suite.

**Proof drill:** kill the pipeline mid-run, restart it, and show the published canonical digest is identical to an uninterrupted run, with the interrupted run visible in the run history.

**Tests:** reordering invariance; re-run invariance; all brief Priority-1 negative controls; stage re-run from checkpoint; abrupt-kill drill scripted so it can be repeated.

**Acceptance evidence:** both digests from the drill recorded in build.md; suite summary; the run-history rows showing the interrupted run.

**Stop conditions:** any nondeterminism found - stop; it is a defect to fix, not to explain away.

**Suggested commit boundaries:** staged pipeline + checkpoints + atomic publish; stale-run detection + run history; invariance suite + drill script.

**Push checkpoint:** after the commit/push gate. This is the reliability core; expect the heaviest review here.

## Phase 5 - Interactive app, local (brief M5)

**Scope:** the export artifact generator (allowlist schema, omits raw provider IDs and sensitive metadata, fixture-only by default, reviewed before commit); the Streamlit app opening the committed export read-only with per-request/session connections, no shared mutable connection, bounded queries; per-customer margins and workload-type breakdown; what-if scenarios recomputed in memory through the same pricing-engine code; the data-quality/provenance panel showing the export manifest and canonical digest; the multi-session isolation test.

**Outputs:** the app running locally against a real pipeline export.

**Tests:** multi-session isolation (one viewer's scenario choices never persist or affect another session); app-displayed digest equals the pipeline's digest for the same export.

**Acceptance evidence:** screenshots of the running app; isolation test summary; matching digests. The committed export is reviewed before it is committed.

**Stop conditions:** any urge to add upload, auth, or admin features - out of scope by design; stop and flag instead of building them.

**Suggested commit boundaries:** export generator + first reviewed export; app pages; scenario engine + panel; tests + screenshots.

**Push checkpoint:** after the commit/push gate.

## Phase 6 - Hosted deployment (brief M6) - APPROVAL GATE

**Stop at the gate.** Before anything is deployed: re-verify Streamlit Community Cloud's current free terms and limits, measure the app's memory footprint against them, and get the audience decision (public read-only demo vs private invite-only) plus approval of any cost. No hosting account creation, no deployment, no URL published before that explicit approval.

**After approval:** deploy from the repo; verify the live URL shows the same canonical digest as the local run; update the README with the URL and the sleep-after-inactivity note.

**Tests:** live smoke pass - open the URL, run one scenario, check the digest.

**Acceptance evidence:** screenshots of the live app; the digest match; the verified terms noted in build.md.

**Suggested commit boundaries:** README/deploy config only, if the platform needs files in the repo.

**Push checkpoint:** per the gate; deployment happens once, after approval.

## Phase 7 - Live capture (brief M7, optional) - APPROVAL GATE

Starts only after explicit scoped approval, with current rates and credit coverage verified first (credits are money). Record a small real workload in the offline write path, strip prompts/payloads at ingestion, keep customer allocation synthetic. Publishing any export derived from real traces is its own separate approval. Scope and commits are briefed when and if this phase is approved.

## Phase 8 - Bill reconciliation (brief M8, optional)

Only if an independent invoice or billing export becomes available. Reconcile estimated dollars against it under the brief's no-arbitrary-tolerance rule. Briefed when applicable.

## Definition of done

The finish line is Phase 6 verified: a reviewer opens the hosted URL, explores per-customer margins and workload breakdowns, runs a what-if scenario, and sees the data-quality/provenance panel with the canonical digest matching the local pipeline, all figures labelled actual/estimated/synthetic, and the production-oriented-demo (not production-validated) boundary stated in the app.

## Approval gates summary

| Gate | Needed before | Status |
|---|---|---|
| Repo creation, remote, visibility | Phase 0 | not approved |
| Dependency installs (approved pins only) | Phase 0 | not approved |
| Each commit and push | every phase | per-phase gate |
| Hosting terms + audience + cost | Phase 6 deployment | not approved |
| Live provider calls / credits | Phase 7 | not approved |
| Export derived from real traces | any publish of Phase 7 output | not approved |
| Upload/auth/admin features | any such work | not approved, separate scope |
