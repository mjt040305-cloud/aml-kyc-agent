"""
case_store.py
--------------
Persistent case storage for the AML/KYC Compliance Flagging Agent, backed
by SQLite - NOT Streamlit session_state, which is lost on any browser
crash, tab close, network drop, or server restart.

A "case" is one uploaded transaction batch under investigation by one
authenticated officer. Every important workflow action (analysis
complete, notes added, decision recorded, sign-off) calls save_case() so
the investigation can be fully restored - including which transactions
were flagged, their AML results, currency/FX-normalized values, the
officer's notes, and exactly which workflow stage was reached - without
re-running the pipeline.

CAVEAT (stated plainly, not hidden): on Streamlit Community Cloud's free
tier, this SQLite file lives on the app's local disk, which persists
across ordinary session crashes, browser closures, and network drops
within a running container, but is NOT guaranteed to survive a full app
redeploy or container recycle. For production durability across
redeploys, this file would need to move to a persistent volume or an
external database (e.g. Postgres) - the schema and functions below are
written so that swap is a drop-in change (only get_conn() would need to
target a different backend).

Completed-case protection: once a case's status is 'completed', save_case()
refuses to modify its decision fields - any further authorized action is
recorded as a NEW audit_log entry, never a silent overwrite of the
original signed decision.
"""

import sqlite3
import json
import os
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.environ.get("AML_AGENT_DB_PATH", "aml_agent.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_case_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                officer_id TEXT NOT NULL,
                thread_id TEXT,
                transactions_json TEXT,
                fx_state_json TEXT,
                rules_config_json TEXT,
                analysed_json TEXT,
                pending_json TEXT,
                final_report_json TEXT,
                officer_notes_json TEXT,
                review_state_json TEXT,
                workflow_node TEXT,
                status TEXT NOT NULL DEFAULT 'in_progress',
                risk_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                officer_id TEXT NOT NULL,
                officer_name TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_status TEXT,
                new_status TEXT,
                decision TEXT,
                timestamp TEXT NOT NULL
            )
        """)


def generate_case_id():
    """e.g. AML-2026-00125 - sequential per year."""
    year = datetime.now().year
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM cases WHERE case_id LIKE ?", (f"AML-{year}-%",)
        ).fetchone()
    seq = row["n"] + 1
    return f"AML-{year}-{seq:05d}"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_case(case_id, officer_id, **fields):
    """
    Create or update a case. JSON-serializable fields (transactions,
    fx_state, rules_config, analysed, pending, final_report, review_state)
    are stored as JSON text. `workflow_node` and `status` are stored as
    plain strings.

    Refuses to modify a case already marked 'completed' - returns
    (False, message) in that case so app.py can redirect the action to
    append_audit() instead of silently overwriting a signed decision.
    """
    with get_conn() as conn:
        existing = conn.execute("SELECT status FROM cases WHERE case_id = ?", (case_id,)).fetchone()

        if existing and existing["status"] == "completed":
            return False, "This case is completed and its signed decision cannot be modified."

        json_fields = {
            "transactions_json", "fx_state_json", "rules_config_json", "analysed_json",
            "pending_json", "final_report_json", "officer_notes_json", "review_state_json",
        }
        columns, values = [], []
        for key, value in fields.items():
            columns.append(key)
            values.append(json.dumps(value) if key in json_fields else value)

        now = _now()
        if existing:
            set_clause = ", ".join(f"{c} = ?" for c in columns)
            conn.execute(
                f"UPDATE cases SET {set_clause}, updated_at = ? WHERE case_id = ?",
                values + [now, case_id],
            )
        else:
            columns = ["case_id", "officer_id", "created_at", "updated_at"] + columns
            values = [case_id, officer_id, now, now] + values
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO cases ({', '.join(columns)}) VALUES ({placeholders})", values
            )
    return True, "Case saved."


def load_case(case_id):
    """Returns the case as a dict with JSON fields decoded, or None."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    case = dict(row)
    for key in ["transactions_json", "fx_state_json", "rules_config_json", "analysed_json",
                "pending_json", "final_report_json", "officer_notes_json", "review_state_json"]:
        if case.get(key):
            case[key.replace("_json", "")] = json.loads(case[key])
        else:
            case[key.replace("_json", "")] = None
    return case


def list_unfinished_cases(officer_id):
    """Cases belonging to this officer that are not yet completed."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT case_id, workflow_node, status, risk_summary, updated_at FROM cases "
            "WHERE officer_id = ? AND status != 'completed' ORDER BY updated_at DESC",
            (officer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def case_belongs_to_officer(case_id, officer_id) -> bool:
    """Authorization check - MUST be called before ever restoring a case
    into a session, so an officer can never resume someone else's case."""
    with get_conn() as conn:
        row = conn.execute("SELECT officer_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    return row is not None and row["officer_id"] == officer_id


def mark_case_completed(case_id, officer_id, officer_name, decision_summary):
    """Finalizes a case. After this call, save_case() will refuse further
    modification of this case's fields - see save_case()'s docstring."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE cases SET status = 'completed', updated_at = ? WHERE case_id = ?",
            (_now(), case_id),
        )
    append_audit(case_id, officer_id, officer_name, "SIGN & COMPLETE DECISION",
                 previous_status="in_progress", new_status="completed", decision=decision_summary)


def append_audit(case_id, officer_id, officer_name, action, previous_status=None, new_status=None, decision=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (case_id, officer_id, officer_name, action, previous_status, "
            "new_status, decision, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, officer_id, officer_name, action, previous_status, new_status, decision, _now()),
        )


def get_audit_trail(case_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE case_id = ? ORDER BY timestamp ASC", (case_id,)
        ).fetchall()
    return [dict(r) for r in rows]
