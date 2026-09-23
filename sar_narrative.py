"""
sar_narrative.py
------------------
Generates a draft Suspicious Activity/Transaction Report (SAR/STR)
narrative for a flagged transaction.

No external API, no API key, no network call - the narrative is composed
entirely from the transaction's own already-computed fields (amount,
currency, jurisdiction, triggered AML rules, risk score) using
professional compliance-report phrasing templates. This keeps the whole
application free to run with zero external dependencies or secrets to
manage, while still producing a properly structured first-draft narrative
that saves the officer from writing one from scratch.

IMPORTANT - this is a DRAFT ONLY, meant to be edited and approved by the
compliance officer before use anywhere. The wording deliberately never
states that the customer or transaction is confirmed to involve a crime -
it describes the facts and the pattern objectively and closes with a
neutral statement that the matter is submitted for compliance review.
This keeps the same "agent flags, human decides" principle as the rest
of the application.
"""

SEVERITY_PHRASE = {
    "High": "a high-severity indicator",
    "Medium": "a moderate-severity indicator",
    "Low": "a lower-severity indicator",
}

CATEGORY_PHRASE = {
    "Geographic": "the transaction's counterparty jurisdiction",
    "Transaction": "the characteristics of the transaction itself",
    "Customer": "the customer's existing KYC risk classification",
    "Behavioural": "the customer's transaction history and behaviour",
}


def _rule_sentence(rule: dict) -> str:
    label = rule.get("label", "an AML rule")
    category = rule.get("category", "")
    severity = rule.get("severity", "Medium")
    reason = rule.get("reason", "")
    category_phrase = CATEGORY_PHRASE.get(category, "an AML risk factor")
    severity_phrase = SEVERITY_PHRASE.get(severity, "an indicator")
    return f"The system flagged \u201c{label}\u201d, {severity_phrase} relating to {category_phrase}: {reason.rstrip('.')}."


def generate_sar_narrative(transaction: dict):
    """
    Returns (narrative: str, None) - always succeeds, since this is a pure
    local text composition with no external dependency. The second element
    is kept as None for interface symmetry with any future generator that
    might need to report an error.
    """
    tid = transaction.get("transaction_id", "N/A")
    cust = transaction.get("customer_id", "N/A")
    date = transaction.get("date", "N/A")
    oc = transaction.get("original_currency", "USD")
    oa = transaction.get("original_amount", transaction.get("amount", 0))
    usd = transaction.get("usd_equivalent", transaction.get("amount", 0))
    country = transaction.get("counterparty_country", "an unspecified jurisdiction")
    txn_type = transaction.get("transaction_type", "transaction")
    kyc_profile = transaction.get("customer_risk_profile", "Low")
    score = transaction.get("risk_score", "N/A")
    bucket = transaction.get("risk_bucket", "N/A")
    triggered = transaction.get("triggered_rules", [])

    currency_clause = (
        f"{oc} {oa:,.2f} (USD equivalent: ${usd:,.2f})" if oc != "USD"
        else f"${oa:,.2f}"
    )

    para1 = (
        f"On {date}, transaction {tid} involving customer {cust} was processed as a "
        f"{txn_type} for {currency_clause}, with a counterparty jurisdiction of {country}. "
        f"The customer's existing KYC risk classification at the time was {kyc_profile}. "
        f"This transaction was evaluated by the institution's automated AML compliance "
        f"screening system and received an overall risk score of {score} ({bucket})."
    )

    if triggered:
        rule_sentences = " ".join(_rule_sentence(r) for r in triggered)
        para2 = f"The following indicators contributed to this classification: {rule_sentences}"
    else:
        para2 = "No specific automated indicators were recorded for this transaction beyond its overall risk score."

    para3 = (
        "This narrative is a system-generated first draft based on the transaction's "
        "recorded facts and the AML rules triggered; it does not constitute a finding "
        "that the customer or transaction is confirmed to involve money laundering or "
        "any other offence. It is submitted for review, editing, and approval by the "
        "assigned compliance officer prior to any internal escalation or external filing."
    )

    return f"{para1}\n\n{para2}\n\n{para3}", None
