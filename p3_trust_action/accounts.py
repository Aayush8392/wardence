"""
Real accounts (2026-07-22) -- supersedes the original "admin hands out raw
bearer tokens" plan for demo-trigger, and now covers admin too (unified,
per user decision, WITH TOTP required for admin specifically to close the
security gap a public-facing login form would otherwise open -- see
wardence_frontend.md's Auth Model section for the full reasoning).

One table for both roles:
  - demo-trigger: username + password only. Admin creates these via the
    account-management endpoints in operator_api.py -- one permanent
    account for the project owner's own testing (permanent is fine here
    because it's revocable: admin can delete/revoke/change its password
    instantly), any others time-limited (24h default).
  - admin: username + password + TOTP secret, REQUIRED at login. A
    leaked/phished password alone is not enough to authenticate as admin
    -- this is the actual mitigation, not obscurity.

Password hashing: bcrypt (via the `bcrypt` package directly, not passlib
-- one hashing scheme is all this needs).
"""

import datetime
import sqlite3
from pathlib import Path

import bcrypt
import pyotp

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

ROLES = {"admin", "demo-trigger", "viewer"}

# How many recent failed attempts for a given username lock out further
# tries, and for how long -- defeats brute-force guessing regardless of
# password strength. Same shape as demo-trigger's existing per-IP
# cooldown in operator_api.py, applied here per-USERNAME instead (an
# attacker guessing passwords targets a specific account, not an IP).
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_MINUTES = 15


def ensure_accounts_tables(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            totp_secret TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            success INTEGER NOT NULL,
            ip TEXT,
            attempted_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_account(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    expires_hours: int | None = None,
    totp_secret: str | None = None,
) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role '{role}', must be one of {ROLES}")
    if role == "admin" and not totp_secret:
        raise ValueError("admin accounts require a totp_secret")

    expires_at = None
    if expires_hours is not None:
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=expires_hours)
        ).isoformat()

    conn.execute(
        """
        INSERT INTO accounts (username, password_hash, role, totp_secret, active, expires_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (username, _hash_password(password), role, totp_secret, expires_at),
    )
    conn.commit()


def get_account(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute(
        "SELECT username, password_hash, role, totp_secret, active, expires_at, created_at "
        "FROM accounts WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return None
    return {
        "username": row[0],
        "password_hash": row[1],
        "role": row[2],
        "totp_secret": row[3],
        "active": bool(row[4]),
        "expires_at": row[5],
        "created_at": row[6],
    }


def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    """Never returns password_hash/totp_secret -- listing is for the
    admin panel to show who exists, not to leak credentials."""
    rows = conn.execute(
        "SELECT username, role, active, expires_at, created_at FROM accounts ORDER BY created_at"
    ).fetchall()
    return [
        {
            "username": r[0],
            "role": r[1],
            "active": bool(r[2]),
            "expires_at": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


def delete_account(conn: sqlite3.Connection, username: str) -> None:
    conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
    conn.commit()


def set_active(conn: sqlite3.Connection, username: str, active: bool) -> None:
    conn.execute("UPDATE accounts SET active = ? WHERE username = ?", (int(active), username))
    conn.commit()


def change_password(conn: sqlite3.Connection, username: str, new_password: str) -> None:
    conn.execute(
        "UPDATE accounts SET password_hash = ? WHERE username = ?",
        (_hash_password(new_password), username),
    )
    conn.commit()


def _is_locked_out(conn: sqlite3.Connection, username: str) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM login_attempts
        WHERE username = ? AND success = 0
          AND attempted_at > datetime('now', ?)
        """,
        (username, f"-{LOCKOUT_WINDOW_MINUTES} minutes"),
    ).fetchone()
    return row[0] >= MAX_FAILED_ATTEMPTS


def _record_attempt(conn: sqlite3.Connection, username: str, success: bool, ip: str | None):
    conn.execute(
        "INSERT INTO login_attempts (username, success, ip) VALUES (?, ?, ?)",
        (username, int(success), ip),
    )
    conn.commit()


def is_expired(account: dict) -> bool:
    if account["expires_at"] is None:
        return False
    expires_at = datetime.datetime.fromisoformat(account["expires_at"])
    return datetime.datetime.now(datetime.timezone.utc) > expires_at


def hours_until_expiry(account: dict) -> float | None:
    """None means no expiry (permanent account) -- caller decides the
    session token's own lifetime in that case."""
    if account["expires_at"] is None:
        return None
    expires_at = datetime.datetime.fromisoformat(account["expires_at"])
    remaining = (expires_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600
    return max(remaining, 0)


def generate_totp_secret() -> str:
    """Called once at admin-account creation time. The resulting secret
    gets shown to the admin exactly once (same one-shot-reveal pattern as
    the R2 API secret) so they can scan it into an authenticator app --
    it is never re-displayed after that."""
    return pyotp.random_base32()


def totp_provisioning_uri(username: str, secret: str, issuer: str = "Wardence") -> str:
    """otpauth:// URI an authenticator app (Google Authenticator, Authy,
    etc.) can scan directly as a QR code to set up the account."""
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    # valid_window=1 accepts the current 30s slot plus the one immediately
    # before/after it -- real tolerance for clock drift/submission latency
    # between client and server, without meaningfully weakening TOTP.
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def verify_login(
    conn: sqlite3.Connection, username: str, password: str, ip: str | None = None
) -> dict:
    """
    Returns {"ok": True, "account": {...}} or {"ok": False, "reason": "..."}.
    Never raises on bad credentials -- callers map "reason" to the right
    HTTP status, and a generic-enough message is used at the API layer so
    a failed login doesn't reveal WHICH part (username vs password) was
    wrong.
    """
    if _is_locked_out(conn, username):
        return {"ok": False, "reason": "locked_out"}

    account = get_account(conn, username)
    if account is None:
        _record_attempt(conn, username, False, ip)
        return {"ok": False, "reason": "invalid_credentials"}

    if not account["active"]:
        _record_attempt(conn, username, False, ip)
        return {"ok": False, "reason": "account_inactive"}

    if is_expired(account):
        _record_attempt(conn, username, False, ip)
        return {"ok": False, "reason": "account_expired"}

    if not _check_password(password, account["password_hash"]):
        _record_attempt(conn, username, False, ip)
        return {"ok": False, "reason": "invalid_credentials"}

    _record_attempt(conn, username, True, ip)
    return {"ok": True, "account": account}
