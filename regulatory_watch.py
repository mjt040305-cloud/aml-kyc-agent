"""
regulatory_watch.py
--------------------
Zimbabwe AML/CFT regulatory context, plus an optional, on-demand live check
against the Reserve Bank of Zimbabwe's public Bank Supervision Guidelines
page.

This feature is OPTIONAL and user-triggered - it never runs automatically,
and it never blocks or breaks the core agent pipeline if the fetch fails
(network restrictions, site downtime, or a changed page structure are all
handled gracefully with a clear message, not a crash).

Zimbabwe's AML/CFT framework (verified via RBZ's own site, Aug 2026):
  - Legal basis: Money Laundering and Proceeds of Crime Act [Chapter 9:24]
    ("MLPC Act")
  - Regulator: Reserve Bank of Zimbabwe (RBZ), Bank Supervision Division
  - Financial Intelligence Unit (FIU Zimbabwe): receives Suspicious
    Transaction Reports (STRs) and issues binding sector guidelines
  - Current governing guideline: AML/CFT/CPF Guideline No: 01-2025/BSSFS
    (June 2025), which requires a risk-based approach, at least annual
    enterprise-wide risk assessment, and ongoing customer/transaction
    monitoring - directly mirrored by this agent's rules engine
  - FATF Recommendations and FATF's high-risk/grey-list jurisdictions
    inform the risk-based approach the RBZ guideline requires
  - Statutory Instrument 99 of 2026 extended AML/CFT oversight to virtual
    asset service providers (VASPs), requiring registration with the RBZ

This module does NOT auto-interpret or summarise legal text - it only
detects whether the RBZ's published guideline list has changed since the
bundled snapshot, so a human compliance officer knows when to go and read
the source document themselves. It is informational, not a substitute for
legal advice or the officer's own regulatory monitoring obligations.
"""

import re
from datetime import datetime

RBZ_GUIDELINES_URL = "https://www.rbz.co.zw/index.php/regulation-supervision/regulation-supervision/guidelines-circulars-and-public-notices"

# Snapshot of guideline titles found on the RBZ Bank Supervision Guidelines
# page as of the date below. Used only to detect NEW entries on a live
# check - not reproduced as legal text, just titles for change-detection.
SNAPSHOT_DATE = "2026-08-31"
KNOWN_GUIDELINES_SNAPSHOT = {
    "Cybersecurity and Resilience Guideline",
    "Prudential Standard No: 02-2025/BSSFS: Corporate Governance",
    "AML/CFT/CPF Guideline No: 01-2025/BSSFS",
    "Prudential Standard No: 01-2024/BSD: Risk Management",
    "Prudential Standard No: 02-2023/BSD: Model Risk Management",
    "Climate Risk Management Guideline: Guideline No.1/2023 BSD",
    "Prudential Standard No: 02-2022/BSD: Guidance on the Implementation of the Liquidity Coverage Ratio",
    "Microfinance Institutions Lending in Foreign Currency Guideline No.01-2022/BSD",
    "Microfinance Institutions Lending in Foreign Currency Guideline No.01-2022/BSDNo.01-2022/BSD",
    "Prudential Standard No. 01-2020/BSD - Framework for dealing with Domestic Systemically Important Banking Institutions",
    "Addendum 1: Prudential Standards No. 02-2016/BSD: Deposit Taking Microfinance Institutions - November 2019",
    "Prudential Standards No. 02/ 2016/BSD: Deposit - Taking Microfinance Institutions - November 2016",
    "Prudential Standards No. 01/ 2016/BSD: Agency Banking - September 2016",
    "External Audit Framework for Banking and Non-Bank Financial Institutions.pdf 02-2015/BSD",
    "Framework on the Relationship Between Bank Supervisors and Bank's External Auditors - October 2004",
    "Accreditation of Credit Rating Agencies Guideline No.04 - 2004",
    "Corporate Governance 2004",
    "Addendum Corporate Governance Guideline",
    "Board & Director Evaluation Framework - Revised",
    "Consolidated Supervision Guideline",
    "Minimum Disclosure Requirements",
    "Minimum Internal Audit Guidelines",
    "National Microfinance Policy",
    "Risk Management Guideline 2006",
    "Securitisation & Structured Finance Guideline",
    "Technical Guidance on Basel II",
    "Troubled and Insolvent Bank Policy Revised. 06.06.2011",
}

