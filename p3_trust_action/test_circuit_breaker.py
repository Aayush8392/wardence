"""One-off script: promote a throwaway class, then trip the circuit
breaker and confirm it forces demotion independent of that class's streak.
Run: python3 p3_trust_action/test_circuit_breaker.py

FIXED 2026-07-31, real bug found the hard way: check_circuit_breaker() is
deliberately GLOBAL (that's the whole point of a circuit breaker) -- it
demotes EVERY can_act class and resets EVERY class's Dimension B/C, not
just TEST_CLASS. Running this test against the real DB wiped real, earned
trust state (crash-loop streak 7, oom/disk-full 5, cpu-throttling 23,
bad-rollout 29, under-provisioned-replicas 11, all real can_act -> wiped
to report_only/0; crash-loop's real Dimension B streak also wiped 1 -> 0).
Manually restored via restore_after_cb_test_bug.py (one-off, already run).
Fixed properly here: snapshot every real class's Dimension A/B/C state
before tripping the breaker, restore it in a finally block regardless of
how the test ends -- so this can never silently damage real state again,
on this run or any future one.
"""

import sqlite3
import sys

sys.path.insert(0, "p3_trust_action")

from circuit_breaker import (  # noqa: E402
    ensure_circuit_breaker_table,
    record_failure,
)
from trust_engine import (  # noqa: E402
    CAN_ACT,
    DB_PATH,
    PROMOTION_STREAK,
    REPORT_ONLY,
    ensure_trust_tables,
    get_trust_state,
    manual_set_state,
    record_outcome,
)
from llm_trust_state import (  # noqa: E402
    ensure_llm_trust_tables,
    get_action_trust,
    get_diagnoser_mode,
)

TEST_CLASS = "test-fault-class-cb"
PROMOTION_STREAK[TEST_CLASS] = 5

conn = sqlite3.connect(DB_PATH)
ensure_trust_tables(conn)
ensure_circuit_breaker_table(conn)
ensure_llm_trust_tables(conn)

conn.execute("DELETE FROM trust_state WHERE fault_class = ?", (TEST_CLASS,))
conn.execute("DELETE FROM trust_history WHERE fault_class = ?", (TEST_CLASS,))
conn.execute("DELETE FROM failure_log WHERE fault_class = ? OR fault_class IS NULL", (TEST_CLASS,))
conn.commit()

# Snapshot every REAL class's Dimension A/B/C state before the breaker
# trips -- the trip is global by design and will touch all of them.
# Real classes only (never TEST_CLASS -- that one's meant to change).
real_classes = [fc for fc in PROMOTION_STREAK if fc != TEST_CLASS]
snapshot_a = {fc: get_trust_state(conn, fc) for fc in real_classes}
snapshot_b = {fc: get_diagnoser_mode(conn, fc) for fc in real_classes}
snapshot_c = {fc: get_action_trust(conn, fc) for fc in real_classes}


def _restore_snapshots():
    for fc, s in snapshot_a.items():
        manual_set_state(conn, fc, s["state"], s["streak"])
    for fc, s in snapshot_b.items():
        conn.execute(
            """
            INSERT INTO diagnoser_mode (fault_class, mode, streak, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(fault_class) DO UPDATE SET
                mode = excluded.mode, streak = excluded.streak, updated_at = excluded.updated_at
            """,
            (fc, s["mode"], s["streak"]),
        )
    for fc, s in snapshot_c.items():
        conn.execute(
            """
            INSERT INTO action_trust (fault_class, state, streak, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(fault_class) DO UPDATE SET
                state = excluded.state, streak = excluded.streak, updated_at = excluded.updated_at
            """,
            (fc, s["state"], s["streak"]),
        )
    conn.commit()
    print(f"\n-- restored {len(real_classes)} real classes' Dimension A/B/C state to their pre-test snapshot --")


try:
    print("-- promoting test class to Can-Act --")
    for _ in range(5):
        record_outcome(conn, TEST_CLASS, correct=True)

    state = get_trust_state(conn, TEST_CLASS)
    print(state)
    assert state["state"] == CAN_ACT

    print("\n-- recording 3 failures, checking breaker after each --")
    result = None
    for i in range(3):
        result = record_failure(conn, reason="test failure", fault_class=TEST_CLASS)
        print(i + 1, result)

    final_state = get_trust_state(conn, TEST_CLASS)
    print("\nfinal state:", final_state)
    assert final_state["state"] == REPORT_ONLY
    assert TEST_CLASS in result["demoted_classes"]
    assert result["tripped"] is True

    print("\nALL ASSERTIONS PASSED")
finally:
    # Always restore real state, whether the test passed, failed, or
    # raised -- this is the fix, not just a cleanup nicety.
    _restore_snapshots()
    conn.execute("DELETE FROM trust_state WHERE fault_class = ?", (TEST_CLASS,))
    conn.execute("DELETE FROM trust_history WHERE fault_class = ?", (TEST_CLASS,))
    conn.execute("DELETE FROM failure_log WHERE fault_class = ? OR fault_class IS NULL", (TEST_CLASS,))
    conn.commit()
    conn.close()
