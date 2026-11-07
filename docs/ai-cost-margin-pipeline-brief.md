# AI Cost & Margin Pipeline - Project Brief

## The problem, in plain terms

AI products can incur variable delivery costs as customers use them. Providers charge in different ways - per token, per minute, per request - and on the token-priced deployment we would use, each call is billed by the tokens it consumes (input, output, and sometimes cached tokens at a different rate). The deployment's actual pricing model gets verified before we rely on it.

When customers pay a flat price, a heavy user can cost more to serve than they pay. That was Krish Subramanian's actual point in the podcast: AI gives software a variable delivery cost, like COGS in manufacturing, so a company needs to know, per customer, what it costs to serve them and whether the pricing covers it.

This project builds a small data pipeline that answers that question - and ships it as a hosted, interactive demo app, not a local report.

## The question it answers

"Customer A paid $X this month. Their AI usage cost an estimated $Y. What drove that cost? Is their price sustainable? What happens if usage doubles, or the provider changes its prices?"

## Who it is for, and the decision it supports

A founder or product/engineering lead at a small AI product. The decisions:

- Is the current pricing sustainable per customer and per workload type?
- Which usage patterns lose money at the current price?
- How sensitive is the margin to usage growth or provider price changes?

## What it is (and is not)

- It is: a data-engineering project with a hosted interactive demo as its end product. Record usage events, apply versioned pricing, attribute cost, join to revenue, compute contribution margins, run what-if scenarios - and publish the results at a URL a reviewer can open, explore, and stress-test.
- It is not: a billing system, an invoicing product, or a claim of an unserved market. Chargebee already ships usage-based billing, PerUnit advertises per-customer AI cost and margin attribution, and Langfuse covers parts of cost tracking. The value of this project is correctness and honest scope, not novelty.

## The end product: a hosted interactive app

The finish line is a working app at a URL, not files on a laptop. A reviewer can:

1. Open a per-customer view: tokens used, estimated cost, simulated revenue, simulated contribution margin, sortable and filterable.
2. Drill into the per-workload-type breakdown (short/light, long/heavy, spiky, cache-heavy) within the one demo feature.
3. Run the what-if scenarios interactively: usage doubles; the provider changes prices; a cheaper model is substituted. Each scenario states its assumptions on the page (the substitution case: workload held constant, no claim of equivalent output quality).
4. Inspect a data-quality and provenance panel: how much of the output is actual vs estimated vs synthetic, the size of the unpriced and unallocated buckets, the export's manifest (code version, input hashes, run cutoff), and the dead-letter/quarantine counts.

Local CLI runs and the written analysis report remain, but they are intermediate artifacts - the report lives inside the app as its analysis page. The hosted app is what "done" means.

Architecture (one small service, chosen to fit the calculation reliability rules below). Everything here is a proposed design; the properties are demonstrated by the milestone proof drills before the project claims them - they are not implemented guarantees today:

- **Write path (offline):** the pipeline runs locally - and only there - as the single writer to its DuckDB working database. That database, with its raw events, run history, and ingestion manifests, stays private and gitignored. For the app it produces a separate, versioned **export artifact**: the canonical result tables plus the data-quality summary and a publication manifest, built from an allowlist schema that omits raw provider event IDs and other sensitive metadata, and reviewed before it is committed. By default the export contains fixture data only; publishing anything derived from real provider traces is a separate approval.
- **Read path (hosted):** a Streamlit app deploys from the project's GitHub repo and serves the committed export artifact. The export file is opened read-only, per request or session - no shared mutable connection - with bounded queries, and scenario what-ifs recompute in memory through the same pricing-engine code the pipeline uses (one calculation path, no separate "UI math" that could diverge), cached against the immutable export. DuckDB documents read-only mode as allowing multiple reader processes, but read-only file access alone is not the whole safety story: the design includes a multi-session isolation test proving one viewer's scenario choices never persist or affect another viewer's session.
- **Updates:** re-run the pipeline locally, regenerate and review the export artifact, commit it, redeploy. Old exports stay in the repo history, so every published number stays traceable to the inputs and code that produced it.

## Data: what is real, estimated, and synthetic

Every number in the output carries a label: **actual**, **estimated**, or **synthetic**.

