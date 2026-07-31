"""One-off sanity check for misdispatch_guard.py -- plain asserts, no
pytest, matches this project's existing test_*.py convention. Uses a
throwaway fault_class so it never touches real data."""
import sqlite3

from misdispatch_guard import (
    DB_PATH, clear_safety_hold, ensure_misdispatch_tables,
    get_safety_hold, record_misdispatch,
)

TEST_CLASS = "test-misdispatch-class"

conn = sqlite3.connect(DB_PATH)
ensure_misdispatch_tables(conn)

conn.execute("DELETE FROM misdispatch_log WHERE predicted_class = ?", (TEST_CLASS,))
conn.execute("DELETE FROM safety_hold WHERE fault_class = ?", (TEST_CLASS,))
conn.commit()

before = get_safety_hold(conn, TEST_CLASS)
print("before:", before)
assert before["active"] is False

r1 = record_misdispatch(conn, "ep1", TEST_CLASS, "other-class")
r2 = record_misdispatch(conn, "ep2", TEST_CLASS, "other-class")
print("r1:", r1)
print("r2:", r2)
assert r1["hold_triggered"] is False
assert r2["hold_triggered"] is False

mid = get_safety_hold(conn, TEST_CLASS)
print("after 2 (below threshold):", mid)
assert mid["active"] is False

r3 = record_misdispatch(conn, "ep3", TEST_CLASS, "other-class")
print("r3:", r3)
assert r3["hold_triggered"] is True
assert r3["recent_misdispatch_count"] == 3

held = get_safety_hold(conn, TEST_CLASS)
print("after 3 (threshold hit):", held)
assert held["active"] is True
assert held["held_since"] is not None

clear_safety_hold(conn, TEST_CLASS)
cleared = get_safety_hold(conn, TEST_CLASS)
print("after manual clear:", cleared)
assert cleared["active"] is False
assert cleared["held_since"] is None

conn.execute("DELETE FROM misdispatch_log WHERE predicted_class = ?", (TEST_CLASS,))
conn.execute("DELETE FROM safety_hold WHERE fault_class = ?", (TEST_CLASS,))
conn.commit()
conn.close()

print("\nALL ASSERTIONS PASSED")
