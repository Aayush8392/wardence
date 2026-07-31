"""
P3 circuit breaker: a global safety net independent of per-class trust
state. 3 failures within 5 minutes -> force EVERY currently Can-Act
class back to Report-Only, regardless of that class's own streak.

A "failure" here is caller-defined -- an action that errored (dry-run
or apply failed) or a verifier that reported "flapped." p3_scorer.py's
record_failure() calls (Dimension A actions) are what actually trip
this; this module only tracks and trips the breaker.

Extended 2026-07-31 (Kimi review 13, real gap found): a trip used to
only reset Dimension A (can_act -> report_only). It now ALSO forces
EVERY class's Dimension B/C back to their safest defaults (stub /
deterministic_fallback), not just the ones currently can_act -- a
global blast-radius event is exactly the moment a class should NOT be
left in LLM-autonomous diagnosis/action mode, regardless of whether
that specific class was the one that tripped the breaker. Real
reasoning: the breaker exists to say "something is wrong enough that
we don't trust automated behavior right now" -- limiting that
statement to Dimension A alone while leaving B/C running unaffected
would be inconsistent with what a "global" safety trip is supposed to
mean.

Uses the same wardence.db as the rest of P3.
"""

import sqlite3
import sys
from pathlib import Path

from trust_engine import CAN_ACT, REPORT_ONLY, get_trust_state

sys.path.insert(0, str(Path(__file__).parent.parent / "p2_readonly_loop"))

from react_agent import FAULT_CLASSES  # noqa: E402
from llm_trust_state import reset_to_safe_defaults  # noqa: E402
from misdispatch_guard import clear_all_safety_holds, ensure_misdispatch_tables  # noqa: E402

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
    trips immediately: every Can-Act class is forced to Report-Only
    (Dimension A), AND every class's Dimension B/C is forced back to
    stub/deterministic_fallback, regardless of current state -- a
    global trip resets all three dimensions, not just A.
    Returns {"tripped": bool, "recent_failures": int, "demoted_classes": [...],
    "llm_trust_reset_classes": [...]}.
    """
    recent = _recent_failure_count(conn)

    if recent < FAILURE_THRESHOLD:
        return {"tripped": False, "recent_failures": recent, "demoted_classes": [], "llm_trust_reset_classes": []}

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

    # Dimension B/C reset -- ALL fault classes (the full real taxonomy,
    # not just the ones with a Dimension A policy), since Dimension B
    # applies to every class, auto-fix or report-only. reset_to_safe_defaults
    # is idempotent -- a class already at stub/deterministic_fallback
    # still gets a (no-op-valued) history row, so the trip is auditable
    # against the full roster regardless of prior state.
    llm_trust_reset = list(FAULT_CLASSES)
    for fault_class in llm_trust_reset:
        reset_to_safe_defaults(conn, fault_class)

    # Misdispatch safety holds (added 2026-07-31, see misdispatch_guard.py)
    # are already a form of forced conservatism -- once the bigger reset
    # above has already forced every can_act class back to report_only,
    # a stale hold with no corresponding live reason would just be
    # confusing. Cleared, not left dangling.
    ensure_misdispatch_tables(conn)
    clear_all_safety_holds(conn)

    conn.commit()
    return {
        "tripped": True, "recent_failures": recent,
        "demoted_classes": demoted, "llm_trust_reset_classes": llm_trust_reset,
    }
