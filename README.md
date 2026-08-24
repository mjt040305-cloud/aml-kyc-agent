# AML/KYC Compliance Flagging Agent

**Course:** HBF2212 - Artificial Intelligence in Finance
**Project:** Project 2 - AI Agent for a finance business problem

## 1. Business problem

Financial institutions (banks, mobile money operators, remittance firms) are
legally required to monitor customer transactions for money-laundering red
flags under AML/CFT regulation (e.g. RBZ's AML/CFT guidelines in Zimbabwe,
FATF recommendations globally). Manual review of every transaction does not
scale, and compliance teams are often left triaging thousands of transactions
against a checklist of red flags under time pressure. Under-detection risks
regulatory sanction; over-flagging wastes scarce compliance officer time.

This agent automates the **first-pass detection and triage** of suspicious
transactions, while keeping a human compliance officer as the final decision
maker - it is a decision-support agent, not an autonomous reporting system.

## 2. What the agent does (pipeline)

The agent performs a sequence of independent steps rather than a single
prompt/response:

1. **Read** - ingest a CSV of transactions (uploaded by the user, or the
   bundled sample dataset).
2. **Analyse** - run six explainable, rule-based AML checks over every
   transaction:
   - Structuring/smurfing (amounts just under the reporting threshold)
   - High-risk jurisdiction counterparties
   - Rapid movement of funds (multiple transactions in a short window)
   - Unusually large round-number amounts
   - Deviation from a customer's own historical transaction pattern
   - Existing KYC risk profile combined with a significant transaction
3. **Decide/flag** - each transaction is scored and bucketed into
   None / Low / Medium / High risk, with a plain-English explanation of
   every rule that triggered.
4. **Human oversight checkpoint** - every Medium/High risk transaction is
   presented to a compliance officer inside the app, who must record a
   decision (Approve as false positive / Escalate to SAR filing / Dismiss)
   before it is included in the final report. **No transaction is ever
   auto-reported by the agent.**
5. **Output** - a downloadable, human-reviewed compliance log (CSV) suitable
   for internal audit or regulatory filing.

## 3. Repository structure

```
aml-agent/
├── app.py                          # Streamlit app - orchestrates the pipeline & UI
├── rules_engine.py                 # Analysis logic (read -> analyse -> decide)
├── sample_transactions.csv         # Simulated dataset for demo/testing
├── requirements.txt
├── .streamlit/secrets.toml.example # Template for any future API keys (no real secrets)
├── .gitignore
└── README.md
```

## 4. Running locally

```bash
git clone <your-repo-url>
cd aml-agent
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL Streamlit prints (usually http://localhost:8501).

Click **"Use sample data instead"** to try it immediately, or upload your own
CSV with these columns:

| column | description |
|---|---|
| transaction_id | unique transaction reference |
| customer_id | unique customer reference |
| date | transaction date/time |
| amount | transaction amount |
| counterparty_country | country of the receiving/sending party |
| transaction_type | e.g. wire_transfer, cash_deposit, mobile_money |
| customer_risk_profile | Low / Medium / High (from existing KYC records) |

## 5. Deploying to Streamlit Community Cloud

1. Push this repository to GitHub (public or accessible to your grader).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in, and click
   "New app".
3. Select this repository, branch `main`, and file path `app.py`.
4. Deploy. No secrets are required for the app to run in its current form.
5. If you extend the agent with an LLM call (e.g. to generate a narrative
   summary of flagged transactions), add your API key under the app's
   **Settings > Secrets** panel using the format shown in
   `.streamlit/secrets.toml.example` - never commit a real key to GitHub.

## 6. Data and security notes

- All data used for testing/demo is **synthetically generated**
  (`sample_transactions.csv`); no real customer or client data is used.
- The app does not connect to any live banking system.
- No API keys or credentials are committed to this repository.

## 7. Limitations

- Rules are threshold-based and illustrative, not calibrated against a real
  institution's transaction history or a regulator-approved policy.
- No entity-resolution/network analysis across related accounts.
- No persistence layer - review decisions exist only for the current session
  unless exported.
- Intended as an educational prototype, not a production compliance system.

## 8. Human oversight checkpoint

This is the core control built into the workflow: the agent's rule engine
only ever *proposes* a risk classification. A human compliance officer must
review and record a decision on every Medium/High risk transaction before it
appears in the final exportable report, and the app visibly warns if any
flagged transaction is still pending review. This keeps a human accountable
for any regulatory filing decision, consistent with AML/CFT compliance
practice.
