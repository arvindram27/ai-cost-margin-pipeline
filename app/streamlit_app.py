"""Phase 5 local app entrypoint: Customers and Workload breakdown pages.

The app reads only the committed, verified export under ``app/export/``. It
writes nothing. Money is rendered as the verbatim 12-decimal strings the
pipeline produced; derived subtotals use exact integer arithmetic at the
10^-12 scale. No chart widgets and no st.metric for money anywhere.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import streamlit as st

import export_data
import scenarios
from ai_cost_margin import aggregate, costing, pricing, revenue
from ai_cost_margin.revenue import workload_sort_key

MONEY_COLUMNS = ("estimated_cost", "simulated_revenue", "simulated_margin")
COUNT_COLUMNS = ("events_costed", "events_unpriced", "events_superseded")

CUSTOMER_COLUMNS = (
    "customer_id", "estimated_cost", "simulated_revenue", "simulated_margin",
    "margin_status",
)
WORKLOAD_COLUMNS = (
    "workload_type", "customer_id",
    "estimated_cost", "simulated_revenue", "simulated_margin",
    "revenue_allocation_status", "margin_status",
)
UNALLOCATED_COLUMNS = ("bucket", "period", "estimated_cost", "events_costed")

COLUMN_LABELS = {
    "customer_id": "Customer",
    "workload_type": "Workload",
    "estimated_cost": "Cost",
    "simulated_revenue": "Revenue",
    "simulated_margin": "Margin",
    "margin_status": "Margin status",
    "revenue_allocation_status": "Allocation",
    "bucket": "Bucket",
    "period": "Period",
    "events_costed": "Events",
}
COLUMN_WIDTHS = {
    "estimated_cost": "medium",
    "simulated_revenue": "medium",
    "simulated_margin": "medium",
    "revenue_allocation_status": "medium",
    "margin_status": "small",
}


@st.cache_data(show_spinner=False)
def _load_bundle(files: tuple[tuple[str, bytes], ...]) -> export_data.ExportBundle:
    return export_data.load_export_from_bytes(files)


def money_str_to_units(text: str) -> int:
    """Parse a ^-?\\d+\\.\\d{12}$ money string into integer 10^-12 units."""
    sign = -1 if text.startswith("-") else 1
    body = text[1:] if sign < 0 else text
    whole, fraction = body.split(".")
    return sign * (int(whole) * 10**12 + int(fraction))


def units_to_money_str(units: int) -> str:
    """Render integer 10^-12 units back as a 12-decimal money string."""
    sign = "-" if units < 0 else ""
    magnitude = abs(units)
    return f"{sign}{magnitude // 10**12}.{magnitude % 10**12:012d}"


def display_money(value) -> str:
    """A null or absent money value renders as an em dash, never zero."""
    return "\u2014" if value is None else value


def _display_rows(rows, money_fields):
    display = []
    for record in rows:
        row = record.as_dict()
        for field in money_fields:
            if field in row:
                row[field] = display_money(row[field])
        display.append(row)
    return display


def _dataframe(rows, money_fields, columns):
    config = {}
    for field in columns:
        label = COLUMN_LABELS.get(field, field)
        width = COLUMN_WIDTHS.get(field)
        if field in money_fields:
            config[field] = st.column_config.TextColumn(label, width=width)
        elif field in COUNT_COLUMNS:
            config[field] = st.column_config.NumberColumn(label, format="%d", width=width)
        else:
            config[field] = st.column_config.TextColumn(label, width=width)
    st.dataframe(rows, hide_index=True, column_config=config, column_order=list(columns))


def _labels_caption(bundle) -> str:
    labels = bundle.manifest["labels"]
    periods = sorted({row["period"] for row in bundle.customer_margins})
    return (
        f"Usage: {labels['usage']} | Cost: {labels['cost']} | "
        f"Revenue: {labels['revenue']} | Margin: {labels['margin']} | "
        f"Period: {', '.join(periods)} | "
        "Production-oriented demo on synthetic local data; no live provider data."
    )


def _sum_units(rows, field) -> int:
    total = 0
    for record in rows:
        value = record.get(field)
        if value is not None:
            total += money_str_to_units(value)
    return total


def _customers_page(bundle) -> None:
    rows = sorted(
        bundle.customer_margins, key=lambda row: (row["period"], row["customer_id"])
    )
    st.subheader("Customers")
    _dataframe(_display_rows(rows, MONEY_COLUMNS), MONEY_COLUMNS, CUSTOMER_COLUMNS)

    complete = [row for row in rows if row["margin_status"] == "complete"]
    incomplete = [row for row in rows if row["margin_status"] != "complete"]
    complete_ids = ", ".join(row["customer_id"] for row in complete)
    cost_subtotal = units_to_money_str(_sum_units(rows, "estimated_cost"))
    revenue_subtotal = units_to_money_str(_sum_units(complete, "simulated_revenue"))
    margin_subtotal = units_to_money_str(_sum_units(complete, "simulated_margin"))
    st.write(
        "Estimated cost subtotal - known priced usage, "
        f"all {len(rows)} customers (excludes the unallocated bucket and "
        f"unpriced usage): {cost_subtotal}"
    )
    st.write(
        "Simulated revenue subtotal - complete-margin customers only "
        f"({complete_ids}): {revenue_subtotal}"
    )
    st.write(
        "Simulated margin subtotal - complete-margin customers only "
        f"(same subset): {margin_subtotal}"
    )
    if incomplete:
        excluded = ", ".join(row["customer_id"] for row in incomplete)
        st.caption(
            f"Excluded from the revenue and margin subtotals: {excluded} - "
            "partial figures with margin_status incomplete."
        )
    first = rows[0]
    st.caption(
        f"Labels - usage: {first['usage_label']}; cost: {first['cost_label']}; "
        f"revenue: {first['revenue_label']}; margin: {first['margin_label']}."
    )

    customer_ids = [row["customer_id"] for row in rows]
    selected = st.selectbox("Customer drill-down", customer_ids)
    customer_row = next(row for row in rows if row["customer_id"] == selected)
    st.write(f"Customer {selected} (period {customer_row['period']}, plan {customer_row['plan_id']})")
    _dataframe(_display_rows([customer_row], MONEY_COLUMNS), MONEY_COLUMNS, CUSTOMER_COLUMNS)
    workload_rows = sorted(
        (row for row in bundle.workload_margins if row["customer_id"] == selected),
        key=lambda row: (workload_sort_key(row["workload_type"]), row["period"]),
    )
    _dataframe(_display_rows(workload_rows, MONEY_COLUMNS), MONEY_COLUMNS, WORKLOAD_COLUMNS)


def _workload_page(bundle) -> None:
    st.subheader("Workload breakdown")
    customer_ids = ["All customers"] + sorted(
        {row["customer_id"] for row in bundle.workload_margins}
    )
    selected = st.selectbox("Customer filter", customer_ids)
    rows = sorted(
        bundle.workload_margins,
        key=lambda row: (
            workload_sort_key(row["workload_type"]),
            row["customer_id"],
            row["period"],
        ),
    )
    if selected != "All customers":
        rows = [row for row in rows if row["customer_id"] == selected]
    _dataframe(_display_rows(rows, MONEY_COLUMNS), MONEY_COLUMNS, WORKLOAD_COLUMNS)
    if any(row["revenue_allocation_status"] != "allocated" for row in bundle.workload_margins):
        st.caption(
            "Rows marked incomplete_unpriced_excluded are not fully allocated - "
            "unpriced usage excluded from the allocation basis."
        )

    st.subheader("Unallocated - no customer attribution")
    _dataframe(_display_rows(bundle.unallocated, ("estimated_cost",)), ("estimated_cost",), UNALLOCATED_COLUMNS)
    st.caption("The unallocated bucket carries no revenue or margin by design.")


def _reset_scenario() -> None:
    for key in [key for key in st.session_state if key.startswith("scenario_")]:
        del st.session_state[key]


def _models(bundle) -> list[str]:
    return sorted({entry["model"] for entry in bundle.price_list["entries"]})


def _first_rates(bundle, model: str):
    entries = [
        entry for entry in bundle.price_list["entries"] if entry["model"] == model
    ]
    first = sorted(entries, key=lambda entry: entry["effective_from"])[0]
    return first["rates_per_million"]


def _render_scenario_tables(customer_rows, workload_rows, unallocated_rows) -> None:
    customer = sorted(
        customer_rows, key=lambda row: (row["period"], row["customer_id"])
    )
    _dataframe(_display_rows(customer, MONEY_COLUMNS), MONEY_COLUMNS, CUSTOMER_COLUMNS)
    workload = sorted(
        workload_rows,
        key=lambda row: (
            workload_sort_key(row["workload_type"]),
            row["customer_id"],
            row["period"],
        ),
    )
    _dataframe(_display_rows(workload, MONEY_COLUMNS), MONEY_COLUMNS, WORKLOAD_COLUMNS)
    _dataframe(
        _display_rows(unallocated_rows, ("estimated_cost",)),
        ("estimated_cost",),
        UNALLOCATED_COLUMNS,
    )


def _scenario_page(bundle) -> None:
    st.subheader("What-if scenarios")
    st.caption(
        "What-if figures recomputed in memory from the committed synthetic "
        "export; nothing is saved. Workload is held constant and no "
        "output-quality claim is made when a model is substituted."
    )
    models = _models(bundle)
    volume = st.slider(
        "Volume scale (%)",
        min_value=10,
        max_value=300,
        step=10,
        value=100,
        key="scenario_volume",
    )
    flat_rates = []
    substitutions = []
    for model in models:
        mode = st.radio(
            f"{model} rates",
            ["Keep original rates", "Set flat rates"],
            key=f"scenario_ratemode_{model}",
        )
        if mode == "Set flat rates":
            defaults = _first_rates(bundle, model)
            rates = tuple(
                st.text_input(
                    f"{model} {unit} rate (per million)",
                    value=defaults[unit],
                    key=f"scenario_rate_{model}_{unit}",
                )
                for unit in ("input", "output", "cached")
            )
            flat_rates.append((model, *rates))
        choice = st.selectbox(
            f"{model} substitution",
            ["original"] + models,
            key=f"scenario_sub_{model}",
        )
        if choice != "original":
            substitutions.append((model, choice))
    st.button("Reset scenario", on_click=_reset_scenario)

    levers = scenarios.ScenarioLevers(
        volume_tenths=volume // 10,
        flat_rates=tuple(flat_rates),
        substitutions=tuple(substitutions),
    )
    lever_lines = [f"Volume scale {volume}%."]
    for model, input_rate, output_rate, cached_rate in flat_rates:
        lever_lines.append(
            f"{model} rates overridden flat at input {input_rate}, output "
            f"{output_rate}, cached {cached_rate} per million (original price "
            "version ids retained with overridden rates)."
        )
    for model, replacement in substitutions:
        lever_lines.append(
            f"{model} events priced as {replacement} (workload held constant; "
            "no output-quality claim)."
        )

    try:
        result = scenarios.compute_scenario(bundle, levers)
    except (
        ValueError,
        pricing.PriceScheduleError,
        revenue.PlanScheduleError,
        aggregate.UnknownPlanError,
        aggregate.CompositionError,
        costing.LineageAmbiguityError,
    ) as exc:
        st.error(
            "Scenario invalid; showing the published baseline, "
            f"overrides not applied: {exc}"
        )
        st.info(
            "The levers below were NOT applied; the tables show the verified "
            "published baseline. Attempted levers: " + " ".join(lever_lines)
        )
        _render_scenario_tables(
            bundle.customer_margins, bundle.workload_margins, bundle.unallocated
        )
        st.write(
            "Published baseline digest (attested in the export manifest): "
            f"{bundle.manifest['source']['digest']}"
        )
        return

    if volume == 100 and not flat_rates and not substitutions:
        st.info("Scenario equals the published baseline (no levers applied).")
    else:
        st.info("Active levers: " + " ".join(lever_lines))
    _render_scenario_tables(
        result.customer_rows, result.workload_rows, result.unallocated_rows
    )
    report = result.report
    reasons = report["unpriced_by_reason"].as_dict()
    reason_text = ", ".join(f"{name}={reasons[name]}" for name in sorted(reasons))
    st.write(
        f"Costed: {report['events_costed']} | Unpriced: {report['events_unpriced']} "
        f"| Superseded: {report['events_superseded']} | "
        f"Unpriced reasons: {reason_text}"
    )
    st.write(
        "Scenario digest (recomputed for these what-if inputs; "
        f"not the published digest): {result.digest}"
    )


def _provenance_page(bundle) -> None:
    st.subheader("Provenance")
    manifest = bundle.manifest
    source = manifest["source"]
    labels = manifest["labels"]
    st.write(f"Run fingerprint: {source['run_fingerprint']}")
    st.write(f"Artifact directory: {source['artifact_dir']}")
    st.write(f"As-of cutoff: {source['as_of_cutoff']}")
    st.write(
        f"Code version: {source['code_version']} "
        f"({source['code_version_source']}, "
        f"working tree clean: {source['working_tree_clean']})"
    )
    st.write(f"Store events digest: {source['store_events_sha256']}")
    st.write(f"Store: {source['store_backend']} / {source['store_locator']}")
    st.write(
        f"Price list: {source['price_list_id']} (sha256 {source['price_list_sha256']})"
    )
    st.write(f"Plans: {source['plans_version_id']} (sha256 {source['plans_sha256']})")
    st.write(
        f"Events in store: {source['events_in_store']} | included: "
        f"{source['events_included']} | excluded by cutoff: "
        f"{source['events_excluded_by_cutoff']}"
    )
    st.write(
        f"Costed: {source['events_costed']} | Unpriced: {source['events_unpriced']} "
        f"| Superseded: {source['events_superseded']}"
    )
    reasons = source["unpriced_by_reason"].as_dict()
    reason_text = ", ".join(f"{name}={reasons[name]}" for name in sorted(reasons))
    st.write(f"Unpriced by reason: {reason_text}")
    st.write(f"Dangling supersedes: {source['dangling_supersedes']}")
    st.write(
        f"Unallocated shares (bp): cost {source['unallocated_cost_share_bp']}, "
        f"events {source['unallocated_event_share_bp']}"
    )
    st.write(
        "Ingest counters (published-attempt ingest counters, not the "
        f"fixture-corpus totals): dead_lettered={source['dead_lettered']}, "
        f"quarantined={source['quarantined']}, duplicates={source['duplicates']}"
    )
    st.write(
        f"Labels - usage: {labels['usage']}; cost: {labels['cost']}; "
        f"revenue: {labels['revenue']}; margin: {labels['margin']}"
    )
    st.write(f"Export schema version: {manifest['export_schema_version']}")

    file_rows = [
        {"name": name, "rows": entry["rows"], "sha256": entry["sha256"]}
        for name, entry in sorted(manifest["files"].items)
    ]
    st.dataframe(file_rows, hide_index=True)
    if bundle.lineage:
        _dataframe(
            _display_rows(bundle.lineage, ()),
            (),
            bundle.lineage[0].keys(),
        )

    attested = source["digest"]
    recomputed = bundle.financial_digest
    st.write(f"Pipeline digest (attested in the export manifest): {attested}")
    st.write(f"Digest recomputed by this app from the exported tables: {recomputed}")
    if recomputed == attested:
        st.success("MATCH")
    else:
        st.error("MISMATCH - do not trust this export")
    st.write(
        "Export integrity digest (recomputed over the export bytes): "
        f"{bundle.integrity_digest}"
    )
    st.caption(
        f"Digest definitions: {source['digest_definition']} | "
        f"{manifest['export_integrity_definition']}"
    )


def main() -> None:
    st.set_page_config(page_title="AI Cost & Margin Pipeline", layout="wide")
    try:
        files = export_data.read_export_files()
        bundle = _load_bundle(files)
    except export_data.ExportIntegrityError as exc:
        st.error(f"Export verification failed: {exc}")
        st.stop()
        return

    st.title("AI Cost & Margin Pipeline - Export Review")
    st.caption(_labels_caption(bundle))

    section = st.sidebar.radio(
        "Section",
        ["Customers", "Workload breakdown", "What-if scenarios", "Provenance"],
    )
    if section == "Customers":
        _customers_page(bundle)
    elif section == "Workload breakdown":
        _workload_page(bundle)
    elif section == "What-if scenarios":
        _scenario_page(bundle)
    else:
        _provenance_page(bundle)


if __name__ == "__main__":
    main()
