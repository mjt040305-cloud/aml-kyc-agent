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

## 2. Agent architecture

The agent is orchestrated as an explicit **LangGraph state graph**
(`agent_graph.py`), not a linear script:

```
START -> analyse -> human_review (interrupt) -> output -> END
```

- **analyse** - runs the rules engine and produces a risk score per
  transaction, decomposed into four categories (see below).
- **human_review** - calls LangGraph's `interrupt()`. This genuinely
  **pauses graph execution**, checkpointed via an in-memory checkpointer
  keyed to the session. The graph cannot reach `output` for a flagged
  transaction until the caller resumes it with a `Command(resume=...)`
  containing a compliance officer's decision - this is a structural
  control, not just a disabled button in the UI.
- **output** - resumes once decisions are supplied and merges them into
  the final, exportable report.

No LLM call is required for the graph to run - "agent" here refers to the
autonomous, stateful, multi-step orchestration of the compliance workflow.
This keeps the deployed app free to run with no API key required, while
still demonstrating genuine agent architecture, checkpointed state, and a
structural (not just cosmetic) human-in-the-loop control.

## 3. Risk scoring model

Every transaction receives an **Overall Risk Score** that is the sum of
four category scores, mirroring how a real banking AML system decomposes
risk:

```
Overall Risk Score = Customer Risk + Transaction Risk
                      + Geographic Risk + Behavioural Risk
```

| Category | What it captures | Rules in this category |
|---|---|---|
| **Customer Risk** | Existing KYC classification | Elevated KYC risk profile |
| **Transaction Risk** | Characteristics of this transaction | Structuring/smurfing, round-number amount |
| **Geographic Risk** | Counterparty jurisdiction | High-risk country |
| **Behavioural Risk** | Deviation from the customer's own pattern | Rapid fund movement, velocity deviation |

Each triggered rule is shown in the UI with a severity indicator
(🔴 High / 🟠 Medium / 🟢 Low) and a plain-English reason, e.g.:

```
AML Rules Triggered
🔴 High-risk jurisdiction (Geographic) - Counterparty jurisdiction 'Iran' is on the high-risk country list
🟠 Elevated KYC risk profile (Customer) - Customer is already flagged High risk...
🟢 Round-number amount (Transaction) - Large round-number amount ($15,000.00)...
```

## 4. What the agent does (pipeline)

1. **Read** - ingest a CSV of transactions (uploaded by the user, or the
   bundled sample dataset).
2. **Analyse** - score every transaction across the four risk categories
   above using six explainable rules.
3. **Decide/flag** - transactions are bucketed into None / Low / Medium /
   High risk based on the summed score.
4. **Human oversight checkpoint** - the LangGraph agent pauses execution
   (via `interrupt()`) and presents every Medium/High risk transaction to
   a compliance officer, who must record a decision (Approve as false
   positive / Escalate to SAR filing / Dismiss) before the graph can
   proceed. **No transaction is ever auto-reported by the agent.**
5. **Output** - the graph resumes and produces a downloadable,
   human-reviewed compliance log (CSV) suitable for internal audit or
   regulatory filing.

## 5. Repository structure

```
aml-agent/
├── app.py                          # Streamlit UI - drives the agent graph
├── agent_graph.py                  # LangGraph orchestration (analyse -> human_review -> output)
├── rules_engine.py                 # Rules + risk-category scoring logic
├── sample_transactions.csv         # Simulated dataset for demo/testing
├── requirements.txt
├── .streamlit/secrets.toml.example # Template for any future API keys (no real secrets)
├── .gitignore
└── README.md
```

## 6. Running locally

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

## 7. Deploying to Streamlit Community Cloud

1. Push this repository to GitHub (public or accessible to your grader).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in, and click
   "New app".
3. Select this repository, branch `main`, and file path `app.py`.
4. Deploy. No secrets are required for the app to run in its current form.
5. If you extend the agent with an LLM call (e.g. to generate a narrative
   summary of flagged transactions), add your API key under the app's
   **Settings > Secrets** panel using the format shown in
   `.streamlit/secrets.toml.example` - never commit a real key to GitHub.

## 8. Data and security notes

- All data used for testing/demo is **synthetically generated**
  (`sample_transactions.csv`); no real customer or client data is used.
- The app does not connect to any live banking system.
- No API keys or credentials are committed to this repository.

## 9. Limitations

- Rules are threshold-based and illustrative, not calibrated against a real
  institution's transaction history or a regulator-approved policy.
- No entity-resolution/network analysis across related accounts.
- The LangGraph checkpointer is in-memory (`MemorySaver`), so paused agent
  runs do not survive an app restart; a production deployment would swap
  this for a persistent checkpointer (e.g. Postgres/SQLite) so paused,
  awaiting-review runs and their audit trail survive restarts.
- Intended as an educational prototype, not a production compliance system.

## 10. Human oversight checkpoint

This is the core control built into the agent's architecture: the graph
structurally cannot reach its output node for a flagged transaction without
a human decision. The rules engine only ever *proposes* a risk
classification; LangGraph's `interrupt()` genuinely pauses execution at the
`human_review` node until a compliance officer supplies a decision for
every Medium/High risk transaction. The app visibly warns while any
decision is still pending, and the "Submit reviews & resume agent" button
is disabled until every transaction has one. This keeps a human accountable
for any regulatory filing decision, consistent with AML/CFT compliance
practice and the literature on human-in-the-loop compliance systems.
