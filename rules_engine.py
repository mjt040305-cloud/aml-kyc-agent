"""
rules_engine.py
----------------
Core analysis logic for the AML/KYC Compliance Flagging Agent.

This module implements the "read -> analyse -> decide/flag" stages of the
agent pipeline. It is deliberately rule-based (transparent, explainable,
auditable) rather than a black-box model, which matters for a compliance
use case where a regulator or auditor must be able to see WHY a
transaction was flagged.

Each rule returns a (triggered: bool, reason: str, weight: int) tuple.
Weights are summed into a risk score, which is then bucketed into
Low / Medium / High.
"""

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Configurable thresholds (in a real deployment these would come from a
# regulator-defined policy document, e.g. RBZ AML/CFT guidelines).
# ---------------------------------------------------------------------------
STRUCTURING_THRESHOLD = 10000        # currency units - reporting threshold
STRUCTURING_MARGIN = 0.10            # "just under" = within 10% of threshold
HIGH_RISK_COUNTRIES = {
    "north korea", "iran", "myanmar", "afghanistan", "syria", "somalia"
}
RAPID_MOVEMENT_HOURS = 24
RAPID_MOVEMENT_MIN_TXNS = 3
ROUND_NUMBER_MULTIPLE = 1000
VELOCITY_STD_MULTIPLIER = 3           # flag if txn > mean + 3*std for that customer


def _is_round_number(amount: float) -> bool:
    return amount % ROUND_NUMBER_MULTIPLE == 0 and amount > 0


def _check_structuring(row, customer_txns) -> tuple:
    """Flag amounts just under the reporting threshold (classic 'smurfing')."""
    lower_bound = STRUCTURING_THRESHOLD * (1 - STRUCTURING_MARGIN)
    if lower_bound <= row["amount"] < STRUCTURING_THRESHOLD:
        return True, f"Amount ${row['amount']:,.2f} sits just below the ${STRUCTURING_THRESHOLD:,.0f} reporting threshold (possible structuring)", 30
    return False, "", 0


def _check_high_risk_country(row, customer_txns) -> tuple:
    country = str(row.get("counterparty_country", "")).strip().lower()
    if country in HIGH_RISK_COUNTRIES:
        return True, f"Counterparty jurisdiction '{row['counterparty_country']}' is on the high-risk country list", 40
    return False, "", 0


def _check_rapid_movement(row, customer_txns) -> tuple:
    """Flag if the same customer has many transactions in a short window."""
    window = customer_txns[
        (customer_txns["date"] >= row["date"] - pd.Timedelta(hours=RAPID_MOVEMENT_HOURS))
        & (customer_txns["date"] <= row["date"])
    ]
    if len(window) >= RAPID_MOVEMENT_MIN_TXNS:
        return True, f"{len(window)} transactions by this customer within {RAPID_MOVEMENT_HOURS}h (rapid movement of funds)", 25
    return False, "", 0


def _check_round_amount(row, customer_txns) -> tuple:
    if _is_round_number(row["amount"]) and row["amount"] >= 5000:
        return True, f"Large round-number amount (${row['amount']:,.2f}) is atypical of normal commercial activity", 10
    return False, "", 0


def _check_velocity_deviation(row, customer_txns) -> tuple:
    """Flag if amount is a statistical outlier vs this customer's own history."""
    hist = customer_txns[customer_txns["date"] < row["date"]]["amount"]
    if len(hist) >= 4:
        mean, std = hist.mean(), hist.std()
        if std > 0 and row["amount"] > mean + VELOCITY_STD_MULTIPLIER * std:
            return True, f"Amount is {row['amount']/mean:.1f}x this customer's historical average (${mean:,.2f})", 20
    return False, "", 0


def _check_customer_risk_profile(row, customer_txns) -> tuple:
    profile = str(row.get("customer_risk_profile", "")).strip().lower()
    if profile == "high" and row["amount"] >= 3000:
        return True, "Customer is already flagged High risk in KYC profile and made a significant transaction", 15
    return False, "", 0


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


def analyse_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Run all rules against every transaction.

    Expects columns: transaction_id, customer_id, date, amount,
    counterparty_country, transaction_type, customer_risk_profile
    Returns the same dataframe with added columns:
    risk_score, risk_bucket, flag_reasons, review_status
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    scores, buckets, reasons_list = [], [], []

    for idx, row in df.iterrows():
        customer_txns = df[df["customer_id"] == row["customer_id"]]
        total_score = 0
        reasons = []
        for rule in RULES:
            triggered, reason, weight = rule(row, customer_txns)
            if triggered:
                total_score += weight
                reasons.append(reason)

        scores.append(total_score)
        buckets.append(score_to_bucket(total_score))
        reasons_list.append("; ".join(reasons) if reasons else "No rules triggered")

    df["risk_score"] = scores
    df["risk_bucket"] = buckets
    df["flag_reasons"] = reasons_list
    df["review_status"] = "Pending"  # human oversight checkpoint field
    df["reviewer_notes"] = ""

    return df.sort_values("risk_score", ascending=False).reset_index(drop=True)
