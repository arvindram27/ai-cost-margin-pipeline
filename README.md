# AI Cost & Margin Pipeline

A small data pipeline that answers one question per customer: *what did this usage
cost to serve, what did the customer pay, and is the price sustainable?* The end
product is a hosted, interactive Streamlit demo; this repo is at the start of that
build.

**Status: Phase 0 - empty but runnable skeleton.** There is no pipeline, no app, no
data, no export and no deployment yet. The only thing proven so far is that a clean
checkout installs the pinned dependencies and the package imports.

The target is a **production-oriented demo**, not a production-validated system:
there is no multi-user access control, no security review, no SLA and no real
multi-tenant deployment. Any later reliability or reproducibility claim is tied to a
proof run that demonstrates it.

## Stack

Python 3.13, DuckDB, Streamlit, plain CSV/JSON inputs. Windows development environment.

| Direct dependency | Pin  | Why it is pinned                                                    |
| ----------------- | ---- | ------------------------------------------------------------------- |
| `streamlit`       | 1.67.0 | Hosting can auto-upgrade unpinned apps; the pin keeps the demo reproducible |
| `duckdb`          | 2.0.0  | Single-writer analytical store for the local pipeline                |
| `pytest`          | 9.2.0  | Test runner                                                          |

`requirements.txt` pins the three direct dependencies only; pip resolves their
transitive dependencies at install time.

## Windows setup

From a fresh clone, in PowerShell at the repo root:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --only-binary :all: -r requirements.txt
.venv\Scripts\python.exe -m pytest
```

Notes:

- Call `.venv\Scripts\python.exe` directly instead of running `Activate.ps1`, so the
  PowerShell Execution Policy cannot get in the way.
- Do not run `pip install --upgrade pip`: the bundled pip is enough, and upgrading
  would install an unpinned package.
- `--only-binary :all:` fails fast when a wheel is missing, rather than trying to
  build from source.

## Layout

```text
ai-cost-margin-pipeline/
  app/                  Streamlit app (Phase 5; placeholder only today)
  docs/                 project brief and implementation roadmap
  fixtures/             synthetic input fixtures (Phase 1; placeholder only today)
  src/ai_cost_margin/   shared pipeline package
  tests/                pytest suite
  var/                  private working state - raw events, run history, DuckDB files
                        (gitignored; not created yet)
```

## Data rules

- Usage data is **synthetic** by default. This repository contains no real customer
  data, no real prompts and no real revenue.
- `var/` - the raw event store, run history, ingestion manifests and working DuckDB
  files - stays private and gitignored. Only a reviewed, sanitized export is ever
  committed.
- No secrets in the repo, ever: no `.env` file, no API keys, no provider identifiers.

Every number the pipeline produces is labelled **actual**, **estimated** or
**synthetic**, and every margin built on simulated revenue says so.

## Known constraints

- Windows long paths are disabled on the development machine, so repository paths are
  kept short by design.
- Whether the hosting platform offers Python 3.13 is a **Phase 6 deployment check**,
  verified then and not claimed here.

## Documentation

- [Project brief](docs/ai-cost-margin-pipeline-brief.md)
- [Implementation roadmap](docs/ai-unit-economics-implementation-roadmap.md)
- [Build log](build.md) - append-only record of progress and evidence

## Not present yet

No pipeline, no schema, no fixtures, no costing, no aggregation, no export, no
Streamlit app, no tests beyond the smoke test, no CI, no deployment, no license yet.
