"""
Phase H, three-dimension trust structure -- Dimensions B and C.

Dimension A (existing, unchanged) is trust_engine.py: per-class
can_act/report_only, driven by real action-outcome history.

Dimension B (this file): per-class diagnoser mode, stub/llm. Auto-
promotes to "llm" on 5 CONSECUTIVE correct background-comparison
diagnoses (evaluated continuously from the very first real episode --
no separate first-entry/re-entry rule). Auto-reverts to "stub" instantly
on any single wrong diagnosis.

Dimension C (this file): action trust, deterministic_fallback/llm_can_act
-- only meaningful when Dimension A=can_act AND Dimension B=llm for that
class. Earned via 5 consecutive episodes where BOTH diagnosis and
proposed action were correct. "Action correct" means:
  - exact parameter match for the three magnitude-sensitive actions
    (patch_memory_limit, patch_cpu_limit, scale_deployment) -- a
    validator-passing but absurd value (4Gi, 4000m, replicas=20) could
    genuinely destabilize this lab's thin real headroom while still
    earning streak credit under a looser bar.
  - tool-only match for the other three (restart_deployment,
    rollback_deployment, restore_from_disk_full) -- no magnitude
    parameter to abuse.
Revoked instantly on a single miss, same asymmetry as Dimension A.

This file is the STATE STORE only -- it records outcomes and exposes
current mode/streak. It is deliberately the only piece with write
access to these tables, mirroring trust_engine.py's own "only the
scorer, which is the only piece allowed to see ground truth, updates
this" discipline. Nothing here decides WHETHER an episode's outcome
was correct -- callers (p3_scorer.py) do that comparison against
ground truth and pass the boolean in.

The 150-episode floor that used to gate this (p2_readonly_loop/
trust_gate.py's REAL_LLM_EPISODE_FLOOR) is DROPPED -- see
wardence_context.md's Model Strategy section, "THE 150-EPISODE FLOOR
IS DROPPED" for the full reasoning. trust_gate.py itself still needs a
rewrite (step 2) to stop referencing it; this file's tables are what
that rewrite will read from instead.

Uses the same wardence.db as every other P2/P3 module (native WSL2
filesystem path, not /mnt/c -- see p2_readonly_loop/scorer.py).
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

STUB = "stub"
LLM = "llm"

DETERMINISTIC_FALLBACK = "deterministic_fallback"
LLM_CAN_ACT = "llm_can_act"

# 5-consecutive-correct for both dimensions, per the locked design --
# same N as trust_engine.py's own PROMOTION_STREAK, not a coincidence,
# just never made a shared constant since the two systems are
# independent by design (Dimension A's N could change without touching
# B/C, and vice versa).
DIAGNOSER_PROMOTION_STREAK = 5
ACTION_PROMOTION_STREAK = 5

# The three magnitude-sensitive actions (Kimi review 13): exact
# parameter match required, not just tool-name match, before an
# action's outcome counts toward the Dimension C streak.
MAGNITUDE_SENSITIVE_ACTIONS = {"patch_memory_limit", "patch_cpu_limit", "scale_deployment"}


def ensure_llm_trust_tables(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diagnoser_mode (
            fault_class TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'stub',
            streak INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diagnoser_mode_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT,
            fault_class TEXT NOT NULL,
            correct INTEGER NOT NULL,
            mode_before TEXT NOT NULL,
            mode_after TEXT NOT NULL,
            streak_before INTEGER NOT NULL,
            streak_after INTEGER NOT NULL,
            recorded_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS action_trust (
            fault_class TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'deterministic_fallback',
            streak INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS action_trust_history (
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


def get_diagnoser_mode(conn: sqlite3.Connection, fault_class: str) -> dict:
    row = conn.execute(
        "SELECT mode, streak FROM diagnoser_mode WHERE fault_class = ?", (fault_class,)
    ).fetchone()
    if row is None:
        return {"fault_class": fault_class, "mode": STUB, "streak": 0}
    return {"fault_class": fault_class, "mode": row[0], "streak": row[1]}


def get_action_trust(conn: sqlite3.Connection, fault_class: str) -> dict:
    row = conn.execute(
        "SELECT state, streak FROM action_trust WHERE fault_class = ?", (fault_class,)
    ).fetchone()
    if row is None:
        return {"fault_class": fault_class, "state": DETERMINISTIC_FALLBACK, "streak": 0}
    return {"fault_class": fault_class, "state": row[0], "streak": row[1]}


def _upsert_diagnoser_mode(conn: sqlite3.Connection, fault_class: str, mode: str, streak: int):
    conn.execute(
        """
        INSERT INTO diagnoser_mode (fault_class, mode, streak, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(fault_class) DO UPDATE SET
            mode = excluded.mode,
            streak = excluded.streak,
            updated_at = excluded.updated_at
        """,
        (fault_class, mode, streak),
    )
    conn.commit()


def _upsert_action_trust(conn: sqlite3.Connection, fault_class: str, state: str, streak: int):
    conn.execute(
        """
        INSERT INTO action_trust (fault_class, state, streak, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(fault_class) DO UPDATE SET
            state = excluded.state,
            streak = excluded.streak,
            updated_at = excluded.updated_at
        """,
        (fault_class, state, streak),
    )
    conn.commit()


def manual_set_diagnoser_mode(conn: sqlite3.Connection, fault_class: str, mode: str, streak: int = 0):
    """Admin/manual override for Dimension B (e.g. testing the streak-5
    auto-promotion boundary without waiting for 5 real episodes) --
    bypasses the normal earn-it-via-record_diagnoser_outcome path. Same
    convention as trust_engine.manual_set_state: this function only
    applies the state change, the caller is responsible for logging why
    (see set_llm_trust_state.py, this project's one-off CLI for it)."""
    if mode not in (STUB, LLM):
        raise ValueError(f"invalid mode {mode!r}")
    _upsert_diagnoser_mode(conn, fault_class, mode, streak)


def manual_set_action_trust(conn: sqlite3.Connection, fault_class: str, state: str, streak: int = 0):
    """Admin/manual override for Dimension C -- same convention as
    manual_set_diagnoser_mode above."""
    if state not in (DETERMINISTIC_FALLBACK, LLM_CAN_ACT):
        raise ValueError(f"invalid state {state!r}")
    _upsert_action_trust(conn, fault_class, state, streak)


def reset_to_safe_defaults(conn: sqlite3.Connection, fault_class: str, episode_id: str | None = None):
    """
    Force both dimensions back to their safest state -- called by
    circuit_breaker.py on a global trip (step 5), same "a global safety
    event overrides everything, not just Dimension A" reasoning Kimi
    review 13 raised. Logs a history row for both even if already at
    the safe default, so the trip is auditable regardless of prior
    state.
    """
    diag_before = get_diagnoser_mode(conn, fault_class)
    if diag_before["mode"] != STUB or diag_before["streak"] != 0:
        _upsert_diagnoser_mode(conn, fault_class, STUB, 0)
    conn.execute(
        """
        INSERT INTO diagnoser_mode_history
            (episode_id, fault_class, correct, mode_before, mode_after, streak_before, streak_after)
        VALUES (?, ?, 0, ?, ?, ?, 0)
        """,
        (episode_id, fault_class, diag_before["mode"], STUB, diag_before["streak"]),
    )

    action_before = get_action_trust(conn, fault_class)
    if action_before["state"] != DETERMINISTIC_FALLBACK or action_before["streak"] != 0:
        _upsert_action_trust(conn, fault_class, DETERMINISTIC_FALLBACK, 0)
    conn.execute(
        """
        INSERT INTO action_trust_history
            (episode_id, fault_class, correct, state_before, state_after, streak_before, streak_after)
        VALUES (?, ?, 0, ?, ?, ?, 0)
        """,
        (episode_id, fault_class, action_before["state"], DETERMINISTIC_FALLBACK, action_before["streak"]),
    )
    conn.commit()


def record_diagnoser_outcome(
    conn: sqlite3.Connection, fault_class: str, correct: bool, episode_id: str | None = None
) -> dict:
    """
    Record one real background-comparison diagnosis outcome (LLM
    diagnosis vs. ground truth, checked by the scorer only) and update
    Dimension B accordingly. Auto-promote on streak, instant revert to
    stub on any miss -- no partial credit, same shape as trust_engine's
    Dimension A.
    """
    before = get_diagnoser_mode(conn, fault_class)
    mode_before, streak_before = before["mode"], before["streak"]

    if correct:
        streak_after = streak_before + 1
        mode_after = LLM if streak_after >= DIAGNOSER_PROMOTION_STREAK else mode_before
    else:
        streak_after = 0
        mode_after = STUB

    _upsert_diagnoser_mode(conn, fault_class, mode_after, streak_after)

    conn.execute(
        """
        INSERT INTO diagnoser_mode_history
            (episode_id, fault_class, correct, mode_before, mode_after, streak_before, streak_after)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (episode_id, fault_class, int(correct), mode_before, mode_after, streak_before, streak_after),
    )
    conn.commit()

    return {
        "fault_class": fault_class,
        "mode_before": mode_before, "mode_after": mode_after,
        "streak_before": streak_before, "streak_after": streak_after,
        "promoted": mode_before != LLM and mode_after == LLM,
        "reverted": mode_before == LLM and mode_after == STUB,
    }


def record_action_outcome(
    conn: sqlite3.Connection, fault_class: str, correct: bool, episode_id: str | None = None
) -> dict:
    """
    Record one real action-proposal outcome (diagnosis AND action both
    correct, per the exact-match/tool-only-match split the caller must
    have already applied -- see MAGNITUDE_SENSITIVE_ACTIONS) and update
    Dimension C. Only meaningful when Dimension A=can_act and Dimension
    B=llm for this class -- callers are responsible for only invoking
    this when that precondition holds; this function itself doesn't
    re-check A/B, it just records the C-specific outcome it's given.
    """
    before = get_action_trust(conn, fault_class)
    state_before, streak_before = before["state"], before["streak"]

    if correct:
        streak_after = streak_before + 1
        state_after = LLM_CAN_ACT if streak_after >= ACTION_PROMOTION_STREAK else state_before
    else:
        streak_after = 0
        state_after = DETERMINISTIC_FALLBACK

    _upsert_action_trust(conn, fault_class, state_after, streak_after)

    conn.execute(
        """
        INSERT INTO action_trust_history
            (episode_id, fault_class, correct, state_before, state_after, streak_before, streak_after)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (episode_id, fault_class, int(correct), state_before, state_after, streak_before, streak_after),
    )
    conn.commit()

    return {
        "fault_class": fault_class,
        "state_before": state_before, "state_after": state_after,
        "streak_before": streak_before, "streak_after": streak_after,
        "promoted": state_before != LLM_CAN_ACT and state_after == LLM_CAN_ACT,
        "revoked": state_before == LLM_CAN_ACT and state_after == DETERMINISTIC_FALLBACK,
    }


if __name__ == "__main__":
    # Quick real-data status check across the roster, no args needed --
    # mirrors trust_gate.py's own __main__ block.
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "p2_readonly_loop"))
    from react_agent import FAULT_CLASSES  # noqa: E402

    conn = sqlite3.connect(DB_PATH)
    ensure_llm_trust_tables(conn)
    print(f"{'class':35s} {'diagnoser':12s} {'streak':7s} {'action':22s} {'streak':7s}")
    for fc in FAULT_CLASSES:
        d = get_diagnoser_mode(conn, fc)
        a = get_action_trust(conn, fc)
        print(f"{fc:35s} {d['mode']:12s} {d['streak']:<7d} {a['state']:22s} {a['streak']:<7d}")
    conn.close()
