"""
AML/KYC Compliance Flagging Agent
==================================
HBF2212 - Artificial Intelligence in Finance, Project 2

An AI agent for AML/KYC transaction monitoring, orchestrated as a
LangGraph state graph (see agent_graph.py):

  START -> analyse -> human_review (interrupt) -> output -> END

  1. READ    -> user uploads a transaction CSV
  2. ANALYSE -> rules_engine.py scores every transaction across four risk
                categories: Customer, Transaction, Geographic, Behavioural
                using thresholds configurable from the sidebar
  3. DECIDE  -> transactions are bucketed into Low / Medium / High risk,
                with every triggered rule shown and explained
  4. HUMAN OVERSIGHT CHECKPOINT -> the graph genuinely pauses execution
                (via LangGraph's interrupt()) until a compliance officer
                supplies a timestamped decision for every Medium/High risk
                transaction
  5. OUTPUT  -> the graph resumes and produces a reviewed, exportable
                compliance report (CSV + Excel + PDF) plus a timestamped
                audit trail, SAR case reference numbers, and a configurable
                reporting currency (USD/ZAR/ZiG/GBP/EUR)

Run locally with:  streamlit run app.py
"""

import streamlit as st
import pandas as pd
from datetime import datetime
import uuid
import tempfile
import os
from io import BytesIO

from agent_graph import build_agent_graph, run_pipeline, resume_pipeline
from rules_engine import SEVERITY_ICON, CATEGORIES, DEFAULT_CONFIG
from pdf_report import build_pdf
from currency import CURRENCY_OPTIONS, format_amount
import regulatory_watch

