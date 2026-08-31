"""
pdf_report.py
-------------
Generates a filing-ready PDF compliance report from the agent's final
output. Kept separate from app.py so the report format can be revised
without touching the Streamlit UI or the agent pipeline.

Uses fpdf2 - a lightweight, pure-Python PDF library with no external
system dependencies, which keeps the Streamlit Community Cloud deployment
simple (no LaTeX/wkhtmltopdf install step required).
"""

from datetime import datetime
from fpdf import FPDF

NAVY = (31, 56, 100)
LIGHTGREY = (240, 240, 240)


class ComplianceReportPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*NAVY)
        self.multi_cell(0, 10, "AML/KYC Compliance Report", align="C")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(90, 90, 90)
        self.multi_cell(0, 6, f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} - Educational prototype, simulated data only", align="C")
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.multi_cell(0, 10, f"Page {self.page_no()}", align="C")


def _safe(text) -> str:
    """fpdf2's default Helvetica font is latin-1 only; strip anything else."""
    return str(text).encode("latin-1", "ignore").decode("latin-1")


def build_pdf(final_report: list, rules_config: dict, output_path: str):
    """
    final_report: list of transaction dicts (as produced by agent_graph output)
    rules_config: the AML thresholds used for this run (for audit purposes)
    """
    pdf = ComplianceReportPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    total = len(final_report)
    high = sum(1 for t in final_report if t["risk_bucket"] == "High")
    medium = sum(1 for t in final_report if t["risk_bucket"] == "Medium")
    flagged_amount = sum(t["amount"] for t in final_report if t["risk_bucket"] in ("High", "Medium"))
    escalated = sum(1 for t in final_report if t.get("review_status") == "Escalate to SAR filing")

    # ---------------- Executive summary ----------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 8, "Executive Summary")
    pdf.set_font("Helvetica", "", 10)
    summary_lines = [
        f"Total transactions analysed: {total}",
        f"High risk: {high}   |   Medium risk: {medium}",
        f"Total value of flagged transactions: ${flagged_amount:,.2f}",
        f"Transactions escalated to SAR filing: {escalated}",
    ]
    for line in summary_lines:
        pdf.multi_cell(0, 6, _safe(line))
    pdf.ln(4)

    # ---------------- Rule configuration used ----------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(0, 8, "AML Rule Configuration Applied")
    pdf.set_font("Helvetica", "", 9)
    cfg_lines = [
        f"Structuring threshold: ${rules_config.get('structuring_threshold', 10000):,.0f} (margin {rules_config.get('structuring_margin', 0.10)*100:.0f}%)",
        f"High-risk countries: {', '.join(rules_config.get('high_risk_countries', []))}",
        f"Rapid movement: {rules_config.get('rapid_movement_min_txns', 3)}+ txns within {rules_config.get('rapid_movement_hours', 24)}h",
        f"Velocity deviation threshold: {rules_config.get('velocity_std_multiplier', 3)} standard deviations",
    ]
    for line in cfg_lines:
        pdf.multi_cell(0, 5, _safe(line))
    pdf.ln(4)

    # ---------------- Flagged transaction table ----------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(0, 8, "Flagged Transactions (Medium/High Risk)")

    col_widths = [22, 22, 22, 30, 16, 60, 40, 30]
    headers = ["Txn ID", "Customer", "Amount", "Date", "Score", "Flag Reasons", "Decision", "Reviewer"]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 7, _safe(h), border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(0, 0, 0)
    row_fill = False
    for t in final_report:
        if t["risk_bucket"] not in ("High", "Medium"):
            continue
        pdf.set_fill_color(*(LIGHTGREY if row_fill else (255, 255, 255)))
        row_fill = not row_fill

        y_before = pdf.get_y()
        x_before = pdf.get_x()

        values = [
            t["transaction_id"], t["customer_id"], f"${t['amount']:,.0f}",
            str(t["date"])[:10], str(t["risk_score"]),
        ]
        for w, v in zip(col_widths[:5], values):
            pdf.cell(w, 6, _safe(v), border=1, fill=True)

        # Flag reasons + decision + reviewer as multi-line-safe truncated cells
        pdf.cell(col_widths[5], 6, _safe(t.get("flag_reasons", ""))[:70], border=1, fill=True)
        pdf.cell(col_widths[6], 6, _safe(t.get("review_status", ""))[:35], border=1, fill=True)
        pdf.cell(col_widths[7], 6, _safe(t.get("reviewed_by", ""))[:25], border=1, fill=True)
        pdf.ln()

    pdf.output(output_path)
    return output_path