- **Usage events:** the default is synthetic fixture files - generated sample traffic, labelled synthetic. Fixtures that replay genuinely recorded provider responses count as actual usage, and only when that recorded origin is evidenced; live calls on the token-priced Baseten deployment would also be actual, but happen only with approval (see constraints). Whatever the origin, we capture whatever fields the provider actually exposes - tokens in/out, cached tokens, model, timestamps - mark any field the provider does not expose as unavailable, and do not assume a usage API or billing export exists until that is verified.
- **Estimated cost:** token counts multiplied by a dated, versioned public price list. This is an estimate, not an actual charge: real bills can differ through discounts, credits, minimums, or different rounding. It stays labelled estimated unless it is reconciled against an independent billed amount.
- **Synthetic customers, revenue, and allocation:** the customers, their plans, and the assignment of usage to customers are invented and marked simulated. Even when the underlying usage events are real, which "customer" they belong to is a synthetic allocation for demonstration - the project never claims real per-customer usage. Every margin figure built on simulated revenue says so, in the UI as well as the report.

**Constraints:**

- No live provider calls without explicit scoped approval, given only after verifying current rates and what the available access and credits cover - credits are money too. The project starts entirely on fixture files, so the fixture-only implementation needs no paid-service approval. This brief is a proposal; no build has started.
- No deployment, hosting account creation, or publishing of a URL without explicit approval. Publishing is an audience decision: who gets the link, and whether the demo is public or invite-only, is approved before the app goes live (see hosting section). Any hosting cost is verified and approved before it is incurred.
- Windows environment, plan-first workflow in OpenCode. Stack kept deliberately small: Python + DuckDB + Streamlit, plain CSV/JSON inputs.

## Demo

One demonstrable AI feature, exercised through distinct workload types (short/light, long/heavy, spiky, cache-heavy). About 20 synthetic customers are assigned usage across those workload types. The event schema carries a feature field so multi-feature attribution can be added later, but the demo stays on one feature. The hosted app presents the per-customer margins, the per-workload-type breakdown, the what-if scenarios, and the data-quality/provenance panel described above, with every figure keeping its actual/estimated/synthetic label.

## How we prove it is correct

1. **Token-count reconciliation:** the pipeline's token totals match the provider's own recorded usage counts exactly, as integer counts, wherever the provider exposes comparable numbers.
2. **Dollar reconciliation is a separate claim** and requires an independent billed amount - an invoice or billing export. Comparing against the same API response we ingested is a consistency check, not independent verification, and the report states which of the two was actually possible.
3. **No arbitrary tolerance:** money comparisons use the documented billing precision and rounding rules; any discrepancy that cannot be explained by those rules is reported as unresolved, not smoothed over.
4. **Independent check:** three customers' costs and margins are hand-calculated a second way (spreadsheet or separate script) and must match the pipeline's output.
5. **Nothing silently dropped:** usage records that cannot be attributed go into an explicit *unallocated* bucket, and the report shows its size. A clean-looking margin with a large unallocated share is a fail, not a pass.
6. **Scope honesty:** contribution margin is stated as inference cost vs revenue only. Hosting, storage, tooling, and the hosting platform's own cost are excluded and named as exclusions. The report states that boundary.
7. **Hosted matches local:** the app displays the export's canonical result digest - the financial tables in a defined sort order, excluding run IDs, timestamps, and logs - so a reviewer can confirm what they are looking at. The deployed file is the committed export's exact bytes. Reproducing from a clean checkout means reproducing the same canonical digest, not byte-identical database files: file metadata can legitimately differ between runs. This check is evidenced with screenshots of the running app at the completion stage, not assumed in advance.

## Production-minded requirements (at demo scale)

These additions are production engineering discipline applied at demo scale - together they make this a **production-oriented demo**. It is not production-validated - there is no multi-user access control, no security review, no SLA, no load or failover testing, and no real multi-tenant deployment - and the app and report state that boundary. Each requirement is a design commitment that its milestone's proof drill must demonstrate; none is claimed as working before that.

### Priority 1 - Trustworthy numbers under re-runs and bad input

