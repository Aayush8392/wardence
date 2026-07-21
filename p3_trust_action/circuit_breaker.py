"""
P3 circuit breaker: a global safety net independent of per-class trust
state. 3 failures within 5 minutes -> force EVERY currently Can-Act
class back to Report-Only, regardless of that class's own streak.

A "failure" here is caller-defined -- an action that errored (dry-run
or apply failed) or a verifier that reported "flapped." Step 6 (wiring
the agent to actually act) is what calls record_failure(); this module
only tracks and trips the breaker.

Uses the same wardence.db as the rest of P3.
"""

import sqlite3
from pathlib import Path

from trust_engine import CAN_ACT, REPORT_ONLY, get_trust_state

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

FAILURE_WINDOW_S = 5 * 60
FAILURE_THRESHOLD = 3


def ensure_circuit_breaker_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS failure_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reason TEXT NOT NULL,
            fault_class TEXT,
            recorded_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def record_failure(conn: sqlite3.Connection, reason: str, fault_class: str | None = None) -> dict:
    """
    Records the failure AND checks/trips the breaker in the same call.
    Originally these were two separate calls (record here, check later
    in the caller) -- if the caller crashed or raised anything in
    between, the failure was logged but the breaker never got checked
    for it, silently delaying a trip until some unrelated later
    episode's scorer run happened to check again. Collapsing them here
    removes that gap entirely.
    """
    conn.execute(
        "INSERT INTO failure_log (reason, fault_class) VALUES (?, ?)", (reason, fault_class)
    )
    conn.commit()
    return check_circuit_breaker(conn)


def _recent_failure_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM failure_log
        WHERE recorded_at >= datetime('now', '-{FAILURE_WINDOW_S} seconds')
        """
    ).fetchone()
    return row[0]


def _all_fault_classes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT fault_class FROM trust_state").fetchall()
    return [r[0] for r in rows]


def check_circuit_breaker(conn: sqlite3.Connection) -> dict:
    """
    Call after every failure is recorded. If the threshold is hit,
    trips immediately: every Can-Act class is forced to Report-Only.
    Returns {"tripped": bool, "recent_failures": int, "demoted_classes": [...]}.
    """
    recent = _recent_failure_count(conn)

    if recent < FAILURE_THRESHOLD:
        return {"tripped": False, "recent_failures": recent, "demoted_classes": []}

    demoted = []
    for fault_class in _all_fault_classes(conn):
        state = get_trust_state(conn, fault_class)
        if state["state"] == CAN_ACT:
            conn.execute(
                "UPDATE trust_state SET state = ?, streak = 0, updated_at = datetime('now') "
                "WHERE fault_class = ?",
                (REPORT_ONLY, fault_class),
            )
            conn.execute(
                """
                INSERT INTO trust_history
                    (fault_class, correct, state_before, state_after, streak_before, streak_after)
                VALUES (?, 0, ?, ?, ?, 0)
                """,
                (fault_class, CAN_ACT, REPORT_ONLY, state["streak"]),
            )
            demoted.append(fault_class)

    conn.commit()
    return {"tripped": True, "recent_failures": recent, "demoted_classes": demoted}
