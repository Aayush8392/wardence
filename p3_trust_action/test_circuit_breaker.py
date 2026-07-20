"""One-off script: promote a throwaway class, then trip the circuit
breaker and confirm it forces demotion independent of that class's streak.
Run: python3 p3_trust_action/test_circuit_breaker.py
"""

import sqlite3
import sys

sys.path.insert(0, "p3_trust_action")

from circuit_breaker import (  # noqa: E402
    check_circuit_breaker,
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
    record_outcome,
)

TEST_CLASS = "test-fault-class-cb"
PROMOTION_STREAK[TEST_CLASS] = 5

conn = sqlite3.connect(DB_PATH)
ensure_trust_tables(conn)
ensure_circuit_breaker_table(conn)

conn.execute("DELETE FROM trust_state WHERE fault_class = ?", (TEST_CLASS,))
conn.execute("DELETE FROM trust_history WHERE fault_class = ?", (TEST_CLASS,))
conn.execute("DELETE FROM failure_log WHERE fault_class = ? OR fault_class IS NULL", (TEST_CLASS,))
conn.commit()

print("-- promoting test class to Can-Act --")
for _ in range(5):
    record_outcome(conn, TEST_CLASS, correct=True)

state = get_trust_state(conn, TEST_CLASS)
print(state)
assert state["state"] == CAN_ACT

print("\n-- recording 3 failures, checking breaker after each --")
for i in range(3):
    record_failure(conn, reason="test failure", fault_class=TEST_CLASS)
    result = check_circuit_breaker(conn)
    print(i + 1, result)

final_state = get_trust_state(conn, TEST_CLASS)
print("\nfinal state:", final_state)
assert final_state["state"] == REPORT_ONLY
assert TEST_CLASS in result["demoted_classes"]
assert result["tripped"] is True

print("\nALL ASSERTIONS PASSED")
conn.close()
