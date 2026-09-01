"""
agent_graph.py
--------------
Orchestrates the AML/KYC agent as a LangGraph StateGraph.

This is the "agent architecture" layer: rather than a Streamlit script that
just calls functions in sequence, the pipeline is expressed as an explicit,
inspectable graph of nodes and edges:

    START -> analyse -> human_review -> output -> END

The human_review node calls LangGraph's `interrupt()`, which genuinely
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


class AgentState(TypedDict):
    transactions: List[Dict[str, Any]]   # raw input rows
    rules_config: Dict[str, Any]         # sidebar-configurable AML thresholds
    analysed: List[Dict[str, Any]]       # rows + risk_score/bucket/breakdown
    reviews: Dict[str, Dict[str, Any]]   # transaction_id -> {status, reviewer, notes}
    final_report: List[Dict[str, Any]]


def node_analyse(state: AgentState) -> AgentState:
    """Stage 2-3: read + analyse + decide/flag."""
    df = pd.DataFrame(state["transactions"])
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
    graph.add_node("analyse", node_analyse)
    graph.add_node("human_review", node_human_review)
    graph.add_node("output", node_output)

    graph.add_edge(START, "analyse")
    graph.add_edge("analyse", "human_review")
    graph.add_edge("human_review", "output")
    graph.add_edge("output", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def run_pipeline(compiled_graph, transactions: List[Dict[str, Any]], thread_id: str, rules_config: Dict[str, Any] = None):
    """
    Start (or restart) a fresh run of the agent for a given thread_id.

    `rules_config` overrides any subset of rules_engine.DEFAULT_CONFIG,
    e.g. thresholds set by the compliance officer in the sidebar.

    Returns a dict with either:
      {"status": "awaiting_review", "pending_transactions": [...]}
    or (if nothing needed review):
      {"status": "complete", "final_report": [...]}
    """
    graph_config = {"configurable": {"thread_id": thread_id}}
    initial_state: AgentState = {
        "transactions": transactions,
        "rules_config": rules_config or {},
        "analysed": [], "reviews": {}, "final_report": [],
    }
    result = compiled_graph.invoke(initial_state, config=graph_config)

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
