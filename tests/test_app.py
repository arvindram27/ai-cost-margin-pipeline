"""Phase 5 app pages through Streamlit AppTest (no server, no network).

Each test builds its own AppTest session from the app script by absolute path.
Dataframe values keep every key passed to ``st.dataframe`` (``column_order`` is
display-only), so rendered rows are projected onto the compared columns.
Nothing here writes to ``app/export/`` or reads ``var/``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export_data  # noqa: E402
from ai_cost_margin.revenue import workload_sort_key  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = ROOT / "app" / "streamlit_app.py"
EXPORT_DIR = ROOT / "app" / "export"
GOLDEN_DIGEST = "39240f3cdd813b7cd7ac67a421a303bc47cf4268ca390b51e81a03ebfe52edf0"
EM_DASH = "\u2014"
SECTIONS = ["Customers", "Workload breakdown", "What-if scenarios", "Provenance"]
CUSTOMER_COLUMNS = (
    "customer_id",
    "estimated_cost",
    "simulated_revenue",
    "simulated_margin",
    "margin_status",
)
WORKLOAD_COLUMNS = (
    "workload_type",
    "customer_id",
    "estimated_cost",
    "simulated_revenue",
    "simulated_margin",
    "revenue_allocation_status",
    "margin_status",
)
UNALLOCATED_COLUMNS = ("bucket", "period", "estimated_cost", "events_costed")
MONEY = ("estimated_cost", "simulated_revenue", "simulated_margin")
BASELINE_BANNER = "Scenario equals the published baseline (no levers applied)."
INVALID_PREFIX = "Scenario invalid; showing the published baseline, overrides not applied"


@pytest.fixture(scope="module")
def bundle():
    return export_data.load_export(EXPORT_DIR)


def _new_app(section=None):
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    if section is not None:
        at.sidebar.radio[0].set_value(section)
        at.run()
        assert not at.exception, [str(e) for e in at.exception]
    return at


def _expected_rows(records, columns, key):
    out = []
    for row in sorted((r.as_dict() for r in records), key=key):
        out.append(
            {
                c: (EM_DASH if row.get(c) is None and c in MONEY else row.get(c))
                for c in columns
            }
        )
    return out


def _rendered(at, length, columns):
    for frame in at.dataframe:
        if len(frame.value) == length:
            return [
                {c: row.get(c) for c in columns}
                for row in (dict(r) for r in frame.value.to_dict("records"))
            ]
    raise AssertionError(f"no rendered dataframe with {length} rows")


def _assert_baseline_tables(at, bundle):
    customers = _expected_rows(
        bundle.customer_margins,
        CUSTOMER_COLUMNS,
        lambda r: (r["period"], r["customer_id"]),
    )
    workloads = _expected_rows(
        bundle.workload_margins,
        WORKLOAD_COLUMNS,
        lambda r: (workload_sort_key(r["workload_type"]), r["customer_id"], r["period"]),
    )
    unallocated = [
        {
            c: (EM_DASH if r.get(c) is None and c == "estimated_cost" else r.get(c))
            for c in UNALLOCATED_COLUMNS
        }
        for r in (rec.as_dict() for rec in bundle.unallocated)
    ]
    assert _rendered(at, len(customers), CUSTOMER_COLUMNS) == customers
    assert _rendered(at, len(workloads), WORKLOAD_COLUMNS) == workloads
    assert _rendered(at, len(unallocated), UNALLOCATED_COLUMNS) == unallocated


def _page_text(at):
    parts = []
    for group in (at.markdown, at.text, at.caption, at.info, at.success, at.error):
        parts.extend(str(e.value) for e in group)
    return "\n".join(parts)


def _cust001(at):
    return next(
        r for r in _rendered(at, 6, CUSTOMER_COLUMNS) if r["customer_id"] == "cust-001"
    )


def _scenario_digest(at):
    for line in _page_text(at).splitlines():
        if "Scenario digest" in line:
            return line.rsplit(" ", 1)[-1].strip()
    raise AssertionError("scenario digest line not found")


def _by_label(elements, label):
    return next(e for e in elements if e.label == label)


def _reset(at):
    _by_label(at.button, "Reset scenario").click().run()
    assert not at.exception, [str(e) for e in at.exception]


def test_app_loads_scenario_baseline_banner_and_digest(bundle):
    at = _new_app("What-if scenarios")
    assert list(at.sidebar.radio[0].options) == SECTIONS
    assert BASELINE_BANNER in [str(e.value) for e in at.info]
    assert _scenario_digest(at) == GOLDEN_DIGEST
    _assert_baseline_tables(at, bundle)
    row = _cust001(at)
    assert row["estimated_cost"] == "0.026507850000"
    assert row["simulated_revenue"] == "49.000000000000"
    assert row["simulated_margin"] == "48.973492150000"
    assert row["margin_status"] == "complete"


def test_two_session_isolation():
    session_a = _new_app("What-if scenarios")
    _by_label(session_a.slider, "Volume scale (%)").set_value(200)
    session_a.run()
    assert not session_a.exception, [str(e) for e in session_a.exception]
    assert _cust001(session_a)["estimated_cost"] == "0.053015700000"

    session_b = _new_app("What-if scenarios")
    assert _by_label(session_b.slider, "Volume scale (%)").value == 100
    assert _cust001(session_b)["estimated_cost"] == "0.026507850000"
    assert BASELINE_BANNER in [str(e.value) for e in session_b.info]

    assert _by_label(session_a.slider, "Volume scale (%)").value == 200
    assert _cust001(session_a)["estimated_cost"] == "0.053015700000"
    assert _scenario_digest(session_a) != _scenario_digest(session_b)


def test_provenance_page_counters_digests_and_match(bundle):
    at = _new_app("Provenance")
    text = _page_text(at)
    source = bundle.manifest["source"]
    assert source["run_fingerprint"] in text
    assert "published-attempt ingest counters" in text
    assert "fixture-corpus totals" in text
    for name in ("dead_lettered", "quarantined", "duplicates"):
        assert f"{name}={source[name]}" in text
    labels = bundle.manifest["labels"]
    assert (
        f"usage: {labels['usage']}; cost: {labels['cost']}; "
        f"revenue: {labels['revenue']}; margin: {labels['margin']}"
    ) in text
    assert text.count(GOLDEN_DIGEST) >= 2
    assert bundle.integrity_digest in text
    assert "MATCH" in [str(e.value) for e in at.success]
    assert not list(at.error)


def test_loader_refuses_tampered_export_copy(tmp_path):
    copied = tmp_path / "export"
    shutil.copytree(EXPORT_DIR, copied)
    target = copied / "customer-margins.jsonl"
    original = target.read_bytes()
    assert b'"0.026507850000"' in original
    target.write_bytes(original.replace(b'"0.026507850000"', b'"0.026507850001"', 1))
    assert target.read_bytes() != original

    with pytest.raises(export_data.ExportIntegrityError):
        export_data.load_export(copied)

    # The committed export is untouched and still verifies.
    assert export_data.load_export(EXPORT_DIR).financial_digest == GOLDEN_DIGEST


def test_app_refuses_tampered_export_copy(tmp_path, monkeypatch):
    copied = tmp_path / "export"
    shutil.copytree(EXPORT_DIR, copied)
    target = copied / "customer-margins.jsonl"
    original = target.read_bytes()
    target.write_bytes(original.replace(b'"0.026507850000"', b'"0.026507850001"', 1))
    monkeypatch.setattr(export_data, "default_export_dir", lambda: copied)

    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any(
        str(e.value).startswith("Export verification failed") for e in at.error
    )
    assert not list(at.title)
    assert not list(at.dataframe)


def test_reset_button_restores_default_levers():
    at = _new_app("What-if scenarios")
    _by_label(at.slider, "Volume scale (%)").set_value(200)
    _by_label(at.radio, "synth-small rates").set_value("Set flat rates")
    at.run()
    _by_label(at.selectbox, "synth-large substitution").set_value("synth-small")
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert BASELINE_BANNER not in [str(e.value) for e in at.info]

    _reset(at)
    assert _by_label(at.slider, "Volume scale (%)").value == 100
    for radio in at.radio:
        if radio.label.endswith(" rates"):
            assert radio.value == "Keep original rates", (radio.label, radio.value)
    for box in at.selectbox:
        if box.label.endswith(" substitution"):
            assert box.value == "original", (box.label, box.value)
    assert BASELINE_BANNER in [str(e.value) for e in at.info]
    row = _cust001(at)
    assert row["estimated_cost"] == "0.026507850000"
    assert row["simulated_margin"] == "48.973492150000"
    assert _scenario_digest(at) == GOLDEN_DIGEST


def test_invalid_rate_falls_back_to_published_baseline(bundle):
    at = _new_app("What-if scenarios")
    _by_label(at.radio, "synth-small rates").set_value("Set flat rates")
    at.run()
    _by_label(at.text_input, "synth-small input rate (per million)").set_value("abc")
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any(str(e.value).startswith(INVALID_PREFIX) for e in at.error)
    _assert_baseline_tables(at, bundle)
    assert GOLDEN_DIGEST in _page_text(at)
    assert any("NOT applied" in str(e.value) for e in at.info)
