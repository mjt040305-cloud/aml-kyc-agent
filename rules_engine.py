"""
rules_engine.py
----------------
Core analysis logic for the AML/KYC Compliance Flagging Agent.

Implements the "read -> analyse -> decide/flag" stages of the agent
pipeline. Deliberately rule-based (transparent, explainable, auditable)
rather than a black-box model - a compliance use case requires that a
regulator or auditor can see WHY a transaction was flagged.

IMPORTANT - USD baseline: this module assumes the 'amount' column it
receives has ALREADY been normalized to its USD equivalent by
fx_normalize.py, upstream in the pipeline (see agent_graph.py's analyse
node). Every AML threshold and rule in this file is therefore always
evaluated in USD, regardless of what currency a transaction was
originally recorded in. This module does not perform currency conversion
itself - see fx_normalize.py for that.

Each rule belongs to one of four risk categories, mirroring how a real
banking AML system decomposes risk:

    Customer Risk     - the customer's existing KYC risk classification
    Transaction Risk  - characteristics of this specific transaction (USD)
    Geographic Risk   - counterparty jurisdiction, per the INSTITUTION's
                         own configured risk classification (see
                         COUNTRY_CLASSIFICATION_WEIGHTS) - never
                         auto-derived from FATF status (see fatf_reference.py)
    Behavioural Risk  - deviation from the customer's own normal pattern (USD)

Overall Risk Score = Customer Risk + Transaction Risk + Geographic Risk
                      + Behavioural Risk

Each rule also carries a severity indicator (High / Medium / Low) used
to render a traffic-light-style "AML Rules Triggered" panel in the UI.

All thresholds are configurable via a `config` dict (see DEFAULT_CONFIG),
so a compliance officer can tune the agent to their institution's risk
appetite from the Streamlit sidebar without editing code. This agent
flags transactions and provides reasons/evidence for human compliance
review - it never autonomously determines that a transaction is illegal.
"""

import pandas as pd
import numpy as np
import fatf_reference

CATEGORIES = ["Customer", "Transaction", "Geographic", "Behavioural"]

SEVERITY_ICON = {"High": "\U0001F534", "Medium": "\U0001F7E0", "Low": "\U0001F7E2"}

# ---------------------------------------------------------------------------
# Default configurable thresholds (in a real deployment these would come
# from a regulator-defined policy document, e.g. RBZ AML/CFT guidelines).
# Exposed via the Streamlit sidebar so risk appetite can be tuned live.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "structuring_threshold": 10000,      # currency units - reporting threshold
    "structuring_margin": 0.10,          # "just under" = within 10% of threshold
    # Institution-configured per-country risk classifications, e.g.
    # {"iran": "High", "south africa": "Institution Selected"}. Deliberately
    # EMPTY by default - this agent does not auto-classify any jurisdiction
    # as high risk. See fatf_reference.py for FATF's own (separate,
    # reference-only) status data, and app.py's Country Risk Classification
    # panel for how the institution builds this dict.
    "country_classifications": {},
    "rapid_movement_hours": 24,
    "rapid_movement_min_txns": 3,
    "round_number_multiple": 1000,
    "round_number_min_amount": 5000,
    "velocity_std_multiplier": 3,        # flag if txn > mean + N*std for that customer
    "high_risk_profile_min_amount": 3000,
}

# Weight/severity assigned per institution classification level. "Low" and
# any unclassified country never trigger the geographic rule - consistent
# with "do not automatically classify every country as high risk".
COUNTRY_CLASSIFICATION_WEIGHTS = {
    "Prohibited/Restricted": {"weight": 50, "severity": "High"},
    "High": {"weight": 40, "severity": "High"},
    "Medium": {"weight": 20, "severity": "Medium"},
    "Low": {"weight": 0, "severity": "Low"},
}


def _is_round_number(amount: float, multiple: int) -> bool:
    return amount % multiple == 0 and amount > 0


def _check_structuring(row, customer_txns, cfg) -> dict:
    """Flag amounts just under the reporting threshold (classic 'smurfing').
    `row['amount']` is always the USD equivalent by the time this runs -
    normalization happens upstream in fx_normalize.py before analysis."""
    threshold = cfg["structuring_threshold"]
    lower_bound = threshold * (1 - cfg["structuring_margin"])
    triggered = lower_bound <= row["amount"] < threshold
    return {
        "triggered": triggered,
        "reason": f"USD equivalent ${row['amount']:,.2f} sits just below the USD {threshold:,.0f} institutional structuring alert threshold (possible structuring)",
        "weight": 30, "category": "Transaction", "severity": "High",
        "label": "Large / structured transaction",
    }


def _check_high_risk_country(row, customer_txns, cfg) -> dict:
    """
    Geographic risk driven entirely by the INSTITUTION's own configured
    classification for this country (Low/Medium/High/Prohibited-Restricted),
    never automatically from FATF status. FATF's status is looked up only
    to enrich the explanation shown to the compliance officer - it never
    decides the outcome by itself (see fatf_reference.py).
    """
    country = str(row.get("counterparty_country", "")).strip()
    classification = cfg["country_classifications"].get(country.lower())
    fatf_status = fatf_reference.get_fatf_status(country)

    if not classification or classification not in COUNTRY_CLASSIFICATION_WEIGHTS:
        return {
            "triggered": False,
            "reason": f"'{country}' has no institutional high-risk classification on file (FATF status: {fatf_status})",
            "weight": 0, "category": "Geographic", "severity": "Low",
            "label": "No institutional geographic classification",
        }

    weight_info = COUNTRY_CLASSIFICATION_WEIGHTS[classification]
    triggered = weight_info["weight"] > 0
    return {
        "triggered": triggered,
        "reason": (
            f"Counterparty jurisdiction '{country}' is classified {classification} risk by this "
            f"institution's risk appetite (FATF status: {fatf_status})"
        ),
        "weight": weight_info["weight"], "category": "Geographic", "severity": weight_info["severity"],
        "label": f"Institution-classified {classification} jurisdiction" if triggered else "Low-risk jurisdiction (institution classified)",
    }