1. **Idempotent, replayable ingestion.** Every raw input file is content-hashed and recorded in an ingestion manifest. Events dedupe on a stable key: provider, account, and the provider's event ID. A second record with the same key but a *different* payload is neither silently kept nor silently dropped - it goes to quarantine unless it carries an explicit version or correction reference. Legitimate billable retries carry distinct attempt IDs, so dedup never removes a retry the provider really charges for; only duplicate delivery of the same event is removed. Re-running the pipeline on the same inputs produces an identical **canonical result artifact** - the financial result tables in a defined sort order. Run IDs, timestamps, and logs are excluded from that comparison: run history legitimately differs between runs.
2. **Append-only corrections with explicit lineage.** Stored events are never edited. A corrected usage record arrives as a new event carrying an explicit supersedes reference to the event it replaces; a record that is merely late - not a correction - needs no supersedes reference. Three times are kept separate: usage time (when consumption happened), arrival time (when the record reached us), and each run's as-of cutoff. A re-run states its cutoff, includes corrections and late arrivals received by then, and produces a new versioned run - the prior run's output is preserved, not overwritten. Determinism is claimed only for the same set of logical events at the same cutoff, with documented deterministic rules for ties and conflicting corrections.
3. **Schema versioning and a dead-letter path.** Every event carries a schema version; the reader validates against it. Records that fail validation go to a dead-letter file with the failure reason and are counted in the run report - never silently dropped and never coerced into a guessed shape.
4. **Decimal money, validated price intervals, explicit time and currency.** Money uses decimal arithmetic (DuckDB DECIMAL / Python Decimal), never floats. Every amount carries an explicit currency, and sums never mix currencies unless an explicit dated FX assumption is recorded and labelled as an assumption. Price versions form validated effective-date intervals per priced item: a gap or an overlap in coverage is an error that stops costing, not a silent choice. Period boundaries (daily, monthly) are computed in one stated timezone, and the price applied to an event is selected by the event's usage time. An event with no applicable rate goes to an explicit **unpriced** bucket; its cost is unknown, not zero, and any margin that includes unpriced usage is marked incomplete in the app as well as the report.
5. **Deterministic test suite with negative controls.** Small golden fixture datasets with committed expected outputs, plus deliberate failure cases: duplicate file delivery, a changed payload under a known event ID, out-of-order arrivals, a correction superseding an earlier event, a price change mid-period, a price-interval gap, a missing rate, a malformed record, an unattributable customer. Property checks: the canonical result artifact is invariant under event reordering and under re-running, for the same logical events and cutoff.

### Priority 2 - Operable and recoverable

6. **Staged, checkpointed processing with honest atomicity.** The pipeline runs in named stages (raw -> validated -> costed -> aggregated), each stage's output persisted in DuckDB and scoped to a run ID, with a single writer at a time (no concurrent runs over the same database). Each stage's load is transactional, so a failed stage leaves no partial stage output - the pipeline does not claim the whole multi-stage run is atomic. The aggregated results are published only when the run completes, as one atomic publish, and the app's export artifact is generated only from a completed run - readers never see a half-finished run. Any stage can be re-run from the previous stage's checkpoint.
7. **Failure detection and run observability.** An abrupt kill cannot write its own failure record, so the next start detects a stale, incomplete run from its checkpoint state, reports it, and either resumes or discards it by a stated rule. Every run writes a structured run record (JSON log line plus a row in a run-history table): records read, validated, dead-lettered, quarantined, unpriced, unallocated, schema versions seen, price versions applied, stage durations. The app's data-quality panel surfaces the export's share of this information.

### Priority 3 - Reproducible and careful with data

