"""
AML/KYC Compliance Flagging Agent
==================================
HBF2212 - Artificial Intelligence in Finance, Project 2

An AI agent for AML/KYC transaction monitoring, orchestrated as a
LangGraph state graph (see agent_graph.py):

  START -> normalize -> analyse -> human_review (interrupt) -> output -> END

  1. READ       -> user uploads a transaction CSV (USD, ZAR, and/or ZiG)
  2. NORMALIZE  -> every transaction is converted to its USD equivalent
                    (fx_normalize.py) - USD is the fixed AML baseline
                    currency; original amount/currency are always preserved
  3. ANALYSE    -> rules_engine.py scores every USD-normalized transaction
                    across four risk categories: Customer, Transaction,
                    Geographic (per the institution's own country risk
                    classifications), and Behavioural
  4. DECIDE     -> transactions are bucketed into Low / Medium / High risk,
                    with every triggered rule shown and explained
  5. HUMAN OVERSIGHT CHECKPOINT -> the graph genuinely pauses execution
                    (via LangGraph's interrupt()) until a compliance
                    officer supplies a timestamped decision for every
                    Medium/High risk transaction
  6. OUTPUT     -> the graph resumes and produces a reviewed, exportable
                    compliance report (CSV + Excel + PDF) plus a
                    timestamped audit trail and SAR case reference numbers

This agent flags transactions and provides reasons/evidence for human
compliance review - it never autonomously determines that a transaction
is illegal, and no transaction is ever auto-reported.

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
from fx_normalize import SUPPORTED_CURRENCIES, CURRENCY_LABELS, FALLBACK_STARTING_RATE_TO_USD, fetch_live_rate
from countries_list import ALL_COUNTRIES
import fatf_reference
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
    # FX state: rate/source per currency, resolved via live fetch or manual override
    "fx_rates": {"USD": 1.0},
    "fx_sources": {"USD": {"source": "Fixed", "timestamp": ""}},
    # Country risk classification: {country_lower: "Low"|"Medium"|"High"|"Prohibited/Restricted"}
    "country_classifications": {},
    "fatf_check_result": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


@st.cache_resource
def get_graph():
    return build_agent_graph()

graph = get_graph()

# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("\u2699\uFE0F Agent Configuration")

    # -------------------- Institutional monitoring thresholds --------------------
    st.subheader("Institutional Monitoring Thresholds")
    st.caption(
        "This is an institution-configured transaction-monitoring threshold based on the "
        "institution's risk appetite. It is not presented as a universal RBZ suspicious "
        "transaction reporting threshold."
    )

    structuring_threshold = st.number_input(
        "Institutional Structuring Alert Threshold (USD)", min_value=1000, max_value=100000,
        value=st.session_state.rules_config["structuring_threshold"], step=500
    )
    structuring_margin = st.slider(
        "Structuring margin (% below threshold)", 1, 30,
        int(st.session_state.rules_config["structuring_margin"] * 100)
    ) / 100
    rapid_hours = st.slider("Rapid movement window (hours)", 1, 72, st.session_state.rules_config["rapid_movement_hours"])
    rapid_min_txns = st.slider("Rapid movement min. transactions", 2, 10, st.session_state.rules_config["rapid_movement_min_txns"])
    velocity_mult = st.slider("Velocity deviation (std. deviations)", 1, 6, st.session_state.rules_config["velocity_std_multiplier"])

    st.session_state.rules_config = {
        "structuring_threshold": structuring_threshold,
        "structuring_margin": structuring_margin,
        "country_classifications": st.session_state.country_classifications,
        "rapid_movement_hours": rapid_hours,
        "rapid_movement_min_txns": rapid_min_txns,
        "round_number_multiple": DEFAULT_CONFIG["round_number_multiple"],
        "round_number_min_amount": DEFAULT_CONFIG["round_number_min_amount"],
        "velocity_std_multiplier": velocity_mult,
        "high_risk_profile_min_amount": DEFAULT_CONFIG["high_risk_profile_min_amount"],
    }
    st.caption("Changes apply the next time you click **Run compliance analysis**.")

    # -------------------- Currency & FX Normalization --------------------
    st.divider()
    st.header("\U0001F4B1 Currency & FX Normalization")
    st.info(
        "All uploaded transaction currencies are normalized to USD before AML compliance "
        "evaluation. Original transaction amounts and currencies are preserved for auditability."
    )
    st.markdown(f"**Accepted transaction currencies:** {', '.join(SUPPORTED_CURRENCIES)}")
    st.markdown("**AML baseline currency:** `USD` \U0001F512 *(locked - not user-changeable)*")

    for code in SUPPORTED_CURRENCIES:
        if code == "USD":
            continue
        with st.expander(f"{code} \u2014 {CURRENCY_LABELS[code]}", expanded=(code == "ZWG")):
            col_a, col_b = st.columns([1, 1])
            with col_a:
                if st.button(f"\U0001F504 Fetch live rate", key=f"live_{code}"):
                    rate, info = fetch_live_rate(code)
                    if rate is not None:
                        st.session_state.fx_rates[code] = rate
                        st.session_state.fx_sources[code] = {"source": "Live", "timestamp": info.get("timestamp", "")}
                        st.success(info["message"])
                    else:
                        st.warning(f"\u26A0\uFE0F Live FX unavailable: {info['message']}")
            with col_b:
                current_rate = st.session_state.fx_rates.get(code, FALLBACK_STARTING_RATE_TO_USD[code])
                manual_rate = st.number_input(
                    f"Manual override (1 {code} = ? USD)", min_value=0.0000001,
                    value=float(current_rate), step=0.0001, format="%.6f", key=f"manual_{code}"
                )
                if st.button(f"Apply manual override", key=f"apply_{code}"):
                    st.session_state.fx_rates[code] = manual_rate
                    st.session_state.fx_sources[code] = {
                        "source": "Manual Override",
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    st.success(f"Manual override recorded for {code}. This does not modify any original transaction data.")

            src = st.session_state.fx_sources.get(code)
            if src:
                rate_val = st.session_state.fx_rates.get(code)
                st.caption(
                    f"FX Source: **{src['source']}**  |  FX Rate: 1 {code} = {rate_val:.6f} USD  |  "
                    f"Last Updated: {src['timestamp'] or 'not yet set'}"
                )
            else:
                st.caption("\u26A0\uFE0F No rate set yet for this currency \u2014 required before analysis if present in your data.")

    resolved = [c for c in SUPPORTED_CURRENCIES if c in st.session_state.fx_rates]
    if len(resolved) == len(SUPPORTED_CURRENCIES):
        st.success("\u2705 All supported currencies have a resolved USD rate.")
    else:
        missing = [c for c in SUPPORTED_CURRENCIES if c not in resolved]
        st.warning(f"\u26A0\uFE0F No rate set yet for: {', '.join(missing)}. Required only if your uploaded data contains these currencies.")

    # -------------------- High-Risk Jurisdictions --------------------
    st.divider()
    st.header("\U0001F30D High-Risk Jurisdictions")
    st.caption("Select jurisdictions according to institutional risk appetite. FATF status is shown for reference only and never auto-assigns a classification.")

    search = st.text_input("\U0001F50D Search country/jurisdiction", key="country_search")
    filtered = [c for c in ALL_COUNTRIES if search.lower() in c.lower()] if search else ALL_COUNTRIES
    add_country = st.selectbox("Select a country to classify", options=[""] + filtered, key="country_to_add")
    classification_choice = st.selectbox(
        "Institution classification", ["Low", "Medium", "High", "Prohibited/Restricted"], key="classification_choice"
    )
    if st.button("+ Add / update classification") and add_country:
        st.session_state.country_classifications[add_country.lower()] = classification_choice
        st.rerun()

    if st.session_state.country_classifications:
        st.markdown("**Selected jurisdictions:**")
        for country_lower, classification in list(st.session_state.country_classifications.items()):
            display_name = country_lower.title()
            fatf_status = fatf_reference.get_fatf_status(display_name)
            col_a, col_b = st.columns([5, 1])
            with col_a:
                st.markdown(f"`{display_name}` \u2014 **{classification}** *(FATF: {fatf_status})*")
            with col_b:
                if st.button("\u2715", key=f"remove_{country_lower}"):
                    del st.session_state.country_classifications[country_lower]
                    st.rerun()
    else:
        st.caption("No jurisdictions classified yet. This agent does not automatically classify any country as high risk.")

    st.session_state.rules_config["country_classifications"] = st.session_state.country_classifications

    with st.expander("FATF June 2026 reference data"):
        st.caption(
            "Regulatory reference information only \u2014 FATF status never automatically determines "
            "this institution's classification (see table above)."
        )
        st.markdown(f"**Call for Action (\"black list\"):** {', '.join(fatf_reference.FATF_CALL_FOR_ACTION)}")
        st.markdown(f"**Increased Monitoring (\"grey list\", {len(fatf_reference.FATF_INCREASED_MONITORING)} jurisdictions):**")
        st.caption(", ".join(fatf_reference.FATF_INCREASED_MONITORING))
        st.caption(f"Bundled snapshot verified {fatf_reference.FATF_VERIFIED_AT} against FATF's {fatf_reference.FATF_PUBLICATION_DATE} publication.")

        if st.button("\U0001F504 Refresh FATF Data"):
            with st.spinner("Checking FATF's site..."):
                result = fatf_reference.refresh_fatf_data()
            st.session_state.fatf_check_result = result

        if st.session_state.fatf_check_result:
            r = st.session_state.fatf_check_result
            if r["status"] == "current":
                st.success(f"\u2705 Current \u2014 {r['message']}")
            elif r["status"] == "possibly_outdated":
                st.warning(f"\u26A0\uFE0F Update unavailable \u2014 using last successfully cached dataset. {r['message']}")
            else:
                st.warning(f"\u26A0\uFE0F Update unavailable \u2014 using last successfully cached dataset. {r['message']}")
            st.caption(f"Last check attempt: {r.get('checked_at', 'n/a')}")

st.title("\U0001F6E1\uFE0F AML/KYC Compliance Flagging Agent")
st.caption(
    "A LangGraph-orchestrated agent that normalizes multi-currency transactions to a USD "
    "baseline, scores them across four risk categories, and pauses at a human oversight "
    "checkpoint before any compliance report is produced."
)

with st.expander("\u2139\uFE0F Agent architecture (click to expand)"):
    st.markdown("""
    This agent is orchestrated as an explicit **LangGraph state graph**, not a
    linear script:

    ```
    START -> normalize -> analyse -> human_review (interrupt) -> output -> END
    ```

    - **normalize**: converts every transaction to its USD equivalent (the
      fixed AML baseline currency) using the FX rates configured in the
      sidebar. Original amount/currency are always preserved.
    - **analyse**: runs the AML rules engine (with your sidebar-configured
      thresholds and country classifications) and produces a risk score per
      transaction, broken into Customer / Transaction / Geographic / Behavioural risk.
    - **human_review**: calls LangGraph's `interrupt()`. Execution genuinely
      **pauses** here - the graph will not proceed to `output` until a
      compliance officer supplies a decision for every flagged transaction.
    - **output**: resumes once decisions are supplied, merges them into the
      final report and timestamped audit trail.

    No transaction is ever auto-reported - the graph structurally cannot
    reach `output` for a flagged transaction without a human decision, and
    the AI never autonomously determines a transaction is illegal.
    """)

with st.expander("\U0001F4DC AML/CFT Regulatory Guidelines (Zimbabwe)"):
    st.caption(
        "The rules in this agent are grounded in Zimbabwe's AML/CFT regulatory "
        "framework, summarised below - the REGULATORY BASELINE. Institutional "
        "thresholds configured in the sidebar are separate, institution-specific "
        "risk parameters, not a restatement of this baseline."
    )
    for item in regulatory_watch.STATIC_FRAMEWORK:
        with st.container(border=True):
            title_line = f"**{item['title']}**  \u2014  *{item['role']}*"
            st.markdown(title_line)
            st.caption(item["note"])
            if item.get("url"):
                st.markdown(f"[Source document]({item['url']})")

    st.divider()
    st.markdown("**FIU Zimbabwe sector-specific guidelines**")
    st.caption(
        f"Catalogued manually as of {regulatory_watch.FIU_SECTOR_GUIDELINES_DATE} "
        "(FIU's site has shown intermittent availability, so this list is not "
        "live-checked - confirm directly at fiu.co.zw/index.php/guidelines/ before filing)."
    )
    for title in regulatory_watch.FIU_SECTOR_GUIDELINES:
        st.markdown(f"- {title}")

    st.divider()
    st.markdown("**Live check: Reserve Bank of Zimbabwe guideline list**")
    st.caption(
        f"Bundled snapshot last taken {regulatory_watch.SNAPSHOT_DATE}. "
        "This checks whether the RBZ has published new/renamed guidelines since then - "
        "it does not read or summarise their content, and it only covers the RBZ's own "
        "guideline page (not FIU's, above)."
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
st.caption("Optional `currency` column accepted (USD/ZAR/ZiG) - if absent, all transactions are treated as USD.")

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

        # Determine which currencies are present and whether all have a resolved rate
        present_currencies = sorted(raw_df["currency"].str.upper().str.replace("ZIG", "ZWG").unique().tolist()) if "currency" in raw_df.columns else ["USD"]
        missing_rates = [c for c in present_currencies if c not in st.session_state.fx_rates]

        if missing_rates:
            st.error(
                f"\u26A0\uFE0F Missing FX rate for: {', '.join(missing_rates)}. "
                f"Set a live or manual rate in the sidebar's Currency & FX Normalization panel before running analysis."
            )

        if st.button("\u25B6 Run compliance analysis", type="primary", disabled=bool(missing_rates)):
            with st.spinner("Agent normalizing currencies and analysing transactions..."):
                st.session_state.thread_id = str(uuid.uuid4())  # fresh run each time
                st.session_state.audit_trail = []
                result = run_pipeline(
                    graph, raw_df.to_dict("records"), st.session_state.thread_id,
                    rules_config=st.session_state.rules_config,
                    fx_rates=st.session_state.fx_rates, fx_sources=st.session_state.fx_sources,
                )
            if result["status"] == "fx_error":
                st.error(f"\u26A0\uFE0F Missing FX rate for: {', '.join(result['missing_currencies'])}. No transaction was evaluated - fix rates in the sidebar and retry.")
            elif result["status"] == "awaiting_review":
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
    """Render the Customer/Transaction/Geographic/Behavioural score bars + rule list + full audit fields."""
    cat_scores = txn.get("category_scores", {})
    st.caption("Overall Risk Score = Customer Risk + Transaction Risk + Geographic Risk + Behavioural Risk")
    cols = st.columns(4)
    for i, cat in enumerate(CATEGORIES):
        with cols[i]:
            st.metric(cat, cat_scores.get(cat, 0))

    with st.container(border=True):
        st.markdown("**Currency & Jurisdiction Audit Trail**")
        oc = txn.get("original_currency", "USD")
        oa = txn.get("original_amount", txn.get("amount"))
        st.markdown(
            f"Original Amount: **{oc} {oa:,.2f}**  |  USD Equivalent: **${txn.get('usd_equivalent', txn.get('amount')):,.2f}**  \n"
            f"FX Rate: 1 {oc} = {txn.get('usd_exchange_rate', 1.0):.6f} USD "
            f"({txn.get('fx_rate_source', 'Fixed')}, {txn.get('fx_rate_timestamp', '') or 'n/a'})  \n"
            f"AML Baseline: **USD**  |  Conversion Status: {txn.get('fx_conversion_status', 'n/a')}"
        )
        country = txn.get("counterparty_country", "")
        classification = st.session_state.country_classifications.get(country.lower(), "Not classified")
        fatf_status = fatf_reference.get_fatf_status(country)
        st.markdown(
            f"Jurisdiction: **{country}**  |  FATF Status: **{fatf_status}**  |  "
            f"Institutional Classification: **{classification}**"
        )

    st.markdown("**AML Rules Triggered**")
    triggered = txn.get("triggered_rules", [])
    if not triggered:
        st.markdown(f"{SEVERITY_ICON['Low']} Customer normally has low-risk activity \u2014 no rules triggered")
    else:
        for r in triggered:
            icon = SEVERITY_ICON.get(r["severity"], "")
            st.markdown(f"{icon} **{r['label']}** ({r['category']}) \u2014 {r['reason']}")


def render_dashboard(all_txns):
    """Executive KPI cards + charts summarising the whole flagged population (all in USD)."""
    flagged = [t for t in all_txns if t["risk_bucket"] in ("High", "Medium")]
    flagged_amount_usd = sum(t["amount"] for t in flagged)
    avg_score = (sum(t["risk_score"] for t in flagged) / len(flagged)) if flagged else 0
    pct_review = (len(flagged) / len(all_txns) * 100) if all_txns else 0

    k1, k2, k3 = st.columns(3)
    k1.metric("Total value flagged (USD)", f"${flagged_amount_usd:,.0f}")
    k2.metric("Avg. score (flagged)", f"{avg_score:.0f}")
    k3.metric("% requiring review", f"{pct_review:.0f}%")

    c1, c2 = st.columns(2)
    with c1:
        st.caption("Risk distribution")
        bucket_counts = pd.Series([t["risk_bucket"] for t in all_txns]).value_counts()
        st.bar_chart(bucket_counts)
    with c2:
        st.caption("Geographic risk contribution (by country, USD)")
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
    rule_labels = [r["label"] for t in flagged for r in t.get("triggered_rules", []) if r["weight"] > 0]
    if rule_labels:
        rule_counts = pd.Series(rule_labels).value_counts()
        st.bar_chart(rule_counts)
    else:
        st.caption("No rules triggered.")

    if any(t.get("original_currency", "USD") != "USD" for t in all_txns):
        st.caption("Currency mix in this batch:")
        cur_counts = pd.Series([t.get("original_currency", "USD") for t in all_txns]).value_counts()
        st.bar_chart(cur_counts)


if st.session_state.pipeline_status in ("awaiting_review", "complete"):
    all_txns = (
        st.session_state.pending_transactions
        if st.session_state.pipeline_status == "awaiting_review"
        else st.session_state.final_report
    )
    total = len(st.session_state.raw_df) if st.session_state.raw_df is not None else len(all_txns)
    high = sum(1 for t in all_txns if t["risk_bucket"] == "High")
    med = sum(1 for t in all_txns if t["risk_bucket"] == "Medium")

    st.header("Step 2-3: Agent risk analysis")
    st.caption("All figures below are USD equivalents - the fixed AML baseline currency.")

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
            "produce Step 5 output until you record a decision for every transaction below. "
            "This agent flags transactions and provides evidence for review - it does not "
            "determine that any transaction is illegal."
        )

        decisions = {}
        for txn in st.session_state.pending_transactions:
            badge = "\U0001F534" if txn["risk_bucket"] == "High" else "\U0001F7E0"
            oc = txn.get("original_currency", "USD")
            oa = txn.get("original_amount", txn.get("amount"))
            with st.container(border=True):
                st.markdown(
                    f"{badge} **{txn['transaction_id']}** \u2014 Customer `{txn['customer_id']}` \u2014 "
                    f"{oc} {oa:,.2f} (USD equivalent: ${txn['amount']:,.2f}) "
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

        report_df = pd.DataFrame(st.session_state.final_report)
        if "case_reference" not in report_df.columns:
            report_df["case_reference"] = ""

        display_cols = [
            "transaction_id", "customer_id", "original_currency", "original_amount",
            "amount", "usd_exchange_rate", "fx_rate_source", "date",
            "risk_bucket", "risk_score", "case_reference", "flag_reasons",
            "review_status", "reviewed_by", "reviewer_notes",
        ]
        st.dataframe(
            report_df[display_cols].rename(columns={"amount": "usd_equivalent"}),
            use_container_width=True
        )
        st.caption(
            "original_amount/original_currency are the values exactly as uploaded (never modified). "
            "usd_equivalent is what every AML rule and threshold was evaluated against."
        )

        with st.expander("View full risk breakdown for a transaction"):
            txn_id = st.selectbox("Select transaction", report_df["transaction_id"].tolist())
            selected = next(t for t in st.session_state.final_report if t["transaction_id"] == txn_id)
            render_risk_breakdown(selected)

        with st.expander("\U0001F50D Customer transaction history"):
            customer_ids = sorted(report_df["customer_id"].unique().tolist())
            selected_customer = st.selectbox("Select customer", customer_ids)
            cust_df = report_df[report_df["customer_id"] == selected_customer].sort_values("date")
            st.dataframe(
                cust_df[["transaction_id", "date", "original_currency", "original_amount", "amount", "risk_bucket", "risk_score", "review_status"]].rename(columns={"amount": "usd_equivalent"}),
                use_container_width=True
            )
            if len(cust_df) > 1:
                chart_df = cust_df[["date", "amount", "risk_score"]].set_index("date")
                c1, c2 = st.columns(2)
                with c1:
                    st.caption("USD equivalent over time")
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
            csv_out = report_df[display_cols + ["category_scores"]].rename(columns={"amount": "usd_equivalent"}).to_csv(index=False).encode("utf-8")
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
                        report_df[display_cols].rename(columns={"amount": "usd_equivalent"}).to_excel(
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
                    build_pdf(st.session_state.final_report, st.session_state.rules_config, tmp_path)
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
