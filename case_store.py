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
                cosign_json TEXT,
                workflow_node TEXT,
                status TEXT NOT NULL DEFAULT 'in_progress',
                risk_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Backward-compatible migration: a database created before the
        # two-person sign-off feature won't have this column yet. SQLite
        # has no "ADD COLUMN IF NOT EXISTS", so attempt it and ignore the
        # error if the column is already there.
        try:
            conn.execute("ALTER TABLE cases ADD COLUMN cosign_json TEXT")
        except sqlite3.OperationalError:
            pass
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

    Refuses to save without a real case_id/officer_id - SQLite allows
    multiple NULL primary keys (unlike most databases), so without this
    guard a bug elsewhere could silently create an orphan row with no
    real identity, which would then correctly - but confusingly - fail
    case_belongs_to_officer() for everyone, including its rightful owner.

    Refuses to modify a case already marked 'completed' - returns
    (False, message) in that case so app.py can redirect the action to
    append_audit() instead of silently overwriting a signed decision.
    """
    if not case_id or not officer_id:
        return False, "Cannot save a case without a valid case_id and officer_id."

    with get_conn() as conn:
        existing = conn.execute("SELECT status FROM cases WHERE case_id = ?", (case_id,)).fetchone()

        if existing and existing["status"] == "completed":
            return False, "This case is completed and its signed decision cannot be modified."

        json_fields = {
            "transactions_json", "fx_state_json", "rules_config_json", "analysed_json",
            "pending_json", "final_report_json", "officer_notes_json", "review_state_json",
            "cosign_json",
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
                "pending_json", "final_report_json", "officer_notes_json", "review_state_json",
                "cosign_json"]:
        if case.get(key):
            case[key.replace("_json", "")] = json.loads(case[key])
        else:
            case[key.replace("_json", "")] = None
    return case


def list_unfinished_cases(officer_id):
    """Cases belonging to this officer that are still directly resumable
    by them (status='in_progress'). Deliberately excludes 'completed' AND
    'pending_cosign' cases - a case awaiting a second officer's
    co-signature must not be resumable through the normal review flow,
    or an autosave during that resume could silently revert its
    pending_cosign state. See list_own_pending_cosign_cases() for a
    read-only view of those instead.
    Excludes any row with a missing/empty case_id - such a row can never
    pass case_belongs_to_officer() (SQLite treats NULL != NULL, so it
    would always deny access, even to its own officer_id), so it must
    never be shown as resumable in the first place. save_case() now
    refuses to create these going forward; this filter also hides any
    that already exist from before that guard was added."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT case_id, workflow_node, status, risk_summary, updated_at FROM cases "
            "WHERE officer_id = ? AND status = 'in_progress' "
            "AND case_id IS NOT NULL AND TRIM(case_id) != '' "
            "ORDER BY updated_at DESC",
            (officer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_own_pending_cosign_cases(officer_id):
    """Read-only view for an officer to see their OWN cases currently
    awaiting a second officer's co-signature - informational only, no
    resume action, since only a DIFFERENT officer can act on these."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT case_id, cosign_json, updated_at FROM cases "
            "WHERE officer_id = ? AND status = 'pending_cosign' ORDER BY updated_at DESC",
            (officer_id,),
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["cosign"] = json.loads(d["cosign_json"]) if d.get("cosign_json") else {}
        results.append(d)
    return results


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

# ---------------------------------------------------------------------------
# SECURITY ALERTS - persistent threshold-breach monitoring
# ---------------------------------------------------------------------------

def record_security_alert(case_id, officer_id, transaction_id, customer_id, amount_usd, threshold_usd, alert_type="Threshold exceeded"):
    """Persist a threshold alert once per case/transaction."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM security_alerts WHERE case_id = ? AND transaction_id = ? AND alert_type = ?",
            (case_id, transaction_id, alert_type),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO security_alerts (case_id, officer_id, transaction_id, customer_id, amount_usd, threshold_usd, alert_type, triggered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, officer_id, transaction_id, customer_id, float(amount_usd), float(threshold_usd), alert_type, _now()),
        )
        return cur.lastrowid


