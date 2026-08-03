# v0.1 | 27-Jun-2026 | Initial Streamlit dashboard
# v0.2 | 26-Jul-2026 | Replace the Phase 2 migration tab with an About tab
#                      (POC framing; portability kept as a design property,
#                      not a roadmap); drop the "Phase 1 pilot" caption. The
#                      Consistency dimension now appears via assess() v0.2.

"""AgentDQ dashboard.

A presentation layer over the shared assess() function. It shows the data
quality scorecard, lets a reader drill into findings, presents the
precision/recall evidence on labelled data, and explains what the proof of
concept demonstrates. All the analysis lives in src/; this file is purely the view.

Launch from the repository root:

    uv run streamlit run app/dashboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repository importable however Streamlit is launched.
ROOT: Path = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st

from src.contracts import Finding
from src.reporting.assessment import AssessmentResult, assess


TABLES: list[str] = ["MARA", "MARC", "MAKT"]
SCORE_SCALE: str = "RdYlGn"
SEVERITY_ORDER: list[str] = ["Critical", "High", "Medium", "Low"]


def discover_datasets(root: Path) -> list[dict[str, str]]:
    """Find datasets available to assess: synthetic scenarios and the real extract."""
    datasets: list[dict[str, str]] = []
    synthetic_root: Path = root / "data" / "synthetic"
    raw_root: Path = root / "data" / "raw"
    scenario_dir: Path = None

    if synthetic_root.exists():
        for scenario_dir in sorted(synthetic_root.iterdir()):
            if (scenario_dir / "MARA.parquet").exists():
                datasets.append({
                    "label": f"Synthetic - {scenario_dir.name}",
                    "dir": str(scenario_dir),
                    "format": "parquet",
                })
    if (raw_root / "MARA_EX_DATA.xlsx").exists():
        datasets.append({"label": "Real CAL extract", "dir": str(raw_root), "format": "xlsx"})
    return datasets


@st.cache_data(show_spinner="Running the agents...")
def cached_assess(data_dir: str, data_format: str) -> AssessmentResult:
    """Cache assessment results so switching tabs does not re-run the agents."""
    return assess(data_dir, str(ROOT / "config" / "schema"), str(ROOT / "config" / "rules"), TABLES, data_format)


def findings_to_frame(findings: list[Finding]) -> pd.DataFrame:
    """Flatten findings into a display DataFrame."""
    records: list[dict[str, object]] = []
    finding: Finding = None

    for finding in findings:
        records.append({
            "Dimension": finding.dimension.value,
            "Table": finding.table,
            "Record": finding.record_id,
            "Field": finding.field,
            "Issue": finding.issue,
            "Severity": finding.severity.value,
            "Observed": finding.observed_value,
            "Rule": finding.rule_id,
        })
    return pd.DataFrame(records)


def render_kpis(result: AssessmentResult) -> None:
    """Render the headline metric row."""
    columns = st.columns(4)
    mean_f1: float = 0.0
    values: list[float] = []
    name: str = ""

    columns[0].metric("Overall DQ score", f"{result.scorecard.overall_score_pct}%")
    columns[1].metric("Records assessed", f"{result.total_records:,}")
    columns[2].metric("Findings raised", f"{result.scorecard.total_findings:,}")
    if result.evaluation is not None:
        values = [result.evaluation[name].f1 for name in result.evaluation]
        mean_f1 = round(sum(values) / len(values), 3) if values else 0.0
        columns[3].metric("Mean F1 vs ground truth", mean_f1)
    else:
        columns[3].metric("Ground truth", "Real data")


def render_scorecard_tab(result: AssessmentResult) -> None:
    """Dimension scores, severity mix and the worst-offending fields."""
    dimension_rows: list[dict[str, object]] = []
    name: str = ""
    score = None
    severity_rows: list[dict[str, object]] = []
    field_rows: list[dict[str, object]] = []
    field: str = ""
    count: int = 0

    for name, score in result.scorecard.by_dimension.items():
        dimension_rows.append({"Dimension": name, "Score": score.score_pct, "Findings": score.findings})
    score_frame: pd.DataFrame = pd.DataFrame(dimension_rows)

    left, right = st.columns(2)
    with left:
        st.subheader("Score by dimension")
        figure = px.bar(
            score_frame, x="Dimension", y="Score", color="Score",
            color_continuous_scale=SCORE_SCALE, range_color=[0, 100], text="Score",
        )
        figure.update_yaxes(range=[0, 100], title="Score (%)")
        figure.update_layout(coloraxis_showscale=False, height=340)
        st.plotly_chart(figure, use_container_width=True)

    with right:
        st.subheader("Findings by severity")
        for name, count in result.scorecard.by_severity.items():
            severity_rows.append({"Severity": name, "Findings": count})
        severity_frame: pd.DataFrame = pd.DataFrame(severity_rows)
        figure_sev = px.bar(severity_frame, x="Severity", y="Findings", color="Severity", height=340)
        st.plotly_chart(figure_sev, use_container_width=True)

    st.subheader("Worst-offending fields")
    for field, count in result.scorecard.top_fields:
        field_rows.append({"Field": field, "Findings": count})
    field_frame: pd.DataFrame = pd.DataFrame(field_rows)
    figure_fields = px.bar(field_frame, x="Findings", y="Field", orientation="h", height=360)
    figure_fields.update_yaxes(autorange="reversed")
    st.plotly_chart(figure_fields, use_container_width=True)


def render_findings_tab(result: AssessmentResult) -> None:
    """A filterable, downloadable findings table."""
    frame: pd.DataFrame = findings_to_frame(result.findings)
    dimensions: list[str] = []
    tables: list[str] = []
    severities: list[str] = []
    filtered: pd.DataFrame = None

    if frame.empty:
        st.info("No findings for this dataset - the data passed every executed rule.")
        return

    controls = st.columns(3)
    dimensions = controls[0].multiselect("Dimension", sorted(frame["Dimension"].unique()))
    tables = controls[1].multiselect("Table", sorted(frame["Table"].unique()))
    severities = controls[2].multiselect("Severity", sorted(frame["Severity"].unique()))

    filtered = frame
    if dimensions:
        filtered = filtered[filtered["Dimension"].isin(dimensions)]
    if tables:
        filtered = filtered[filtered["Table"].isin(tables)]
    if severities:
        filtered = filtered[filtered["Severity"].isin(severities)]

    st.caption(f"Showing {len(filtered):,} of {len(frame):,} findings")
    st.dataframe(filtered, use_container_width=True, height=420)
    st.download_button(
        "Download filtered findings (CSV)",
        data=filtered.to_csv(index=False).encode("utf-8"),
        file_name="agentdq_findings.csv",
        mime="text/csv",
    )


def render_evidence_tab(result: AssessmentResult) -> None:
    """The precision/recall evidence: the credibility anchor for the results."""
    rows: list[dict[str, object]] = []
    name: str = ""
    row = None
    plot_rows: list[dict[str, object]] = []
    metric: str = ""

    if result.evaluation is None:
        st.info(
            "This dataset has no ground-truth labels, so precision and recall cannot be "
            "computed here. Select a synthetic scenario to see the evidence - those datasets "
            "carry a known defect for every issue, which is what makes measurement possible."
        )
        return

    st.subheader("Measured against known defects")
    st.caption(
        "On synthetic data every injected defect is recorded, so the system's findings can be "
        "scored exactly. Precision is the share of findings that are real; recall is the share of "
        "real defects that were caught."
    )

    for name, row in result.evaluation.items():
        rows.append({
            "Dimension": name,
            "True positives": row.true_positive,
            "False positives": row.false_positive,
            "False negatives": row.false_negative,
            "Precision": row.precision,
            "Recall": row.recall,
            "F1": row.f1,
        })
        for metric in ("Precision", "Recall", "F1"):
            plot_rows.append({"Dimension": name, "Metric": metric, "Value": getattr(row, metric.lower())})

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    figure = px.bar(
        pd.DataFrame(plot_rows), x="Dimension", y="Value", color="Metric",
        barmode="group", range_y=[0, 1.05], height=360,
    )
    st.plotly_chart(figure, use_container_width=True)


def render_about_tab() -> None:
    """What this proof of concept demonstrates."""
    dot: str = """
    digraph {
      rankdir=LR;
      node [shape=box, style="rounded,filled", fontname="Helvetica", fillcolor="#eef4ff"];
      edge [color="#8899bb"];
      Profile [label="Profile the data"];
      Suggest [label="Suggest rules (agent)", fillcolor="#fff4e6"];
      Approve [label="Human approves (gate)", fillcolor="#e8f7ec"];
      Execute [label="Execute approved rules"];
      Remediate [label="Prioritise + explain (agent)", fillcolor="#fff4e6"];
      Profile -> Suggest -> Approve -> Execute -> Remediate;
    }
    """
    demonstrates: list[dict[str, str]] = [
        {"Capability": "Rule suggestion", "How": "an agent proposes rules from profiled data, grounded in a rule bank and data-driven inference"},
        {"Capability": "Human approval gate", "How": "a data steward approves, edits or rejects each suggestion before it can run"},
        {"Capability": "Deterministic execution", "How": "approved rules run through an exact, reproducible pandas executor - no LLM in the measurement"},
        {"Capability": "Explained findings", "How": "each finding carries its rule, evidence and severity; ground-truth evaluation reports precision and recall"},
    ]

    st.subheader("What this proof of concept shows")
    st.write(
        "AgentDQ is a proof of concept and a portfolio piece. It shows how a multi-agent AI system "
        "can assess and quantify data quality for SAP master data, with the judgement placed where it "
        "belongs: an agent proposes rules, a human decides which to adopt, and a deterministic executor "
        "does the measuring. The agents reason only over provided evidence - the rule bank, the reference "
        "tables, the schema and the profile - never from model memory."
    )
    st.graphviz_chart(dot, use_container_width=True)
    st.subheader("What it demonstrates")
    st.dataframe(pd.DataFrame(demonstrates), use_container_width=True, hide_index=True)
    st.subheader("A note on portability")
    st.write(
        "Rules are compiled from a declarative representation rather than written as code, so the same "
        "approved rules could run on a different executor - for example SQL pushdown - without changing "
        "the agents. That portability is a property of the design, shown here on pandas; it is not a "
        "planned migration."
    )


def main() -> None:
    """Compose the dashboard."""
    st.set_page_config(page_title="AgentDQ", page_icon=None, layout="wide")
    datasets: list[dict[str, str]] = discover_datasets(ROOT)
    labels: list[str] = []
    chosen_label: str = ""
    chosen = None
    result: AssessmentResult = None

    st.title("AgentDQ - SAP Master Data Quality")
    st.caption("Agentic data quality assessment for SAP Material Master - proof of concept")

    if not datasets:
        st.error("No datasets found. Generate a synthetic dataset or place CAL extracts in data/raw.")
        return

    labels = [dataset["label"] for dataset in datasets]
    chosen_label = st.sidebar.selectbox("Dataset", labels)
    chosen = next(dataset for dataset in datasets if dataset["label"] == chosen_label)
    st.sidebar.caption(f"Source: {chosen['dir']}")
    st.sidebar.caption(f"Format: {chosen['format']}")

    result = cached_assess(chosen["dir"], chosen["format"])
    render_kpis(result)

    scorecard_tab, findings_tab, evidence_tab, about_tab = st.tabs(
        ["Scorecard", "Findings", "Evidence", "About"]
    )
    with scorecard_tab:
        render_scorecard_tab(result)
    with findings_tab:
        render_findings_tab(result)
    with evidence_tab:
        render_evidence_tab(result)
    with about_tab:
        render_about_tab()


main()
