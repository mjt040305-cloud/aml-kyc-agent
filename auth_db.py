"""
auth_db.py
----------
Individual compliance-officer account management, backed by SQLite.

Passwords are NEVER stored in plaintext - each is hashed with
PBKDF2-HMAC-SHA256 (200,000 iterations, via Python's standard `hashlib` -
no extra dependency) using a unique random salt per officer.

There is no code path anywhere in this module, or intended to exist in
app.py, that lets an officer type another officer's name to "become" them.
The ONLY way to attach an authenticated identity to a session is a
successful call to verify_login(). Every compliance decision recorded by
the app must use the officer dict returned from that call - never a
free-text name field.
"""

import sqlite3
import hashlib
import secrets
import os
from datetime import datetime
from tz_utils import zim_now_str
from contextlib import contextmanager

# On Streamlit Community Cloud this file lives on the app's local disk,
# which persists across ordinary session/browser restarts but is NOT
# guaranteed to survive a full app redeploy or container recycle on the
# free tier. See case_store.py's module docstring for the same caveat -
# this protects against browser crashes, network drops, and accidental
# tab closures within a running instance, not against Streamlit Cloud
# rebuilding the container from git.
DB_PATH = os.environ.get("AML_AGENT_DB_PATH", "aml_agent.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SECURITY_QUESTIONS = [
    "What was the name of your first school?",
    "What is your mother's maiden name?",
    "What was the make of your first car?",
    "What city were you born in?",
    "What is the name of your favourite childhood teacher?",
]


def init_auth_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS officers (
                officer_id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                security_question TEXT,
                security_answer_hash TEXT,
                security_answer_salt TEXT
            )
        """)
        # Backward-compatible migration for accounts registered before the
        # forgot-password feature existed - SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so attempt each and ignore if already present.
        for stmt in [
            "ALTER TABLE officers ADD COLUMN security_question TEXT",
            "ALTER TABLE officers ADD COLUMN security_answer_hash TEXT",
            "ALTER TABLE officers ADD COLUMN security_answer_salt TEXT",
        ]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()


def register_officer(full_name: str, officer_id: str, email: str, password: str, role: str,
                      security_question: str = None, security_answer: str = None):
    """Create a new officer account. Returns (success: bool, message: str).
    security_question/security_answer are optional for backward compatibility,
    but an account without them cannot use self-service password reset later -
    the registration form should always collect them for new accounts."""
    if not all([full_name.strip(), officer_id.strip(), email.strip(), password, role.strip()]):
        return False, "All fields are required."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."

    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(password, salt)

    answer_hash = answer_salt = None
    if security_question and security_answer:
        answer_salt = secrets.token_bytes(16)
        # Normalize case/whitespace so the answer isn't unreasonably strict
        answer_hash = _hash_password(security_answer.strip().lower(), answer_salt)

    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO officers (officer_id, full_name, email, password_hash, salt, role, created_at, "
                "security_question, security_answer_hash, security_answer_salt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (officer_id.strip(), full_name.strip(), email.strip().lower(), pw_hash, salt.hex(), role.strip(),
                 zim_now_str(), security_question, answer_hash, answer_salt.hex() if answer_salt else None),
            )
        except sqlite3.IntegrityError:
            return False, "An officer with this Officer ID or email already exists."
    return True, "Account created. You can now log in."


def get_security_question(email_or_id: str):
    """Returns the officer's security question string, or None if the
    account has none set (e.g. registered before this feature existed) or
    doesn't exist - callers should treat both cases the same (no self-service
    reset possible) without revealing which one it is, to avoid leaking
    whether an account exists."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT security_question FROM officers WHERE email = ? OR officer_id = ?",
            (email_or_id.strip().lower(), email_or_id.strip()),
        ).fetchone()
    if row is None or not row["security_question"]:
        return None
    return row["security_question"]


def reset_password(email_or_id: str, security_answer: str, new_password: str):
    """
    Self-service password reset via security question - no email/SMS
    infrastructure required. Returns (success: bool, message: str).
    """
    if len(new_password) < 8:
        return False, "New password must be at least 8 characters."

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM officers WHERE email = ? OR officer_id = ?",
            (email_or_id.strip().lower(), email_or_id.strip()),
        ).fetchone()
        if row is None or not row["security_answer_hash"]:
            return False, "No account found with a security question set up. Contact your administrator."

        salt = bytes.fromhex(row["security_answer_salt"])
        candidate_hash = _hash_password(security_answer.strip().lower(), salt)
        if not secrets.compare_digest(candidate_hash, row["security_answer_hash"]):
            return False, "Security answer is incorrect."

        new_salt = secrets.token_bytes(16)
        new_hash = _hash_password(new_password, new_salt)
        conn.execute(
            "UPDATE officers SET password_hash = ?, salt = ? WHERE officer_id = ?",
            (new_hash, new_salt.hex(), row["officer_id"]),
        )
    return True, "Password reset successfully. You can now log in with your new password."


def verify_login(email_or_id: str, password: str):
    """
    Returns the officer record (dict, password fields excluded) on success,
    or None on failure. This is the ONLY function in the application that
    establishes an authenticated officer identity.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM officers WHERE email = ? OR officer_id = ?",
            (email_or_id.strip().lower(), email_or_id.strip()),
        ).fetchone()
    if row is None:
        return None
    salt = bytes.fromhex(row["salt"])
    candidate_hash = _hash_password(password, salt)
    if secrets.compare_digest(candidate_hash, row["password_hash"]):
        officer = dict(row)
        officer.pop("password_hash", None)
        officer.pop("salt", None)
        return officer
    return None


def get_officer(officer_id: str):
    """Look up an officer's public record by ID (no password fields)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM officers WHERE officer_id = ?", (officer_id,)).fetchone()
    if row is None:
        return None
    officer = dict(row)
    officer.pop("password_hash", None)
    officer.pop("salt", None)
    return officer


def list_officers_by_roles(roles):
    """
    Returns public records (no password fields) for every officer whose
    role is in `roles` - used to populate a "route to" dropdown when an
    officer escalates a High-risk transaction and wants to direct the
    co-signature request to a specific senior colleague, rather than
    leaving it open to anyone with a qualifying role.
    """
    placeholders = ", ".join("?" for _ in roles)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM officers WHERE role IN ({placeholders}) ORDER BY full_name ASC", tuple(roles)
        ).fetchall()
    results = []
    for row in rows:
        officer = dict(row)
        officer.pop("password_hash", None)
        officer.pop("salt", None)
        results.append(officer)
    return results
