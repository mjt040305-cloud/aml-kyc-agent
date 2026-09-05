"""
agent_graph.py
--------------
Orchestrates the AML/KYC agent as a LangGraph StateGraph.

This is the "agent architecture" layer: rather than a Streamlit script that
just calls functions in sequence, the pipeline is expressed as an explicit,
inspectable graph of nodes and edges:

    START -> normalize -> analyse -> human_review -> output -> END

The `normalize` node converts every transaction's amount to its USD
equivalent (see fx_normalize.py) BEFORE any AML rule runs - USD is the
fixed AML compliance baseline currency, never user-changeable. The
human_review node calls LangGraph's `interrupt()`, which genuinely
pauses graph execution and hands control back to the caller with the list
of transactions requiring sign-off. Execution only resumes - continuing on
to the output node - when the caller supplies a `Command(resume=...)`
containing the compliance officer's decisions. This is LangGraph's
documented human-in-the-loop pattern, not a UI-only checkpoint, and gives
the agent a durable, checkpointed execution state (via a checkpointer) that
could be persisted to a database in a production deployment for audit
purposes.

No LLM call is required for this pipeline to function - the "agent" here
refers to the autonomous, multi-step, stateful orchestration of the
compliance workflow, not to a language model. This keeps the deployed app
free to run (no API key required) while still demonstrating a genuine agent
architecture with explainability and auditability built in.
"""

from typing import TypedDict, List, Dict, Any
from datetime import datetime
import pandas as pd

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

from rules_engine import analyse_transactions
from fx_normalize import normalize_transactions, MissingFXRateError


class AgentState(TypedDict):
    transactions: List[Dict[str, Any]]   # raw input rows (original currency/amount)
    fx_rates: Dict[str, float]           # currency_code -> rate_to_usd, from app.py's FX panel
    fx_sources: Dict[str, Dict[str, Any]]  # currency_code -> {source, timestamp}
    normalized: List[Dict[str, Any]]     # rows + original_amount/currency + usd_equivalent
    rules_config: Dict[str, Any]         # sidebar-configurable AML thresholds
    analysed: List[Dict[str, Any]]       # rows + risk_score/bucket/breakdown
    reviews: Dict[str, Dict[str, Any]]   # transaction_id -> {status, reviewer, notes}
    final_report: List[Dict[str, Any]]


def node_normalize(state: AgentState) -> AgentState:
    """
    Currency normalization stage: converts every transaction to its USD
    equivalent before any AML evaluation happens. Raises MissingFXRateError
    (caught by run_pipeline) if a currency present in the data has no
    resolved rate - app.py's UI is expected to prevent this by requiring a
    rate for every present currency before allowing the run to start.
    """
    df = pd.DataFrame(state["transactions"])
    normalized_df = normalize_transactions(df, state.get("fx_rates", {}), state.get("fx_sources", {}))
    normalized_df["date"] = pd.to_datetime(normalized_df["date"]).astype(str)
    state["normalized"] = normalized_df.to_dict("records")
    return state


def node_analyse(state: AgentState) -> AgentState:
    """Stage: analyse + decide/flag. Operates on USD-normalized amounts."""
    df = pd.DataFrame(state["normalized"])
    analysed_df = analyse_transactions(df, state.get("rules_config"))
    # dates are Timestamps after analyse_transactions - make JSON/state safe
    analysed_df["date"] = analysed_df["date"].astype(str)
    state["analysed"] = analysed_df.to_dict("records")
    return state


def node_human_review(state: AgentState) -> AgentState:
    """
    Stage 4: human oversight checkpoint.

    Pauses graph execution and surfaces every Medium/High risk transaction
    to the caller. Execution will not proceed to node_output until the
    caller resumes the graph with a `Command(resume=<reviews dict>)`.
    """
    pending = [t for t in state["analysed"] if t["risk_bucket"] in ("High", "Medium")]

    if not pending:
        state["reviews"] = {}
        return state

    reviews = interrupt({"pending_transactions": pending})
    state["reviews"] = reviews
    return state


def node_output(state: AgentState) -> AgentState:
    """Stage 5: merge human decisions into the final, exportable report."""
    reviews = state.get("reviews", {}) or {}
    final = []
    sar_counter = 1
    year = datetime.now().year
    for t in state["analysed"]:
        r = reviews.get(t["transaction_id"], {})
        needs_review = t["risk_bucket"] in ("High", "Medium")
        t = dict(t)
        t["review_status"] = r.get("status", "Pending" if needs_review else "Not required")
        t["reviewer_notes"] = r.get("notes", "")
        t["reviewed_by"] = r.get("reviewer", "")
        if t["review_status"] == "Escalate to SAR filing":
            t["case_reference"] = f"SAR-{year}-{sar_counter:04d}"
            sar_counter += 1
        else:
            t["case_reference"] = ""
        final.append(t)
    state["final_report"] = final
    return state


def build_agent_graph():
    """Compile and return the AML agent graph with an in-memory checkpointer."""
    graph = StateGraph(AgentState)
    graph.add_node("normalize", node_normalize)
    graph.add_node("analyse", node_analyse)
    graph.add_node("human_review", node_human_review)
    graph.add_node("output", node_output)

    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "analyse")
    graph.add_edge("analyse", "human_review")
    graph.add_edge("human_review", "output")
    graph.add_edge("output", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def run_pipeline(compiled_graph, transactions: List[Dict[str, Any]], thread_id: str,
                  rules_config: Dict[str, Any] = None,
                  fx_rates: Dict[str, float] = None, fx_sources: Dict[str, Dict[str, Any]] = None):
    """
    Start (or restart) a fresh run of the agent for a given thread_id.

    `rules_config` overrides any subset of rules_engine.DEFAULT_CONFIG,
    e.g. thresholds and country classifications set by the compliance
    officer in the sidebar.
    `fx_rates`/`fx_sources` drive currency normalization (see
    fx_normalize.py) - app.py is expected to ensure every currency present
    in `transactions` has a resolved rate before calling this, but a
    missing rate is still handled gracefully here rather than crashing.

    Returns a dict with one of:
      {"status": "awaiting_review", "pending_transactions": [...]}
      {"status": "complete", "final_report": [...]}
      {"status": "fx_error", "missing_currencies": [...]}
    """
    graph_config = {"configurable": {"thread_id": thread_id}}
    initial_state: AgentState = {
        "transactions": transactions,
        "fx_rates": fx_rates or {},
        "fx_sources": fx_sources or {},
        "normalized": [],
        "rules_config": rules_config or {},
        "analysed": [], "reviews": {}, "final_report": [],
    }
    try:
        result = compiled_graph.invoke(initial_state, config=graph_config)
    except MissingFXRateError as e:
        return {"status": "fx_error", "missing_currencies": e.missing_currencies}

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        return {"status": "awaiting_review", "pending_transactions": payload["pending_transactions"]}

    return {"status": "complete", "final_report": result["final_report"]}


def resume_pipeline(compiled_graph, reviews: Dict[str, Dict[str, Any]], thread_id: str):
    """
    Resume a paused agent run with the compliance officer's decisions.

    `reviews` maps transaction_id -> {"status": ..., "reviewer": ..., "notes": ...}
    """
    graph_config = {"configurable": {"thread_id": thread_id}}
    result = compiled_graph.invoke(Command(resume=reviews), config=graph_config)
    return {"status": "complete", "final_report": result["final_report"]}
