# v0.4 | 10-Aug-2026 | Package 4f fix. The Settings tab names any block that was
#                      too large to compare. One oversized block used to end the
#                      whole run with a ValueError.
# v0.3 | 10-Aug-2026 | Package 4f. The dashboard moves onto the assessment
#                      graph. It reached the graph by calling assess(), which
#                      now builds and invokes it, so the screen shows the same
#                      four dimensions the console does. Adds a Duplicates tab
#                      and a read-only Settings tab, a rules-directory selector,
#                      the no-checks guard, and a banner when the matcher ran
#                      without its vectors.
# v0.2 | 26-Jul-2026 | Replace the Phase 2 migration tab with an About tab
#                      (POC framing; portability kept as a design property,
#                      not a roadmap); drop the "Phase 1 pilot" caption. The
#                      Consistency dimension now appears via assess() v0.2.
# v0.1 | 27-Jun-2026 | Initial Streamlit dashboard

"""AgentDQ dashboard.

A presentation layer over the shared assess() function, which runs the
assessment graph. It shows the data quality scorecard, lets a reader drill into
findings and duplicate clusters, presents the precision/recall evidence on
labelled data, shows the uniqueness settings in force, and explains what the
proof of concept demonstrates.

All the analysis lives in src/; this file is purely the view. The shaping of the
cluster and settings tables lives in src/reporting/cluster_view.py, so it can be
tested without starting Streamlit.

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

from src.agents.embedding_store import subject_tables  # v0.3
from src.contracts import Finding
from src.data.schema import load_schemas  # v0.3
from src.reporting.assessment import (  # v0.3
    AssessmentResult,
    assess,
    load_frames,
    no_checks_warning,
)
from src.reporting.cluster_view import (  # v0.3
    advisory_lines,
    candidate_pairs_frame,
    cluster_members_frame,
    cluster_overview_frame,
    compare_field_texts,
    decoy_frame,
    held_back_frame,
    match_mode_note,
    oversized_blocks_frame,
    score_spread_frame,
    settings_rows,
    twin_recall_frame,
)


TABLES: list[str] = ["MARA", "MARC", "MAKT"]
SCORE_SCALE: str = "RdYlGn"
SEVERITY_ORDER: list[str] = ["Critical", "High", "Medium", "Low"]
# Which rules the agents execute. config/rules is every imported rule; the
# approved directory is the export from the approval gate, so choosing it
# demonstrates the governance loop: only a rule a human approved can run.
RULE_SOURCES: dict[str, str] = {  # v0.3
    "All imported rules (config/rules)": "config/rules",
    "Approved rules only (data/approved)": "data/approved",
}
MAX_CLUSTERS_LISTED: int = 25  # v0.3


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
def cached_assess(data_dir: str, data_format: str, rules_dir: str) -> AssessmentResult:
    """Cache assessment results so switching tabs does not re-run the agents.

    The rules directory is part of the cache key, so changing it re-runs the
    agents rather than showing the previous run's numbers under a new label.
    """
    return assess(
        data_dir, str(ROOT / "config" / "schema"), str(ROOT / rules_dir), TABLES, data_format
    )  # v0.3


@st.cache_data(show_spinner="Reading the descriptions...")
def cached_compare_texts(data_dir: str, data_format: str) -> dict[str, dict[str, str]]:
    """The original text of every compare field, for the cluster tables.

    A cluster carries record keys and scores. Two keys and a number tell a
    reader nothing about why the records look alike, so the descriptions are
    read here and put beside each member.
    """
    frames = load_frames(data_dir, TABLES, data_format)  # v0.3
    schemas = load_schemas(str(ROOT / "config" / "schema"), TABLES)  # v0.3
    subjects: list[str] = subject_tables(schemas)  # v0.3

    if not subjects:
        return {}
    return compare_field_texts(frames, schemas, subjects[0])  # v0.3


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
    st.caption(  # v0.3
        "Uniqueness is absent from this table on purpose. The injector labels a duplicate "
        "twin and holds no view on which of the two records deserves to be kept, so a "
        "record-by-record score would measure a business judgement against a label with no "
        "opinion on it. Uniqueness is measured on clusters instead, and those numbers are "
        "on the Duplicates tab."
    )


def render_match_mode_banner(result: AssessmentResult) -> None:  # v0.3
    """Say which rungs of the score ladder ran, before anything else is read.

    A fuzzy-only run that looks like a full one is the failure this banner
    exists to prevent.
    """
    summary: dict = (result.uniqueness_settings or {}).get("summary", {})
    level: str = ""
    message: str = ""

    if not summary:
        return
    level, message = match_mode_note(summary)
    if level == "ok":
        st.caption(message)
    else:
        st.warning(message)


def render_duplicates_tab(result: AssessmentResult, texts: dict) -> None:  # v0.3
    """The clusters the matcher found, and the pairs it could not settle."""
    settings: dict = result.uniqueness_settings or {}
    summary: dict = settings.get("summary", {})
    pairs: list = settings.get("candidate_pairs", [])
    overview = cluster_overview_frame(result.clusters)
    labels: list[str] = []
    chosen: str = ""
    cluster = None
    assessed: int = int(summary.get("records_assessed", 0) or 0)
    metrics = None

    render_match_mode_banner(result)

    if not summary:
        st.info(
            "The uniqueness stage did not run for this dataset. It needs a table whose "
            "schema declares blocking keys and compare fields."
        )
        return
    if assessed == 0:
        st.error(
            "0 records were compared, so the Uniqueness score means 'checked nothing' "
            "rather than 'no duplicates'. Every record was held back, most often because "
            "no description could be read."
        )

    metrics = st.columns(4)
    metrics[0].metric("Clusters found", f"{len(result.clusters):,}")
    metrics[1].metric("Records compared", f"{assessed:,}")
    metrics[2].metric("Records held back", f"{summary.get('held_back_total', 0):,}")
    metrics[3].metric("Pairs awaiting a decision", f"{summary.get('candidate_pairs', 0):,}")

    st.subheader("Clusters")
    st.caption(
        "A cluster is a group of records that appear to describe one thing. The agent "
        "recommends one record to keep and merges nothing. Weakest link is the lowest "
        "score of any pair inside the cluster: a low number means the group is held "
        "together by a chain rather than by mutual agreement."
    )
    if overview.empty:
        st.info("No duplicate clusters were found in this dataset.")
    else:
        st.dataframe(overview, use_container_width=True, hide_index=True, height=320)
        st.download_button(
            "Download clusters (CSV)",
            data=overview.to_csv(index=False).encode("utf-8"),
            file_name="agentdq_clusters.csv",
            mime="text/csv",
        )
        st.subheader("Inside a cluster")
        labels = [str(value) for value in overview["Cluster"].tolist()[:MAX_CLUSTERS_LISTED]]
        chosen = st.selectbox("Cluster", labels)
        for cluster in result.clusters:
            if cluster.cluster_id == chosen:
                st.dataframe(
                    cluster_members_frame(cluster, texts),
                    use_container_width=True, hide_index=True,
                )
                st.caption(
                    f"Keep {cluster.survivor_id} ({cluster.survivor_reason.value}). "
                    f"Resolution: {cluster.resolution.value}. "
                    f"A member marked 'Below band' joined through a chain and deserves "
                    f"a closer look."
                )

    st.subheader("Pairs awaiting a decision")
    st.caption(
        "These pairs scored inside the review band, so the deterministic methods could "
        "not settle them. They form no cluster and raise no finding. The adjudicator in "
        "Package 4g takes them, which is why model cost follows real ambiguity rather "
        "than dataset size."
    )
    if not pairs:
        st.info("No pairs fell in the review band.")
    else:
        st.dataframe(
            candidate_pairs_frame(pairs, texts),
            use_container_width=True, hide_index=True, height=280,
        )

    st.subheader("How the scored pairs fell")
    st.dataframe(score_spread_frame(summary), use_container_width=True, hide_index=True)
    render_uniqueness_evaluation(result)


def render_uniqueness_evaluation(result: AssessmentResult) -> None:  # v0.3
    """Cluster-level measurement, on datasets that carry labels."""
    evaluation = result.uniqueness_evaluation
    recall = None
    decoys = None
    metrics = None

    st.subheader("How well the matcher did")
    if evaluation is None:
        st.info(
            "This dataset carries no injected labels, so the matcher cannot be scored "
            "here. Select a synthetic scenario to see twin recall and the decoy error "
            "rate."
        )
        return

    recall = evaluation.twin_recall
    decoys = evaluation.decoy_result
    metrics = st.columns(3)
    metrics[0].metric("Twin recall", f"{recall.recall_pct}%",
                      help="the share of injected duplicate twins found")
    metrics[1].metric("Decoy error rate", f"{decoys.error_rate_pct}%",
                      help="the share of decoy pairs wrongly joined; the headline precision figure")
    metrics[2].metric("Unlabelled joins", f"{evaluation.unlabelled_joins:,}",
                      help="joined pairs that neither the labels nor the decoys knew about")
    st.caption(
        "A decoy is two different materials given a confusable description, so every "
        "decoy joined into one cluster is a false positive with no argument. Unlabelled "
        "joins are reported as a count rather than a rate, because some are genuine "
        "duplicates that carry no label."
    )
    if recall.hidden_by_other_defect:
        st.caption(
            f"{recall.hidden_by_other_defect} twin(s) had a blocking key corrupted by "
            f"another defect, so no matcher could have found them. Recall on the "
            f"matchable set is {recall.recall_on_matchable_pct}%."
        )
    left, right = st.columns(2)
    with left:
        st.caption("Twin recall by the change that made the twin")
        st.dataframe(twin_recall_frame(evaluation), use_container_width=True, hide_index=True)
    with right:
        st.caption("Decoy errors by kind")
        st.dataframe(decoy_frame(evaluation), use_container_width=True, hide_index=True)


def render_settings_tab(result: AssessmentResult) -> None:  # v0.3
    """The uniqueness settings in force. Read-only.

    Writing back is deliberately absent. The schema YAML files are generated by
    tools/build_schema.py, so a screen that wrote to them would lose its writes
    on the next rebuild.
    """
    settings: dict = result.uniqueness_settings or {}
    resolved: dict = settings.get("resolved", {})
    summary: dict = settings.get("summary", {})
    lines: list[str] = advisory_lines(settings)
    held = None
    line: str = ""

    if not resolved:
        st.info("No uniqueness settings were resolved for this dataset.")
        return

    st.subheader("Settings in force")
    st.caption(
        "A steward writes these in the table schema. An advisory from another agent can "
        "raise the bands, and both numbers are shown so nobody concludes their setting "
        "was ignored. This screen is read-only: the schema files are generated, so a "
        "write here would be erased on the next rebuild."
    )
    st.dataframe(
        pd.DataFrame(settings_rows(resolved, summary)),
        use_container_width=True, hide_index=True,
    )

    st.subheader("Advice from other agents")
    if not lines:
        st.info("No agent sent advice to the uniqueness stage on this run.")
    for line in lines:
        st.write(f"- {line}")

    st.subheader("Records held back")
    st.caption(
        "A held-back record took no part in matching, so it is not a record that was "
        "found to be unique. It is a record that was never compared."
    )
    held = held_back_frame(summary)
    if held.empty:
        st.info("No records were held back.")
    else:
        st.dataframe(held, use_container_width=True, hide_index=True)

    oversized = oversized_blocks_frame(summary)  # v0.4
    if not oversized.empty:  # v0.4
        st.subheader("Blocks that were too large to compare")
        st.warning(
            "Comparison inside a block is all-pairs, so a block of n records costs "
            "n(n-1)/2 comparisons. These blocks were past the ceiling, so they were "
            "held back whole and their records left the uniqueness score. Raise "
            "max_block_pairs in the table schema, or add a blocking key to make the "
            "blocks smaller."
        )
        st.dataframe(oversized, use_container_width=True, hide_index=True)


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
    rules_label: str = ""  # v0.3
    rules_dir: str = ""  # v0.3
    texts: dict = {}  # v0.3
    warning = None  # v0.3

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

    rules_label = st.sidebar.selectbox("Rules", list(RULE_SOURCES))  # v0.3
    rules_dir = RULE_SOURCES[rules_label]  # v0.3
    st.sidebar.caption(  # v0.3
        "Approved rules are the export from the approval gate, so choosing them shows the "
        "governance loop: only a rule a human approved can run."
    )

    result = cached_assess(chosen["dir"], chosen["format"], rules_dir)  # v0.3
    texts = cached_compare_texts(chosen["dir"], chosen["format"])  # v0.3
    warning = no_checks_warning(result.rules_loaded, result.rules_run, result.rules_dir)  # v0.3

    if warning is not None:  # v0.3
        st.error(warning)
    render_kpis(result)

    scorecard_tab, findings_tab, duplicates_tab, evidence_tab, settings_tab, about_tab = st.tabs(
        ["Scorecard", "Findings", "Duplicates", "Evidence", "Settings", "About"]
    )  # v0.3
    with scorecard_tab:
        render_scorecard_tab(result)
    with findings_tab:
        render_findings_tab(result)
    with duplicates_tab:  # v0.3
        render_duplicates_tab(result, texts)
    with evidence_tab:
        render_evidence_tab(result)
    with settings_tab:  # v0.3
        render_settings_tab(result)
    with about_tab:
        render_about_tab()


main()
