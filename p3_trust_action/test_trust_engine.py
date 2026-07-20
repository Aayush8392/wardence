"""One-off script: simulate outcomes to confirm promotion at streak=5
and instant demotion on a wrong outcome. Uses a throwaway fault class
name so it never touches real crash-loop/oom/disk-full state.
Run: python3 p3_trust_action/test_trust_engine.py
"""

import sqlite3
import sys

sys.path.insert(0, "p3_trust_action")

from trust_engine import (  # noqa: E402
    CAN_ACT,
    DB_PATH,
    PROMOTION_STREAK,
    REPORT_ONLY,
    ensure_trust_tables,
    record_outcome,
)

TEST_CLASS = "test-fault-class"
PROMOTION_STREAK[TEST_CLASS] = 5

conn = sqlite3.connect(DB_PATH)
ensure_trust_tables(conn)

# Clean slate for the test class.
conn.execute("DELETE FROM trust_state WHERE fault_class = ?", (TEST_CLASS,))
conn.execute("DELETE FROM trust_history WHERE fault_class = ?", (TEST_CLASS,))
conn.commit()

print("-- 5 consecutive correct outcomes (expect promotion on the 5th) --")
for i in range(5):
    result = record_outcome(conn, TEST_CLASS, correct=True)
    print(i + 1, result)
    assert result["state_after"] == (CAN_ACT if i == 4 else REPORT_ONLY)

print("\n-- 1 wrong outcome while Can-Act (expect instant demotion) --")
result = record_outcome(conn, TEST_CLASS, correct=False)
print(result)
assert result["demoted"] is True
assert result["state_after"] == REPORT_ONLY
assert result["streak_after"] == 0

print("\nALL ASSERTIONS PASSED")
conn.close()
