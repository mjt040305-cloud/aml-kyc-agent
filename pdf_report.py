"""
pdf_report.py
-------------
Generates a filing-ready PDF compliance report from the agent's final
output. Kept separate from app.py so the report format can be revised
without touching the Streamlit UI or the agent pipeline.

Every monetary figure is shown as BOTH the original transaction
amount/currency (exactly as uploaded, never modified) and its USD
equivalent (what every AML rule and threshold was actually evaluated
against) - see fx_normalize.py for the normalization this reflects.
Jurisdiction rows show both FATF's reference status and the
institution's own configured classification, kept visibly separate per
the agent's risk-based-approach design (see fatf_reference.py).

Uses fpdf2 - a lightweight, pure-Python PDF library with no external
system dependencies, which keeps the Streamlit Community Cloud deployment
simple (no LaTeX/wkhtmltopdf install step required).
"""

from datetime import datetime
from fpdf import FPDF

import fatf_reference

NAVY = (31, 56, 100)
LIGHTGREY = (240, 240, 240)

PDF_SAFE_SYMBOL = {"USD": "$", "ZAR": "R", "ZWG": "ZiG "}


def _fmt(amount: float, currency_code: str) -> str:
    symbol = PDF_SAFE_SYMBOL.get(currency_code, "")
    return f"{symbol}{amount:,.2f}" if symbol else f"{currency_code} {amount:,.2f}"


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
    final_report: list of transaction dicts (as produced by agent_graph's
        output node) - each already carries original_amount,
        original_currency, usd_exchange_rate, usd_equivalent (== amount),
        fx_rate_source, fx_rate_timestamp, fx_conversion_status.
    rules_config: the AML thresholds AND country_classifications used for
        this run (for audit purposes) - always USD-denominated.
    """
    pdf = ComplianceReportPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    total = len(final_report)
    high = sum(1 for t in final_report if t["risk_bucket"] == "High")
    medium = sum(1 for t in final_report if t["risk_bucket"] == "Medium")
    flagged_amount_usd = sum(t["amount"] for t in final_report if t["risk_bucket"] in ("High", "Medium"))
    escalated = sum(1 for t in final_report if t.get("review_status") == "Escalate to SAR filing")
    country_classifications = rules_config.get("country_classifications", {}) or {}

    # ---------------- Executive summary ----------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 8, "Executive Summary")
    pdf.set_font("Helvetica", "", 10)
    summary_lines = [
        f"Total transactions analysed: {total}  (AML baseline currency: USD, fixed)",
        f"High risk: {high}   |   Medium risk: {medium}",
        f"Total value of flagged transactions: {_fmt(flagged_amount_usd, 'USD')}",
        f"Transactions escalated to SAR filing: {escalated}",
    ]
    for line in summary_lines:
        pdf.multi_cell(0, 6, _safe(line))
    pdf.ln(4)

    # ---------------- Rule configuration used ----------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(0, 8, "Institutional Monitoring Parameters Applied")
    pdf.set_font("Helvetica", "", 9)
    classified = [f"{c.title()} ({v})" for c, v in country_classifications.items() if v and v != "Low"]
    cfg_lines = [
        f"Institutional Structuring Alert Threshold: {_fmt(rules_config.get('structuring_threshold', 10000), 'USD')} "
        f"(margin {rules_config.get('structuring_margin', 0.10)*100:.0f}%) - an institution-configured "
        f"monitoring threshold, not a universal RBZ statutory reporting threshold.",
        f"Rapid movement: {rules_config.get('rapid_movement_min_txns', 3)}+ txns within {rules_config.get('rapid_movement_hours', 24)}h",
        f"Velocity deviation threshold: {rules_config.get('velocity_std_multiplier', 3)} standard deviations",
        f"Institution-classified jurisdictions (Medium/High/Prohibited): {', '.join(classified) if classified else 'none configured'}",
    ]
    for line in cfg_lines:
        pdf.multi_cell(0, 5, _safe(line))
    pdf.ln(4)

    # ---------------- Flagged transaction table ----------------
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(0, 8, "Flagged Transactions (Medium/High Risk) - Full Audit Trail")

    col_widths = [14, 16, 22, 20, 18, 10, 14, 18, 22, 18, 40, 24, 18]
    headers = ["Txn ID", "Customer", "Original Amt", "USD Equiv.", "FX Source",
               "Score", "Case #", "Country", "FATF Status", "Inst. Class.",
               "Flag Reasons", "Decision", "Reviewer"]

    pdf.set_font("Helvetica", "B", 7)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 7, _safe(h), border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 6.5)
    pdf.set_text_color(0, 0, 0)
    row_fill = False
    for t in final_report:
        if t["risk_bucket"] not in ("High", "Medium"):
            continue
        pdf.set_fill_color(*(LIGHTGREY if row_fill else (255, 255, 255)))
        row_fill = not row_fill

        country = t.get("counterparty_country", "")
        fatf_status = fatf_reference.get_fatf_status(country)
        inst_class = country_classifications.get(country.lower(), "Not classified")
        original_currency = t.get("original_currency", "USD")
        original_amount = t.get("original_amount", t.get("amount", 0))

        values = [
            t["transaction_id"], t["customer_id"],
            _fmt(original_amount, original_currency),
            _fmt(t["amount"], "USD"),
            t.get("fx_rate_source", "Fixed"),
            str(t["risk_score"]),
            t.get("case_reference", "") or "-",
            country,
            fatf_status,
            inst_class,
        ]
        for w, v in zip(col_widths[:10], values):
            pdf.cell(w, 6, _safe(v), border=1, fill=True)

        pdf.cell(col_widths[10], 6, _safe(t.get("flag_reasons", ""))[:60], border=1, fill=True)
        pdf.cell(col_widths[11], 6, _safe(t.get("review_status", ""))[:26], border=1, fill=True)
        pdf.cell(col_widths[12], 6, _safe(t.get("reviewed_by", ""))[:18], border=1, fill=True)
        pdf.ln()

    pdf.output(output_path)
    return output_path
