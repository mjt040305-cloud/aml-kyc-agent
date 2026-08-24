"""
AML/KYC Compliance Flagging Agent
==================================
HBF2212 - Artificial Intelligence in Finance, Project 2

An autonomous agent pipeline for AML/KYC transaction monitoring:

  1. READ    -> user uploads a transaction CSV
  2. ANALYSE -> rules_engine.py scores every transaction against AML red flags
  3. DECIDE  -> transactions are bucketed into Low / Medium / High risk with
                explainable flag reasons
  4. HUMAN OVERSIGHT CHECKPOINT -> a compliance officer must review every
                Medium/High flagged transaction and Approve / Dismiss /
                Escalate before it is treated as final
  5. OUTPUT  -> a reviewed, exportable compliance report (CSV) suitable for
                internal audit or regulatory filing

Run locally with:  streamlit run app.py
"""

import streamlit as st
import pandas as pd
from datetime import datetime
from rules_engine import analyse_transactions

st.set_page_config(page_title="AML/KYC Compliance Agent", page_icon="🛡️", layout="wide")

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------
if "analysed_df" not in st.session_state:
    st.session_state.analysed_df = None
if "review_log" not in st.session_state:
    st.session_state.review_log = []

st.title("🛡️ AML/KYC Compliance Flagging Agent")
st.caption(
    "An autonomous agent that reads transaction data, flags suspicious activity "
    "against AML red-flag rules, and routes flagged items to a human compliance "
    "officer for final sign-off before any report is produced."
)

with st.expander("ℹ️ How this agent works (click to expand)"):
    st.markdown("""
    **Step 1 - Read:** Upload a CSV of transactions, or use the bundled sample data.

    **Step 2 - Analyse:** The agent runs six explainable AML rules over every
    transaction (structuring, high-risk jurisdictions, rapid fund movement,
    round-number amounts, deviation from customer history, and existing KYC
    risk profile).

    **Step 3 - Decide/Flag:** Each transaction receives a risk score and a
    Low / Medium / High bucket, with plain-English reasons.

    **Step 4 - Human Oversight Checkpoint:** *No transaction is automatically
    reported.* A compliance officer must review each Medium/High risk
    transaction below and Approve, Dismiss, or Escalate it, optionally with
    notes. This keeps a human accountable in the loop, consistent with
    AML/CFT regulatory expectations.

    **Step 5 - Output:** Download the final, human-reviewed compliance log.
    """)

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

if "raw_df" not in st.session_state:
    st.session_state.raw_df = None

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

        # -------------------------------------------------------------
        # STEP 2 & 3: ANALYSE + DECIDE/FLAG (runs automatically once data loads)
        # -------------------------------------------------------------
        if st.button("▶ Run compliance analysis", type="primary"):
            with st.spinner("Agent analysing transactions against AML rules..."):
                st.session_state.analysed_df = analyse_transactions(raw_df)
            st.rerun()

# ---------------------------------------------------------------------------
# STEP 2/3 RESULTS + STEP 4: HUMAN OVERSIGHT CHECKPOINT
# ---------------------------------------------------------------------------
if st.session_state.analysed_df is not None:
    df = st.session_state.analysed_df

    st.header("Step 2-3: Agent risk analysis")
    total = len(df)
    high = (df["risk_bucket"] == "High").sum()
    med = (df["risk_bucket"] == "Medium").sum()
    low = (df["risk_bucket"] == "Low").sum()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total transactions", total)
    m2.metric("🔴 High risk", high)
    m3.metric("🟠 Medium risk", med)
    m4.metric("🟡 Low risk", low)

    st.header("Step 4: Human Oversight Checkpoint")
    st.info(
        "The agent does **not** auto-finalise any flag. As the compliance "
        "officer, review each Medium/High risk transaction below and record "
        "a decision before generating the final report."
    )

    review_needed = df[df["risk_bucket"].isin(["High", "Medium"])].reset_index(drop=True)

    if review_needed.empty:
        st.success("No Medium/High risk transactions require review.")
    else:
        for i, row in review_needed.iterrows():
            badge = "🔴" if row["risk_bucket"] == "High" else "🟠"
            with st.container(border=True):
                st.markdown(
                    f"{badge} **{row['transaction_id']}** — Customer `{row['customer_id']}` — "
                    f"${row['amount']:,.2f} — {row['date']} — risk score **{row['risk_score']}**"
                )
                st.caption(f"Flag reasons: {row['flag_reasons']}")

                col_a, col_b, col_c = st.columns([1, 1, 2])
                with col_a:
                    decision = st.selectbox(
                        "Decision",
                        ["Pending", "Approve (false positive)", "Escalate to SAR filing", "Dismiss - insufficient grounds"],
                        key=f"decision_{row['transaction_id']}"
                    )
                with col_b:
                    reviewer = st.text_input("Reviewer name", key=f"reviewer_{row['transaction_id']}")
                with col_c:
                    notes = st.text_input("Notes (optional)", key=f"notes_{row['transaction_id']}")

                df.loc[df["transaction_id"] == row["transaction_id"], "review_status"] = decision
                df.loc[df["transaction_id"] == row["transaction_id"], "reviewer_notes"] = notes
                df.loc[df["transaction_id"] == row["transaction_id"], "reviewed_by"] = reviewer

    st.session_state.analysed_df = df

    # -----------------------------------------------------------------
    # STEP 5: OUTPUT
    # -----------------------------------------------------------------
    st.header("Step 5: Output - Compliance report")
    st.dataframe(
        df[["transaction_id", "customer_id", "amount", "date", "risk_bucket",
            "risk_score", "flag_reasons", "review_status", "reviewer_notes"]],
        use_container_width=True
    )

    pending_count = (df["review_status"] == "Pending").sum()
    if pending_count > 0:
        st.warning(f"⚠️ {pending_count} flagged transaction(s) still awaiting compliance officer decision.")

    csv_out = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇ Download final compliance report (CSV)",
        data=csv_out,
        file_name=f"aml_compliance_report_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        type="primary"
    )

st.divider()
st.caption(
    "Educational prototype using simulated data only. Not connected to any live "
    "banking system. Built for HBF2212 - Artificial Intelligence in Finance."
)
