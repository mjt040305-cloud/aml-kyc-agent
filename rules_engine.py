"""
rules_engine.py
----------------
Core analysis logic for the AML/KYC Compliance Flagging Agent.

Implements the "read -> analyse -> decide/flag" stages of the agent
pipeline. Deliberately rule-based (transparent, explainable, auditable)
rather than a black-box model - a compliance use case requires that a
regulator or auditor can see WHY a transaction was flagged.

Each rule belongs to one of four risk categories, mirroring how a real
banking AML system decomposes risk:

    Customer Risk     - the customer's existing KYC risk classification
    Transaction Risk  - characteristics of this specific transaction
    Geographic Risk   - counterparty jurisdiction risk
    Behavioural Risk  - deviation from the customer's own normal pattern

Overall Risk Score = Customer Risk + Transaction Risk + Geographic Risk
                      + Behavioural Risk

Each rule also carries a severity indicator (High / Medium / Low) used
to render a traffic-light-style "AML Rules Triggered" panel in the UI.

All thresholds are configurable via a `config` dict (see DEFAULT_CONFIG),
so a compliance officer can tune the agent to their institution's risk
appetite from the Streamlit sidebar without editing code.
"""

import pandas as pd
import numpy as np

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
    "high_risk_countries": [
        "north korea", "iran", "myanmar", "afghanistan", "syria", "somalia"
    ],
    "rapid_movement_hours": 24,
    "rapid_movement_min_txns": 3,
    "round_number_multiple": 1000,
    "round_number_min_amount": 5000,
    "velocity_std_multiplier": 3,        # flag if txn > mean + N*std for that customer
    "high_risk_profile_min_amount": 3000,
}


def _is_round_number(amount: float, multiple: int) -> bool:
    return amount % multiple == 0 and amount > 0


def _check_structuring(row, customer_txns, cfg) -> dict:
    """Flag amounts just under the reporting threshold (classic 'smurfing')."""
    threshold = cfg["structuring_threshold"]
    lower_bound = threshold * (1 - cfg["structuring_margin"])
    triggered = lower_bound <= row["amount"] < threshold
    return {
        "triggered": triggered,
        "reason": f"Amount ${row['amount']:,.2f} sits just below the ${threshold:,.0f} reporting threshold (possible structuring)",
        "weight": 30, "category": "Transaction", "severity": "High",
        "label": "Large / structured transaction",
    }


def _check_high_risk_country(row, customer_txns, cfg) -> dict:
    country = str(row.get("counterparty_country", "")).strip().lower()
    triggered = country in cfg["high_risk_countries"]
    return {
        "triggered": triggered,
        "reason": f"Counterparty jurisdiction '{row['counterparty_country']}' is on the high-risk country list",
        "weight": 40, "category": "Geographic", "severity": "High",
        "label": "High-risk jurisdiction",
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
        "reason": f"Large round-number amount (${row['amount']:,.2f}) is atypical of normal commercial activity",
        "weight": 10, "category": "Transaction", "severity": "Low",
        "label": "Round-number amount",
    }


def _check_velocity_deviation(row, customer_txns, cfg) -> dict:
    """Flag if amount is a statistical outlier vs this customer's own history."""
    hist = customer_txns[customer_txns["date"] < row["date"]]["amount"]
    triggered = False
    reason = "Amount is consistent with this customer's historical pattern"
    if len(hist) >= 4:
        mean, std = hist.mean(), hist.std()
        if std > 0 and row["amount"] > mean + cfg["velocity_std_multiplier"] * std:
            triggered = True
            reason = f"Amount is {row['amount']/mean:.1f}x this customer's historical average (${mean:,.2f})"
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
        "reason": "Customer is already flagged High risk in KYC profile and made a significant transaction",
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
    cfg["high_risk_countries"] = {c.strip().lower() for c in cfg["high_risk_countries"]}

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
