"""Independent hand-calculation for three Phase 3 customer-months.

The tool under ``tools/`` deliberately does not import the package. These tests
load it by path, then check both its arithmetic and its standard-library-only
import graph against the pipeline's own customer-margin table.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

from ai_cost_margin.ingest import ingest
from ai_cost_margin.margins import run_margins

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "fixtures" / "phase1"
PHASE3 = ROOT / "fixtures" / "phase3"
PRICES = ROOT / "fixtures" / "phase2" / "price-list-synthetic-v1.json"
PLANS = PHASE3 / "customer-plans-simulated-v1.json"
TOOL = ROOT / "tools" / "handcalc_margins.py"
CUTOFF = "2026-11-01T23:59:59Z"
GOLDEN_VERSION = "fixtures-phase3-golden-v1"
DELIVERIES = [
    "batch-001-clean.json",
    "batch-002-retries.json",
    "batch-003-changed-payload.json",
    "batch-004-correction.json",
    "batch-005-malformed.json",
    "batch-006-unavailable-fields.json",
    "batch-007-invalid-json.json",
    "batch-001-clean.json",
]


def _load_tool():
    spec = importlib.util.spec_from_file_location("handcalc_margins_phase3", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path):
    home = tmp_path / "store"
    ingest([PHASE1 / name for name in DELIVERIES], home)
    result = run_margins(
        home=home,
        prices=PRICES,
        plans=PLANS,
        cutoff=CUTOFF,
        out_root=tmp_path / "runs",
        code_version_override=GOLDEN_VERSION,
    )
    return home, result


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_handcalc_matches_every_customer_month_for_cust_001_to_003(tmp_path):
    handcalc = _load_tool()
    home, result = _run(tmp_path)
    events = _read_jsonl(home / "state" / "events.jsonl")
    prices = json.loads(PRICES.read_text(encoding="utf-8"))
    plans = json.loads(PLANS.read_text(encoding="utf-8"))
    table_rows = _read_jsonl(result["run_dir"] / "customer-margins.jsonl")

    computed = handcalc.compute_customer_rows(events, prices, plans, CUTOFF)
    comparisons = handcalc.compare_rows(computed, table_rows)

    assert {row["customer_id"] for row in computed} == {"cust-001", "cust-002", "cust-003"}
    assert {row["period"] for row in computed} == {"2026-10"}
    assert len(comparisons) == 3
    assert all(comparison["match"] for comparison in comparisons), comparisons


def test_handcalc_known_values_are_exact_for_cust_001(tmp_path):
    handcalc = _load_tool()
    home, _ = _run(tmp_path)
    events = _read_jsonl(home / "state" / "events.jsonl")
    prices = json.loads(PRICES.read_text(encoding="utf-8"))
    plans = json.loads(PLANS.read_text(encoding="utf-8"))

    rows = handcalc.compute_customer_rows(events, prices, plans, CUTOFF, customers=("cust-001",))
    assert rows == [
        {
            "period": "2026-10",
            "customer_id": "cust-001",
            "events_costed": 4,
            "events_unpriced": 0,
            "estimated_cost": "0.026507850000",
            "simulated_revenue": "49.000000000000",
            "simulated_margin": "48.973492150000",
            "margin_status": "complete",
        }
    ]


def test_handcalc_script_is_standard_library_only_and_never_imports_the_package():
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    assert "ai_cost_margin" not in roots
    assert roots <= set(sys.stdlib_module_names)


def test_handcalc_cli_reports_a_full_match(tmp_path, capsys):
    handcalc = _load_tool()
    home, result = _run(tmp_path)
    code = handcalc.main(
        [
            "--home", str(home),
            "--prices", str(PRICES),
            "--plans", str(PLANS),
            "--cutoff", CUTOFF,
            "--run-dir", str(result["run_dir"]),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "cust-001" in output
    assert "cust-002" in output
    assert "cust-003" in output
    assert "hand-calc: 3/3 customer-month rows match" in output