def list_security_alerts(officer_id=None, limit=100):
    """Return newest threshold alerts, optionally limited to one officer."""
    with get_conn() as conn:
        if officer_id:
            rows = conn.execute(
                "SELECT * FROM security_alerts WHERE officer_id = ? ORDER BY triggered_at DESC, id DESC LIMIT ?",
                (officer_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM security_alerts ORDER BY triggered_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    return [dict(r) for r in rows]


def acknowledge_security_alert(alert_id, officer_id, officer_name):
    """Acknowledge an alert and record the acknowledgement in the audit log."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM security_alerts WHERE id = ?", (alert_id,)).fetchone()
        if row is None:
            return False, "Security alert not found."
        if row["acknowledged_by"]:
            return False, "This security alert has already been acknowledged."
        conn.execute(
            "UPDATE security_alerts SET acknowledged_by = ?, acknowledged_at = ? WHERE id = ?",
            (officer_id, _now(), alert_id),
        )
    append_audit(
        row["case_id"], officer_id, officer_name, "SECURITY ALERT ACKNOWLEDGED",
        decision=f"{row['transaction_id']}: {row['alert_type']} - USD {row['amount_usd']:,.2f} >= USD {row['threshold_usd']:,.2f}"
    )
    return True, "Security alert acknowledged."

# ---------------------------------------------------------------------------
# TWO-PERSON SIGN-OFF (maker-checker) for High/Critical risk escalations.
#
# When a transaction is escalated to SAR filing AND its risk bucket is
# High, the case does NOT complete on the first officer's signature alone.
# It moves to status='pending_cosign' and waits for a SECOND, DIFFERENT
# officer (with an appropriately senior role, enforced by app.py before
# calling record_second_signature) to co-sign. Only once every such
# transaction in the case has a second signature does the case become
# 'completed' - at which point save_case()'s existing completed-lock
# takes over exactly as it does for a single-signature case.
# ---------------------------------------------------------------------------

def submit_for_cosign(case_id, officer_id, officer_name, cosign_requirements: dict):
    """
    Marks a case as awaiting second sign-off instead of completing it
    outright. `cosign_requirements`: {transaction_id: {...any case-specific
    detail worth keeping, e.g. "risk_bucket", "decision", "amount_usd"}}
    for each transaction that requires a second signature. Each entry is
    stamped with the first officer's identity and timestamp here.
    """
    now = _now()
    stamped = {
        tid: {
            **details,
            "first_officer_id": officer_id, "first_officer_name": officer_name,
            "first_signed_at": now,
            "second_officer_id": None, "second_officer_name": None, "second_signed_at": None,
        }
        for tid, details in cosign_requirements.items()
    }
    save_case(case_id, officer_id, cosign_json=stamped, workflow_node="pending_cosign", status="pending_cosign")
    append_audit(case_id, officer_id, officer_name, "SUBMIT FOR SECOND SIGN-OFF",
                 previous_status="in_progress", new_status="pending_cosign",
                 decision=f"{len(cosign_requirements)} transaction(s) require a second signature")


def list_pending_cosign(exclude_officer_id):
    """Cases awaiting a second officer's co-signature, excluding cases
    originated by exclude_officer_id - an officer can never see their own
    escalation in a queue meant for approving someone else's decision
    (also re-enforced inside record_second_signature as a hard check)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT case_id, officer_id, risk_summary, cosign_json, updated_at FROM cases "
            "WHERE status = 'pending_cosign' AND officer_id != ? ORDER BY updated_at ASC",
            (exclude_officer_id,),
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["cosign"] = json.loads(d["cosign_json"]) if d.get("cosign_json") else {}
        results.append(d)
    return results


def record_second_signature(case_id, transaction_id, second_officer_id, second_officer_name):
    """
    Records a second officer's co-signature for one transaction in a case
    awaiting sign-off. Refuses if the second officer is the same as the
    first (no self-approval) or if this transaction was already
    co-signed. Once every transaction in the case's cosign record has a
    second signature, the case is automatically marked 'completed', at
    which point save_case()'s normal lock protects it exactly like any
    other completed case.

    Returns (success: bool, message: str).
    """
    case = load_case(case_id)
    if case is None:
        return False, "Case not found."
    if case["status"] != "pending_cosign":
        return False, "This case is not currently awaiting a second sign-off."

    cosign = case.get("cosign") or {}
    entry = cosign.get(transaction_id)
    if entry is None:
        return False, "This transaction is not part of the case's co-signature requirements."
    if entry["first_officer_id"] == second_officer_id:
        return False, "The officer who escalated this transaction cannot also provide the second sign-off."
    if entry.get("second_officer_id"):
        return False, "This transaction already has a second signature recorded."

    entry["second_officer_id"] = second_officer_id
    entry["second_officer_name"] = second_officer_name
    entry["second_signed_at"] = _now()
    cosign[transaction_id] = entry

    all_signed = all(e.get("second_officer_id") for e in cosign.values())
    new_status = "completed" if all_signed else "pending_cosign"

    with get_conn() as conn:
        conn.execute(
            "UPDATE cases SET cosign_json = ?, status = ?, updated_at = ? WHERE case_id = ?",
            (json.dumps(cosign), new_status, _now(), case_id),
        )
    append_audit(case_id, second_officer_id, second_officer_name, "SECOND SIGN-OFF (CO-SIGN)",
                 previous_status="pending_cosign", new_status=new_status,
                 decision=f"{transaction_id}: co-signed")
    if all_signed:
        append_audit(case_id, second_officer_id, second_officer_name, "CASE COMPLETED (ALL CO-SIGNATURES RECEIVED)",
                     previous_status="pending_cosign", new_status="completed",
                     decision="All required co-signatures received")
    return True, "Co-signature recorded." + (" All required signatures received - case is now completed." if all_signed else " Awaiting further co-signature(s) on this case.")