def _check_rapid_movement(row, customer_txns, cfg) -> dict:
    """Flag if the same customer has many transactions in a short window."""
    hours = cfg["rapid_movement_hours"]
    window = customer_txns[
        (customer_txns["date"] >= row["date"] - pd.Timedelta(hours=hours))
        & (customer_txns["date"] <= row["date"])
    ]
    triggered = len(window) >= cfg["rapid_movement_min_txns"]
    return {
        "triggered": triggered,
        "reason": f"{len(window)} transactions by this customer within {hours}h (rapid movement of funds)",
        "weight": 25, "category": "Behavioural", "severity": "Medium",
        "label": "Rapid movement of funds",
    }


def _check_round_amount(row, customer_txns, cfg) -> dict:
    triggered = (
        _is_round_number(row["amount"], cfg["round_number_multiple"])
        and row["amount"] >= cfg["round_number_min_amount"]
    )
    return {
        "triggered": triggered,
        "reason": f"Large round-number USD equivalent (${row['amount']:,.2f}) is atypical of normal commercial activity",
        "weight": 10, "category": "Transaction", "severity": "Low",
        "label": "Round-number amount",
    }


def _check_velocity_deviation(row, customer_txns, cfg) -> dict:
    """Flag if amount is a statistical outlier vs this customer's own history.
    All amounts compared here are USD equivalents (see fx_normalize.py)."""
    hist = customer_txns[customer_txns["date"] < row["date"]]["amount"]
    triggered = False
    reason = "USD equivalent is consistent with this customer's historical pattern"
    if len(hist) >= 4:
        mean, std = hist.mean(), hist.std()
        if std > 0 and row["amount"] > mean + cfg["velocity_std_multiplier"] * std:
            triggered = True
            reason = f"USD equivalent is {row['amount']/mean:.1f}x this customer's historical average (${mean:,.2f})"
    return {
        "triggered": triggered, "reason": reason,
        "weight": 20, "category": "Behavioural", "severity": "Medium",
        "label": "Unusual vs customer history",
    }


def _check_customer_risk_profile(row, customer_txns, cfg) -> dict:
    profile = str(row.get("customer_risk_profile", "")).strip().lower()
    triggered = profile == "high" and row["amount"] >= cfg["high_risk_profile_min_amount"]
    return {
        "triggered": triggered,
        "reason": f"Customer is already flagged High risk in KYC profile and made a USD-equivalent transaction \u2265 ${cfg['high_risk_profile_min_amount']:,.0f}",
        "weight": 15, "category": "Customer", "severity": "Medium",
        "label": "Elevated KYC risk profile",
    }


RULES = [
    _check_structuring,
    _check_high_risk_country,
    _check_rapid_movement,
    _check_round_amount,
    _check_velocity_deviation,
    _check_customer_risk_profile,
]


def score_to_bucket(score: int) -> str:
    if score >= 40:
        return "High"
    elif score >= 15:
        return "Medium"
    elif score > 0:
        return "Low"
    return "None"


def analyse_transactions(df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
    """
    Run all rules against every transaction.

    Expects columns: transaction_id, customer_id, date, amount,
    counterparty_country, transaction_type, customer_risk_profile

    `config` overrides any subset of DEFAULT_CONFIG (e.g. from the
    Streamlit sidebar); missing keys fall back to the default.

    Returns the same dataframe with added columns:
      risk_score, risk_bucket, flag_reasons, review_status,
      category_scores (dict: Customer/Transaction/Geographic/Behavioural -> int),
      triggered_rules (list of dicts: label, reason, severity, category, weight)
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cfg["country_classifications"] = {
        k.strip().lower(): v for k, v in cfg["country_classifications"].items()
    }

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    scores, buckets, reasons_list = [], [], []
    category_scores_list, triggered_rules_list = [], []

    for idx, row in df.iterrows():
        customer_txns = df[df["customer_id"] == row["customer_id"]]
        total_score = 0
        reasons = []
        category_scores = {c: 0 for c in CATEGORIES}
        triggered_rules = []

        for rule in RULES:
            result = rule(row, customer_txns, cfg)
            if result["triggered"]:
                total_score += result["weight"]
                category_scores[result["category"]] += result["weight"]
                reasons.append(result["reason"])
                triggered_rules.append({
                    "label": result["label"],
                    "reason": result["reason"],
                    "severity": result["severity"],
                    "category": result["category"],
                    "weight": result["weight"],
                })

        scores.append(total_score)
        buckets.append(score_to_bucket(total_score))
        reasons_list.append("; ".join(reasons) if reasons else "No rules triggered")
        category_scores_list.append(category_scores)
        triggered_rules_list.append(triggered_rules)

    df["risk_score"] = scores
    df["risk_bucket"] = buckets
    df["flag_reasons"] = reasons_list
    df["category_scores"] = category_scores_list
    df["triggered_rules"] = triggered_rules_list
    df["review_status"] = "Pending"  # human oversight checkpoint field
    df["reviewer_notes"] = ""
    df["reviewed_by"] = ""

    return df.sort_values("risk_score", ascending=False).reset_index(drop=True)
