"""
Cross-class misdispatch guard -- built 2026-07-31 after Kimi review 15
(one-off, same chat as 12-14, not archived as its own numbered file
before this) rejected the original fix (penalizing the WRONGLY-INVOKED
class's own Dimension A trust_state/streak whenever its real action
fired under a different actual fault than it was meant for).

Real reasoning for the rejection, verified before adopting: that
original fix conflated two genuinely distinct things -- "is this
class's own action mechanism reliable" (dry-run passed, patch applied
cleanly -- it worked exactly as designed) vs. "did the diagnostic layer
correctly attribute this fault to this class" (a completely separate
failure, already tracked by Dimension B's own per-class diagnoser-mode
revert). Penalizing the dispatched class's promotion streak for a
mistake that was never its own would corrupt what "N consecutive
correct" is supposed to measure -- a class frequently confused with
others (e.g. oom <-> under-provisioned-replicas, both target catalogue)
would get demoted despite a perfectly reliable action mechanism.

This module is the accepted alternative: track misdispatches
separately, NEVER touching trust_state's streak. If a class is
misdispatched into wrong contexts often enough in a short window, force
it to a temporary "safety hold" (report-only-equivalent, but WITHOUT
resetting its earned streak) until the pattern stops or an admin clears
it. This is a genuinely different signal from Dimension A's own
demotion: demotion means "this class was tested on its own real fault
and failed"; a safety hold means "this class keeps getting invoked for
faults that turn out not to be its own, so trusting its real dispatch
right now is unsafe regardless of how good its own action history is."

MISDISPATCH_HOLD_THRESHOLD/WINDOW, sized against real data, not
guessed: this project's own real overnight batch (2026-07-31, 136
episodes) produced exactly 1 real cross-class misdispatch -- a real
base rate of ~0.7%. At a similarly-sized ~136-episode day, the
EXPECTED misdispatch count at that real observed rate is <1 (~0.95).
Hitting 3 in a single 24h window would require the real rate to spike
roughly 3x above what's actually been observed -- a genuine, detectable
change in diagnostic reliability, not a plausible fluke at normal
volumes. Revisit if real data ever shows this threshold firing on
normal, non-anomalous days.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

MISDISPATCH_HOLD_THRESHOLD = 3
MISDISPATCH_HOLD_WINDOW_HOURS = 24


def ensure_misdispatch_tables(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS misdispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT,
            predicted_class TEXT NOT NULL,
            actual_class TEXT NOT NULL,
            recorded_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS safety_hold (
            fault_class TEXT PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            held_since TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def get_safety_hold(conn: sqlite3.Connection, fault_class: str) -> dict:
    row = conn.execute(
        "SELECT active, reason, held_since FROM safety_hold WHERE fault_class = ?", (fault_class,)
    ).fetchone()
    if row is None:
        return {"fault_class": fault_class, "active": False, "reason": None, "held_since": None}
    return {"fault_class": fault_class, "active": bool(row[0]), "reason": row[1], "held_since": row[2]}


def _set_safety_hold(conn: sqlite3.Connection, fault_class: str, active: bool, reason: str | None = None):
    # Plain read-then-write, not a CASE-based UPSERT -- matches this
    # project's simpler existing convention (llm_trust_state.py's own
    # _upsert_diagnoser_mode/_upsert_action_trust). held_since is only
    # ever refreshed on a fresh 0->1 transition, preserved across
    # repeated already-active writes, and cleared to NULL on release.
    existing = get_safety_hold(conn, fault_class)
    if active and existing["active"]:
        held_since = existing["held_since"]  # already held -- keep the original timestamp
    elif active:
        held_since = _now_str(conn)  # fresh 0 -> 1 transition -- starts now
    else:
        held_since = None  # released

    conn.execute(
        """
        INSERT INTO safety_hold (fault_class, active, reason, held_since, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(fault_class) DO UPDATE SET
            active = excluded.active,
            reason = excluded.reason,
            held_since = excluded.held_since,
            updated_at = excluded.updated_at
        """,
        (fault_class, int(active), reason, held_since),
    )
    conn.commit()


def _now_str(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT datetime('now')").fetchone()[0]


def clear_safety_hold(conn: sqlite3.Connection, fault_class: str, reason: str = "manually cleared"):
    """Admin/manual clear -- e.g. once the diagnostic confusion causing
    the holds has itself been fixed. Real accountability rule: does NOT
    touch trust_state's own streak, since a safety hold was never a
    demotion in the first place."""
    _set_safety_hold(conn, fault_class, False, reason)


def clear_all_safety_holds(conn: sqlite3.Connection, reason: str = "circuit breaker global trip"):
    """Called by circuit_breaker.py alongside its existing Dimension A/B/C
    resets on a global trip -- a safety hold is already a form of forced
    conservatism, so once the bigger reset has already forced every
    can_act class back to report_only, a stale hold sitting around with
    no corresponding live reason would just be confusing. Mirrors
    llm_trust_state.reset_to_safe_defaults's own "reset to safest state
    on a global event" pattern."""
    for row in conn.execute("SELECT fault_class FROM safety_hold WHERE active = 1").fetchall():
        _set_safety_hold(conn, row[0], False, reason)


def record_misdispatch(conn: sqlite3.Connection, episode_id: str, predicted_class: str, actual_class: str) -> dict:
    """
    Logs a real cross-class misdispatch (a real action fired under
    predicted_class while the true fault was actual_class) and checks
    whether the recent-count threshold trips a safety hold. NEVER calls
    trust_engine.record_outcome or touches trust_state -- that's the
    whole point of this being a separate mechanism from Dimension A's
    own demotion path.
    """
    conn.execute(
        "INSERT INTO misdispatch_log (episode_id, predicted_class, actual_class) VALUES (?, ?, ?)",
        (episode_id, predicted_class, actual_class),
    )
    conn.commit()

    recent_count = conn.execute(
        f"""
        SELECT COUNT(*) FROM misdispatch_log
        WHERE predicted_class = ? AND recorded_at >= datetime('now', '-{MISDISPATCH_HOLD_WINDOW_HOURS} hours')
        """,
        (predicted_class,),
    ).fetchone()[0]

    hold_triggered = False
    if recent_count >= MISDISPATCH_HOLD_THRESHOLD:
        existing = get_safety_hold(conn, predicted_class)
        if not existing["active"]:
            hold_triggered = True
        _set_safety_hold(
            conn, predicted_class, True,
            reason=f"{recent_count} cross-class misdispatches in the last {MISDISPATCH_HOLD_WINDOW_HOURS}h",
        )

    return {
        "predicted_class": predicted_class, "actual_class": actual_class,
        "recent_misdispatch_count": recent_count, "hold_triggered": hold_triggered,
    }
