"""
sar_filing_report.py
---------------------
Generates the final SAR filing PDF for a fully completed (two-person
signed-off) case, formatted for submission to the Financial Intelligence
Unit (FIU) Zimbabwe - the statutory recipient of Suspicious Transaction
Reports under the Money Laundering and Proceeds of Crime Act.

This module only produces output for transactions that have BOTH the
first officer's decision AND a second officer's co-signature recorded
(see case_store.py's two-person sign-off workflow). A case still awaiting
co-signature has no completed filing package to produce - see
build_sar_filing_pdf()'s early return in that situation.
"""

from datetime import datetime
from fpdf import FPDF

import sar_narrative

NAVY = (31, 56, 100)


def _safe(text) -> str:
    """fpdf2's default Helvetica font is latin-1 only; strip anything else."""
    return str(text).encode("latin-1", "ignore").decode("latin-1")


class SARFilingPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*NAVY)
        self.set_x(self.l_margin)
        self.multi_cell(0, 10, "Suspicious Activity Report - Filing Package", align="C")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(90, 90, 90)
        self.set_x(self.l_margin)
        self.multi_cell(0, 6, "Prepared for submission to the Financial Intelligence Unit (FIU), Zimbabwe", align="C")
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.multi_cell(0, 10, f"Page {self.page_no()}", align="C")


def build_sar_filing_pdf(case: dict, output_path: str):
    """
    `case`: the dict returned by case_store.load_case() for a case whose
    status is 'completed'.

    Returns (output_path, error) - error is None on success. If the case
    has no fully co-signed SAR escalations, returns (None, message)
    instead of producing an empty or misleading filing document.
    """
    cosign = case.get("cosign") or {}
    escalated = {
        tid: e for tid, e in cosign.items()
        if e.get("decision") == "Escalate to SAR filing" and e.get("second_officer_id")
    }
    if not escalated:
        return None, "This case has no fully co-signed SAR escalations to file."

    final_report = case.get("final_report") or []
    txn_by_id = {t["transaction_id"]: t for t in final_report}

    pdf = SARFilingPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(0, 0, 0)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 8, _safe(f"Case Reference: {case['case_id']}"))
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 6, _safe(
        f"Prepared: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Transactions filed: {len(escalated)}"
    ))
    pdf.ln(4)

    for tid, entry in escalated.items():
        txn = txn_by_id.get(tid, {})

        pdf.set_font("Helvetica", "B", 12)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 8, _safe(f"Transaction {tid}"))

        pdf.set_font("Helvetica", "", 9)
        oc = txn.get("original_currency", "USD")
        oa = txn.get("original_amount", txn.get("amount", 0)) or 0
        usd = txn.get("usd_equivalent", txn.get("amount", 0)) or 0
        fields = [
            f"Customer ID: {txn.get('customer_id', 'N/A')}",
            f"Date: {txn.get('date', 'N/A')}",
            f"Original amount: {oc} {oa:,.2f}  |  USD equivalent: ${usd:,.2f}",
            f"FX rate source: {txn.get('fx_rate_source', 'N/A')}",
            f"Counterparty jurisdiction: {txn.get('counterparty_country', 'N/A')}",
            f"Overall risk score: {txn.get('risk_score', 'N/A')} ({txn.get('risk_bucket', 'N/A')})",
        ]
        for f in fields:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 5, _safe(f))

        triggered = txn.get("triggered_rules", [])
        if triggered:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 5, "AML rules triggered:")
            for r in triggered:
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(0, 5, _safe(f"  - {r.get('label', '')}: {r.get('reason', '')}"))

        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, "Narrative:")
        pdf.set_font("Helvetica", "", 9)
        narrative, _ = sar_narrative.generate_sar_narrative(txn)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, _safe(narrative))

        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, "Sign-off (two-person maker-checker):")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, _safe(
            f"First reviewer: {entry.get('first_officer_name', 'N/A')} "
            f"({entry.get('first_officer_role', 'N/A')}) - {entry.get('first_signed_at', 'N/A')}"
        ))
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, _safe(
            f"Second reviewer (co-sign): {entry.get('second_officer_name', 'N/A')} "
            f"({entry.get('second_officer_role', 'N/A')}) - {entry.get('second_signed_at', 'N/A')}"
        ))
        pdf.ln(6)

    pdf.output(output_path)
    return output_path, None
