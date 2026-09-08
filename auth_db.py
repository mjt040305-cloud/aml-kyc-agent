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
                created_at TEXT NOT NULL
            )
        """)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()


def register_officer(full_name: str, officer_id: str, email: str, password: str, role: str):
    """Create a new officer account. Returns (success: bool, message: str)."""
    if not all([full_name.strip(), officer_id.strip(), email.strip(), password, role.strip()]):
        return False, "All fields are required."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."

    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(password, salt)

    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO officers (officer_id, full_name, email, password_hash, salt, role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (officer_id.strip(), full_name.strip(), email.strip().lower(), pw_hash, salt.hex(), role.strip(),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        except sqlite3.IntegrityError:
            return False, "An officer with this Officer ID or email already exists."
    return True, "Account created. You can now log in."


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