st.set_page_config(page_title="AML/KYC Compliance Agent", page_icon="\U0001F6E1\uFE0F", layout="wide")

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------
defaults = {
    "raw_df": None,
    "thread_id": str(uuid.uuid4()),
    "pipeline_status": None,          # None | "awaiting_review" | "complete"
    "pending_transactions": [],
    "final_report": None,
    "rules_config": dict(DEFAULT_CONFIG),
    "audit_trail": [],                # list of {transaction_id, status, reviewer, notes, reviewed_at}
    "currency_code": "USD",
    "fx_rate": 1.0,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


@st.cache_resource
def get_graph():
    return build_agent_graph()

graph = get_graph()

# ---------------------------------------------------------------------------
# SIDEBAR: configurable rule thresholds (institution risk appetite)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("\u2699\uFE0F Agent Configuration")
    st.caption("Tune AML rule thresholds to your institution's risk appetite.")

    structuring_threshold = st.number_input(
        "Structuring / reporting threshold ($)", min_value=1000, max_value=100000,
        value=st.session_state.rules_config["structuring_threshold"], step=500
    )
    structuring_margin = st.slider(
        "Structuring margin (% below threshold)", 1, 30,
        int(st.session_state.rules_config["structuring_margin"] * 100)
    ) / 100

    all_countries = ["north korea", "iran", "myanmar", "afghanistan", "syria", "somalia",
                      "venezuela", "yemen", "libya"]
    high_risk_countries = st.multiselect(
        "High-risk jurisdictions", options=all_countries,
        default=[c for c in st.session_state.rules_config["high_risk_countries"] if c in all_countries]
    )

    rapid_hours = st.slider("Rapid movement window (hours)", 1, 72, st.session_state.rules_config["rapid_movement_hours"])
    rapid_min_txns = st.slider("Rapid movement min. transactions", 2, 10, st.session_state.rules_config["rapid_movement_min_txns"])
    velocity_mult = st.slider("Velocity deviation (std. deviations)", 1, 6, st.session_state.rules_config["velocity_std_multiplier"])

    st.session_state.rules_config = {
        "structuring_threshold": structuring_threshold,
        "structuring_margin": structuring_margin,
        "high_risk_countries": high_risk_countries,
        "rapid_movement_hours": rapid_hours,
        "rapid_movement_min_txns": rapid_min_txns,
        "round_number_multiple": DEFAULT_CONFIG["round_number_multiple"],
        "round_number_min_amount": DEFAULT_CONFIG["round_number_min_amount"],
        "velocity_std_multiplier": velocity_mult,
        "high_risk_profile_min_amount": DEFAULT_CONFIG["high_risk_profile_min_amount"],
    }

    st.caption("Changes apply the next time you click **Run compliance analysis**.")

    st.divider()
    st.header("\U0001F4B1 Reporting Currency")
    st.caption("Transaction data is recorded in USD. AML thresholds are always evaluated in USD - this only changes how amounts are *displayed*.")

    currency_code = st.selectbox(
        "Currency", options=list(CURRENCY_OPTIONS.keys()),
        format_func=lambda c: f"{c} \u2014 {CURRENCY_OPTIONS[c]['name']}",
        index=list(CURRENCY_OPTIONS.keys()).index(st.session_state.currency_code),
    )
    default_rate = CURRENCY_OPTIONS[currency_code]["default_rate"]
    fx_rate = st.number_input(
        f"Exchange rate (1 USD = ? {currency_code})",
        min_value=0.0001, value=float(default_rate), step=0.01, format="%.4f",
    )
    st.caption("\u26A0\uFE0F Officer-entered rate for this session only \u2014 not a live FX feed. Verify against your institution's official rate before filing.")

    st.session_state.currency_code = currency_code
    st.session_state.fx_rate = fx_rate

st.title("\U0001F6E1\uFE0F AML/KYC Compliance Flagging Agent")
st.caption(
    "A LangGraph-orchestrated agent that reads transaction data, scores it across "
    "four risk categories, and pauses at a human oversight checkpoint before any "
    "compliance report is produced."
)

with st.expander("\u2139\uFE0F Agent architecture (click to expand)"):
    st.markdown("""
    This agent is orchestrated as an explicit **LangGraph state graph**, not a
    linear script:

    ```
    START -> analyse -> human_review (interrupt) -> output -> END
    ```

    - **analyse**: runs the AML rules engine (with your sidebar-configured
      thresholds) and produces a risk score per transaction, broken into
      Customer / Transaction / Geographic / Behavioural risk.
    - **human_review**: calls LangGraph's `interrupt()`. Execution genuinely
      **pauses** here - the graph will not proceed to `output` until a
      compliance officer supplies a decision for every flagged transaction.
    - **output**: resumes once decisions are supplied, merges them into the
      final report and timestamped audit trail.

    No transaction is ever auto-reported - the graph structurally cannot
    reach `output` for a flagged transaction without a human decision.
    """)

with st.expander("\U0001F4DC AML/CFT Regulatory Guidelines (Zimbabwe)"):
    st.caption(
        "The rules in this agent are grounded in Zimbabwe's AML/CFT regulatory "
        "framework, summarised below. This is informational context, not legal "
        "advice - always consult the source documents and your institution's "
        "compliance officer."
    )
    for item in regulatory_watch.STATIC_FRAMEWORK:
        with st.container(border=True):
            title_line = f"**{item['title']}**  \u2014  *{item['role']}*"
            st.markdown(title_line)
            st.caption(item["note"])
            if item.get("url"):
                st.markdown(f"[Source document]({item['url']})")

    st.divider()
    st.markdown("**Live check: Reserve Bank of Zimbabwe guideline list**")
    st.caption(
        f"Bundled snapshot last taken {regulatory_watch.SNAPSHOT_DATE}. "
        "This checks whether the RBZ has published new/renamed guidelines since then - "
        "it does not read or summarise their content."
    )
    if st.button("\U0001F504 Check RBZ site for updates now"):
        with st.spinner("Fetching the live RBZ guidelines page..."):
            result = regulatory_watch.check_for_updates()
        if result["status"] == "error":
            st.warning(f"\u26A0\uFE0F Live check failed: {result['message']} Falling back to the bundled snapshot above.")
        else:
            st.success(f"Checked at {result['checked_at']} \u2014 {result['total_found']} guideline documents found on the live RBZ page.")
            if result["new_or_changed"]:
                st.warning("\U0001F195 Possibly new or renamed since the bundled snapshot \u2014 review manually:")
                for title in result["new_or_changed"]:
                    st.markdown(f"- {title}")
            else:
                st.info("No new guideline titles detected since the bundled snapshot.")

# ---------------------------------------------------------------------------
# STEP 1: READ INPUT
# ---------------------------------------------------------------------------
st.header("Step 1: Load transaction data")

col1, col2 = st.columns([2, 1])
with col1:
    uploaded_file = st.file_uploader("Upload transaction CSV", type=["csv"])
with col2:
    use_sample = st.button("Use sample data instead", use_container_width=True)

required_cols = {
    "transaction_id", "customer_id", "date", "amount",
    "counterparty_country", "transaction_type", "customer_risk_profile"
}

if uploaded_file is not None:
    st.session_state.raw_df = pd.read_csv(uploaded_file)
elif use_sample:
    st.session_state.raw_df = pd.read_csv("sample_transactions.csv")

raw_df = st.session_state.raw_df

if raw_df is not None:
    missing = required_cols - set(raw_df.columns)
    if missing:
        st.error(f"Uploaded file is missing required columns: {', '.join(missing)}")
    else:
        st.success(f"Loaded {len(raw_df)} transactions.")
        st.dataframe(raw_df.head(10), use_container_width=True)

        if st.button("\u25B6 Run compliance analysis", type="primary"):
            with st.spinner("Agent analysing transactions across four risk categories..."):
                st.session_state.thread_id = str(uuid.uuid4())  # fresh run each time
                st.session_state.audit_trail = []
                result = run_pipeline(
                    graph, raw_df.to_dict("records"), st.session_state.thread_id,
                    rules_config=st.session_state.rules_config
                )
            if result["status"] == "awaiting_review":
                st.session_state.pipeline_status = "awaiting_review"
                st.session_state.pending_transactions = result["pending_transactions"]
                st.session_state.final_report = None
            else:
                st.session_state.pipeline_status = "complete"
                st.session_state.final_report = result["final_report"]
            st.rerun()

# ---------------------------------------------------------------------------
# STEP 2-3: RISK ANALYSIS SUMMARY + DASHBOARD
# ---------------------------------------------------------------------------
def render_risk_breakdown(txn):
    """Render the Customer/Transaction/Geographic/Behavioural score bars + rule list."""
    cat_scores = txn.get("category_scores", {})
    st.caption("Overall Risk Score = Customer Risk + Transaction Risk + Geographic Risk + Behavioural Risk")
    cols = st.columns(4)
    for i, cat in enumerate(CATEGORIES):
        with cols[i]:
            st.metric(cat, cat_scores.get(cat, 0))

    st.markdown("**AML Rules Triggered**")
    triggered = txn.get("triggered_rules", [])
    if not triggered:
        st.markdown(f"{SEVERITY_ICON['Low']} Customer normally has low-risk activity \u2014 no rules triggered")
    else:
        for r in triggered:
            icon = SEVERITY_ICON.get(r["severity"], "")
            st.markdown(f"{icon} **{r['label']}** ({r['category']}) \u2014 {r['reason']}")


def render_dashboard(all_txns):
    """Executive KPI cards + charts summarising the whole flagged population."""
    currency_code = st.session_state.currency_code
    fx_rate = st.session_state.fx_rate

    flagged = [t for t in all_txns if t["risk_bucket"] in ("High", "Medium")]
    flagged_amount = sum(t["amount"] for t in flagged)
    avg_score = (sum(t["risk_score"] for t in flagged) / len(flagged)) if flagged else 0
    pct_review = (len(flagged) / len(all_txns) * 100) if all_txns else 0

    k1, k2, k3 = st.columns(3)
    k1.metric(f"Total value flagged ({currency_code})", format_amount(flagged_amount, currency_code, fx_rate))
    k2.metric("Avg. score (flagged)", f"{avg_score:.0f}")
    k3.metric("% requiring review", f"{pct_review:.0f}%")

    c1, c2 = st.columns(2)
    with c1:
        st.caption("Risk distribution")
        bucket_counts = pd.Series([t["risk_bucket"] for t in all_txns]).value_counts()
        st.bar_chart(bucket_counts)
    with c2:
        st.caption("Geographic risk contribution (by country)")
        geo_rows = [
            {"country": t["counterparty_country"], "geo_score": t.get("category_scores", {}).get("Geographic", 0)}
            for t in flagged
        ]
        if geo_rows:
            geo_df = pd.DataFrame(geo_rows).groupby("country")["geo_score"].sum().sort_values(ascending=False)
            st.bar_chart(geo_df)
        else:
            st.caption("No flagged transactions to summarise.")

    st.caption("Most frequently triggered AML rules (flagged transactions)")
    rule_labels = [r["label"] for t in flagged for r in t.get("triggered_rules", [])]
    if rule_labels:
        rule_counts = pd.Series(rule_labels).value_counts()
        st.bar_chart(rule_counts)
    else:
        st.caption("No rules triggered.")


if st.session_state.pipeline_status in ("awaiting_review", "complete"):
    all_txns = (
        st.session_state.pending_transactions
        if st.session_state.pipeline_status == "awaiting_review"
        else st.session_state.final_report
    )
    # for the metrics header we need the full population; when awaiting review
    # we only have the pending subset, so approximate total from raw_df
    total = len(st.session_state.raw_df) if st.session_state.raw_df is not None else len(all_txns)
    high = sum(1 for t in all_txns if t["risk_bucket"] == "High")
    med = sum(1 for t in all_txns if t["risk_bucket"] == "Medium")

    st.header("Step 2-3: Agent risk analysis")
    m1, m2, m3 = st.columns(3)
    m1.metric("Total transactions", total)
    m2.metric("\U0001F534 High risk", high)
    m3.metric("\U0001F7E0 Medium risk", med)

    with st.expander("\U0001F4CA Executive dashboard", expanded=True):
        render_dashboard(all_txns)

    # -----------------------------------------------------------------
    # STEP 4: HUMAN OVERSIGHT CHECKPOINT (graph is paused via interrupt)
    # -----------------------------------------------------------------
    if st.session_state.pipeline_status == "awaiting_review":
        st.header("Step 4: Human Oversight Checkpoint")
        st.info(
            "\u23F8\uFE0F The agent graph is **paused** at the human_review node. It will not "
            "produce Step 5 output until you record a decision for every transaction below."
        )

        decisions = {}
        for txn in st.session_state.pending_transactions:
            badge = "\U0001F534" if txn["risk_bucket"] == "High" else "\U0001F7E0"
            with st.container(border=True):
                st.markdown(
                    f"{badge} **{txn['transaction_id']}** \u2014 Customer `{txn['customer_id']}` \u2014 "
                    f"{format_amount(txn['amount'], st.session_state.currency_code, st.session_state.fx_rate)} "
                    f"\u2014 {txn['date']} \u2014 **Overall risk score: {txn['risk_score']}**"
                )
                render_risk_breakdown(txn)

                col_a, col_b, col_c = st.columns([1, 1, 2])
                with col_a:
                    status = st.selectbox(
                        "Decision",
                        ["Pending", "Approve (false positive)", "Escalate to SAR filing", "Dismiss - insufficient grounds"],
                        key=f"decision_{txn['transaction_id']}"
                    )
                with col_b:
                    reviewer = st.text_input("Reviewer name", key=f"reviewer_{txn['transaction_id']}")
                with col_c:
                    notes = st.text_input("Notes (optional)", key=f"notes_{txn['transaction_id']}")

                decisions[txn["transaction_id"]] = {"status": status, "reviewer": reviewer, "notes": notes}

        still_pending = sum(1 for d in decisions.values() if d["status"] == "Pending")
        if still_pending > 0:
            st.warning(f"\u26A0\uFE0F {still_pending} transaction(s) still marked Pending. The agent will remain paused until every transaction has a decision.")

        if st.button("\u2705 Submit reviews & resume agent", type="primary", disabled=(still_pending > 0)):
            reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for txn_id, d in decisions.items():
                d["reviewed_at"] = reviewed_at
                st.session_state.audit_trail.append({
                    "transaction_id": txn_id, "status": d["status"],
                    "reviewer": d["reviewer"], "notes": d["notes"], "reviewed_at": reviewed_at,
                })
            with st.spinner("Resuming agent graph and generating final report..."):
                result = resume_pipeline(graph, decisions, st.session_state.thread_id)
            st.session_state.pipeline_status = "complete"
            st.session_state.final_report = result["final_report"]
            st.rerun()

    # -----------------------------------------------------------------
    # STEP 5: OUTPUT
    # -----------------------------------------------------------------
    if st.session_state.pipeline_status == "complete":
        st.header("Step 5: Output - Compliance report")
        st.success("\u2705 Agent graph reached the output node \u2014 all flagged transactions have a recorded human decision.")

        currency_code = st.session_state.currency_code
        fx_rate = st.session_state.fx_rate

        report_df = pd.DataFrame(st.session_state.final_report)
        report_df[f"amount_{currency_code}"] = report_df["amount"] * fx_rate
        if "case_reference" not in report_df.columns:
            report_df["case_reference"] = ""

        display_cols = ["transaction_id", "customer_id", "amount", f"amount_{currency_code}", "date",
                         "risk_bucket", "risk_score", "case_reference", "flag_reasons",
                         "review_status", "reviewed_by", "reviewer_notes"]
        st.dataframe(
            report_df[display_cols].rename(columns={"amount": "amount_USD"}),
            use_container_width=True
        )
        st.caption(f"amount_USD is the original recorded value; amount_{currency_code} is converted at your sidebar rate (1 USD = {fx_rate:.4f} {currency_code}).")

        with st.expander("View full risk breakdown for a transaction"):
            txn_id = st.selectbox("Select transaction", report_df["transaction_id"].tolist())
            selected = next(t for t in st.session_state.final_report if t["transaction_id"] == txn_id)
            render_risk_breakdown(selected)

        with st.expander("\U0001F50D Customer transaction history"):
            customer_ids = sorted(report_df["customer_id"].unique().tolist())
            selected_customer = st.selectbox("Select customer", customer_ids)
            cust_df = report_df[report_df["customer_id"] == selected_customer].sort_values("date")
            st.dataframe(
                cust_df[["transaction_id", "date", "amount", f"amount_{currency_code}", "risk_bucket", "risk_score", "review_status"]],
                use_container_width=True
            )
            if len(cust_df) > 1:
                chart_df = cust_df[["date", "amount", "risk_score"]].set_index("date")
                c1, c2 = st.columns(2)
                with c1:
                    st.caption("Transaction amounts over time (USD)")
                    st.line_chart(chart_df["amount"])
                with c2:
                    st.caption("Risk score over time")
                    st.line_chart(chart_df["risk_score"])
            else:
                st.caption("Only one transaction on record for this customer.")

        if st.session_state.audit_trail:
            with st.expander("\U0001F4CB Timestamped audit trail"):
                audit_df = pd.DataFrame(st.session_state.audit_trail)
                st.dataframe(audit_df, use_container_width=True)
                st.download_button(
                    "\u2B07 Download audit trail (CSV)",
                    data=audit_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"aml_audit_trail_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                    mime="text/csv",
                )

        dl1, dl2, dl3 = st.columns(3)
        with dl1:
            csv_out = report_df[display_cols + ["category_scores"]].rename(columns={"amount": "amount_USD"}).to_csv(index=False).encode("utf-8")
            st.download_button(
                "\u2B07 Download report (CSV)",
                data=csv_out,
                file_name=f"aml_compliance_report_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                type="primary"
            )
        with dl2:
            if st.button("\U0001F4CA Generate Excel report"):
                with st.spinner("Building Excel workbook..."):
                    excel_buf = BytesIO()
                    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
                        report_df[display_cols].rename(columns={"amount": "amount_USD"}).to_excel(
                            writer, sheet_name="Compliance Report", index=False
                        )
                        if st.session_state.audit_trail:
                            pd.DataFrame(st.session_state.audit_trail).to_excel(
                                writer, sheet_name="Audit Trail", index=False
                            )
                    excel_bytes = excel_buf.getvalue()
                st.download_button(
                    "\u2B07 Download report (Excel)",
                    data=excel_bytes,
                    file_name=f"aml_compliance_report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
        with dl3:
            if st.button("\U0001F4C4 Generate PDF report"):
                with st.spinner("Building PDF..."):
                    tmp_path = os.path.join(tempfile.gettempdir(), "aml_compliance_report.pdf")
                    build_pdf(
                        st.session_state.final_report, st.session_state.rules_config, tmp_path,
                        currency_code=currency_code, fx_rate=fx_rate,
                    )
                    with open(tmp_path, "rb") as f:
                        pdf_bytes = f.read()
                st.download_button(
                    "\u2B07 Download report (PDF)",
                    data=pdf_bytes,
                    file_name=f"aml_compliance_report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                    mime="application/pdf",
                )

st.divider()
st.caption(
    "Educational prototype using simulated data only. Not connected to any live "
    "banking system. Built for HBF2212 - Artificial Intelligence in Finance."
)