8. **Reproducible environment.** Pinned dependencies (Streamlit's own docs recommend pinning the Streamlit version, since unpinned apps can be auto-upgraded), a seeded deterministic fixture generator, one command to run the full pipeline, one to run the test suite, one to start the app locally, and a documented Windows setup. A clean-checkout clone must reproduce the same canonical result digest the hosted app serves.
9. **Data privacy, scoped honestly.** Customers are synthetic by default - the hosted demo contains no real customer data, no real prompts, and no real revenue. The offline working stores (raw events, run history, ingestion manifests) stay private and gitignored; only the reviewed, sanitized export is committed. If live capture is ever approved, ingestion keeps token counts and metadata only - no prompts or payloads are stored - and customer identifiers are pseudonymous. Customer IDs are a grouping key for attribution, not an access-control boundary: the project has no tenant isolation, handles no real personal data, and makes no compliance claim.

## Hosting: recommendation and security design

**Recommended candidate: Streamlit Community Cloud** (verified against Streamlit's docs, November 7, 2026):

- A free candidate for a demo of this size. Its terms and resource limits can change, so they are verified again at deployment time, and the app's memory footprint is measured against them before go-live - no fit is claimed without measuring.
- Deploys directly from the project's GitHub repo, so the repo, pinned dependencies, fixtures, and export artifact are exactly what the hosted app runs. Public versus private visibility is an explicit choice (below).
- Apps sleep after a period without traffic and wake when a viewer visits - acceptable for a portfolio demo, and stated in the README.

**Security design proposal (an approval decision, not an assumption):**

- **Default: a read-only public demo** serving only the sanitized, labelled synthetic export. No upload endpoint, no admin surface, no write path from the browser, and no credentials anywhere in the hosted app in v1 - the app never calls any provider. Publishing a public URL is still an explicit approval: a public app is shareable and searchable, and the audience decision is the owner's.
- **Alternative: a private, invite-only app** if the owner prefers a closed audience - also an approval, and the invite list is part of it.
- **Any future credentials** (e.g., a Baseten key if live capture is approved) stay in the offline environment - local environment variables or a local secret store - never in the repo and never hosted.
- **Publishing hygiene:** only the sanitized export artifact is committed; raw events, run history, ingestion manifests, and the working database stay gitignored and private. Exporting anything derived from real provider traces or identifiers is a separate approval, reviewed before commit.
- **No upload/admin functionality is added without a separate approved scope.** The moment the app accepts data from visitors, it stops being a read-only demo and needs authentication and a security review - that is a different project phase, not a stretch goal.

**Decision gates before any deployment:** verify the provider's current free terms and limits and measure the app against them, confirm the audience choice (public link vs invited viewers), and approve any cost. Nothing is deployed, and no hosting account is created, before those approvals.

## Deliberately not added

- **No Kafka or streaming:** inputs are files arriving in batches; a batch pipeline matches the data and keeps failure modes simple.
- **No Airflow or external orchestrator:** one ordered script with checkpointed stages is enough at this scale.
- **No microservices or separate API layer:** the write path is one Python process and a DuckDB file; the read path is one Streamlit app. There is nothing to split.
- **No database server or multi-writer setup:** DuckDB's documented multi-process options (a client-server protocol or a coordinated catalog) solve a problem this project does not have - the hosted app only reads a committed export.
- **No auth, user accounts, or upload flows:** the demo is read-only by design; adding them is a separate, approved scope.
- **No containers:** the pipeline runs locally on Windows and the hosting candidate builds from the repo; a Dockerfile adds a build layer with nothing to justify it at this scale.

Adding any of these now would grow the architecture without making the numbers more correct.

## Milestones (each ends in something runnable)

- **M1 - Schema, fixtures, and ingestion foundation:** define the versioned event schema (including explicit handling for provider fields that turn out to be unavailable), the event identity and dedup/quarantine rules, the ingestion manifest, and the dead-letter path; build the fixture files. Fully runnable with zero live calls and zero spend.
- **M2 - Pricing engine:** decimal money, validated price intervals, timezone-explicit period boundaries, price selection by usage time, the unpriced bucket. Output: costed events from fixtures.
- **M3 - Aggregation and margins:** the unallocated bucket, per-customer and per-workload-type margin tables, the canonical result digest definition. Output: the margin tables from a local run.
- **M4 - Determinism and recovery:** checkpointed stages with per-stage transactions and atomic publish, stale-run detection on restart, the full negative-control and invariance suite, one command to run everything. Proof: kill the pipeline mid-run, restart, and show the published canonical result digest is identical to an uninterrupted run, with the interrupted run visible in the run history.
- **M5 - Interactive app (local):** the Streamlit app reading the committed export artifact read-only: per-customer margins, workload-type breakdown, interactive what-if scenarios via the same pricing engine, the data-quality/provenance panel showing the export manifest and canonical digest, and the multi-session isolation test. Output: the app running locally against a real pipeline export, evidenced by screenshots at completion.
- **M6 - Hosted deployment (approval-gated):** after approval of audience and hosting terms, publish the repo to Streamlit Community Cloud (or the approved alternative). Proof: a reviewer opens the URL, explores the demo, runs a scenario, and sees the same canonical digest as the local run - evidenced by screenshots of the live app.
- **Optional M7 - Live capture (approval-gated):** after explicit scoped approval and verified rates and credit coverage, record a small real workload on Baseten in the offline write path and replace fixtures with actual usage events, stripping prompts/payloads at ingestion. Customer allocation stays synthetic, and publishing any export derived from real traces is its own separate approval.
- **Optional M8 - Bill reconciliation:** if an independent invoice or billing export is available, reconcile the estimated dollars against it.

## Why this is a good portfolio piece

It takes a real business question - sustainable unit economics for AI products - and answers it with data-engineering craft: schema design, price versioning, attribution, reconciliation against independent sources where available, and explicit labelling of what is real, estimated, or simulated. It ends in something a reviewer can actually open and use: a hosted app with interactive scenarios and a visible data-quality story, served from a pipeline whose numbers survive re-runs, crashes, and bad input. The reliability work is scoped to this project's real failure modes, with heavy infrastructure deliberately left out and each addition tied to a failure it prevents. The discipline around what is proven, what is assumed, and what is explicitly out of scope is the point of the project.

## Sources checked (November 7, 2026)

Hosting and concurrency claims above were verified against these pages on that date; free terms and limits can change, which is why deployment has a verification gate:

- Streamlit Community Cloud - app management, sleep behaviour, and resource-limit guidance: https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app
- Streamlit Community Cloud - sharing and public/private app visibility: https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app
- DuckDB concurrency model - single writer process vs multiple read-only processes: https://duckdb.org/docs/2.0/connect/concurrency