STATIC_FRAMEWORK = [
    {
        "title": "Money Laundering and Proceeds of Crime Act [Chapter 9:24]",
        "role": "Primary legislation",
        "note": "Legal basis for AML/CFT obligations in Zimbabwe; RBZ and FIU guidelines are issued under this Act.",
        "url": None,
    },
    {
        "title": "AML/CFT/CPF Guideline No: 01-2025/BSSFS (June 2025)",
        "role": "RBZ Bank Supervision Guideline",
        "note": "Current governing guideline for banks/deposit-takers. Requires a risk-based approach, at-least-annual enterprise-wide risk assessment, and ongoing customer/transaction monitoring - the same principles this agent's rules engine implements.",
        "url": "https://www.rbz.co.zw/documents/bank_sup/Guidelines_/AML_CFT_CPF_GUIDELINE_-_June_2025.pdf",
    },
    {
        "title": "AML-RBA Oversight Guideline for Payment Service Providers (Jan 2021)",
        "role": "RBZ National Payment Systems Guideline",
        "note": "Extends AML/CFT risk-based supervision to mobile money and payment service providers.",
        "url": "https://www.rbz.co.zw/documents/nps/AML-RBA-OVERSIGHT-GUIDELINE-2021.pdf",
    },
    {
        "title": "Financial Intelligence Unit (FIU) Zimbabwe Guidelines",
        "role": "Sector guidelines",
        "note": "FIU issues binding minimum-standard AML/CFT guidelines under the MLPC Act and is the authority that receives Suspicious Transaction Reports (STRs).",
        "url": "https://www.fiu.co.zw/index.php/guidelines/",
    },
    {
        "title": "Statutory Instrument 99 of 2026 (Virtual Asset Service Providers)",
        "role": "Statutory Instrument",
        "note": "Brought virtual asset service providers (VASPs) under RBZ AML/CFT oversight, requiring registration before offering digital asset services in Zimbabwe.",
        "url": None,
    },
    {
        "title": "FATF Recommendations & high-risk/grey-list jurisdictions",
        "role": "International standard",
        "note": "The RBZ guideline explicitly incorporates FATF's risk-based approach and high-risk country identification - the basis for this agent's Geographic Risk category.",
        "url": None,
    },
]


def check_for_updates(timeout=8):
    """
    Attempt a live fetch of the RBZ Bank Supervision Guidelines page and
    compare the guideline titles found there against the bundled snapshot.

    Never raises - always returns a dict, so it is safe to call directly
    from a Streamlit button handler:

      {"status": "ok", "checked_at": ..., "total_found": N,
       "new_or_changed": [...], "snapshot_date": ...}
      {"status": "error", "message": "..."}
    """
    try:
        import requests
    except ImportError:
        return {"status": "error", "message": "The 'requests' package is not installed in this environment."}

    try:
        resp = requests.get(
            RBZ_GUIDELINES_URL, timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return {"status": "error", "message": f"Could not reach the RBZ guidelines page right now: {e}"}

    try:
        matches = re.findall(
            r'<a[^>]+href="(https://www\.rbz\.co\.zw/documents/[^"]+?\.pdf)"[^>]*>(.*?)</a>',
            html, re.IGNORECASE | re.DOTALL,
        )
        found_titles = set()
        for _, raw_text in matches:
            clean = re.sub(r"<[^>]+>", "", raw_text)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                found_titles.add(clean)
    except Exception as e:
        return {"status": "error", "message": f"Fetched the page but could not parse it: {e}"}

    if not found_titles:
        return {"status": "error", "message": "Page fetched successfully but no guideline links were found - the RBZ site's structure may have changed since this agent was built."}

    new_items = sorted(found_titles - KNOWN_GUIDELINES_SNAPSHOT)

    return {
        "status": "ok",
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_found": len(found_titles),
        "new_or_changed": new_items,
        "snapshot_date": SNAPSHOT_DATE,
    }
