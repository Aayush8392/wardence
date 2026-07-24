"""
P3 trust state-machine. Per fault class: Report-Only -> Can-Act -> Demoted.

Promotion: N consecutive correct outcomes (N=5 for the 3 easy auto-fix
classes -- crash-loop, OOM, disk-full -- per the locked fault taxonomy).
Demotion: any wrong outcome while Can-Act -- immediate, streak resets to
zero, class must re-earn the full streak from scratch (no partial credit).

Uses the same wardence.db as P2 (native WSL2 filesystem -- see
p2_readonly_loop/scorer.py for why /mnt/c paths are avoided for SQLite).
State lives entirely in SQLite, so it survives restarts with no separate
hydration step -- every read goes straight to the DB.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

PROMOTION_STREAK = {
    "crash-loop": 5,
    "oom": 5,
    "disk-full": 5,
    "cpu-throttling": 5,
    "under-provisioned-replicas": 5,
    "bad-rollout": 5,
}

REPORT_ONLY = "report_only"
CAN_ACT = "can_act"
DEMOTED = "demoted"


def ensure_trust_tables(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trust_state (
            fault_class TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            streak INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trust_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT,
            fault_class TEXT NOT NULL,
            correct INTEGER NOT NULL,
            state_before TEXT NOT NULL,
            state_after TEXT NOT NULL,
            streak_before INTEGER NOT NULL,
            streak_after INTEGER NOT NULL,
            recorded_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def get_trust_state(conn: sqlite3.Connection, fault_class: str) -> dict:
    row = conn.execute(
        "SELECT state, streak FROM trust_state WHERE fault_class = ?", (fault_class,)
    ).fetchone()
    if row is None:
        return {"fault_class": fault_class, "state": REPORT_ONLY, "streak": 0}
    return {"fault_class": fault_class, "state": row[0], "streak": row[1]}


def _upsert_trust_state(conn: sqlite3.Connection, fault_class: str, state: str, streak: int):
    conn.execute(
        """
        INSERT INTO trust_state (fault_class, state, streak, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(fault_class) DO UPDATE SET
            state = excluded.state,
            streak = excluded.streak,
            updated_at = excluded.updated_at
        """,
        (fault_class, state, streak),
    )
    conn.commit()


def manual_set_state(conn: sqlite3.Connection, fault_class: str, state: str, streak: int = 0):
    """Admin override (Operator API /promote, /demote) -- bypasses the
    normal earn-it-via-outcomes path. Caller (operator_api.py) is
    responsible for audit-logging who did this and why; this function
    only applies the state change."""
    if state not in (REPORT_ONLY, CAN_ACT, DEMOTED):
        raise ValueError(f"invalid state '{state}'")
    _upsert_trust_state(conn, fault_class, state, streak)


def record_outcome(
    conn: sqlite3.Connection, fault_class: str, correct: bool, episode_id: str | None = None
) -> dict:
    """
    Record one verified outcome (diagnosis + fix, post-durability-window)
    and update the trust state accordingly. Returns the before/after state
    so callers can log or display the transition.
    """
    if fault_class not in PROMOTION_STREAK:
        raise ValueError(f"'{fault_class}' has no promotion policy -- report-only class")

    before = get_trust_state(conn, fault_class)
    state_before, streak_before = before["state"], before["streak"]

    if state_before == CAN_ACT and not correct:
        # Demotion: instant, full streak reset.
        state_after, streak_after = REPORT_ONLY, 0
    elif correct:
        streak_after = streak_before + 1
        target_n = PROMOTION_STREAK[fault_class]
        state_after = CAN_ACT if streak_after >= target_n else state_before
    else:
        # Wrong while already Report-Only: no promotion progress, no
        # further penalty (can't demote from a state with no trust yet).
        state_after, streak_after = state_before, 0

    _upsert_trust_state(conn, fault_class, state_after, streak_after)

    conn.execute(
        """
        INSERT INTO trust_history
            (episode_id, fault_class, correct, state_before, state_after, streak_before, streak_after)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (episode_id, fault_class, int(correct), state_before, state_after, streak_before, streak_after),
    )
    conn.commit()

    return {
        "fault_class": fault_class,
        "state_before": state_before,
        "state_after": state_after,
        "streak_before": streak_before,
        "streak_after": streak_after,
        "promoted": state_before != CAN_ACT and state_after == CAN_ACT,
        "demoted": state_before == CAN_ACT and state_after == REPORT_ONLY,
    }
