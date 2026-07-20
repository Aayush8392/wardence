"""One-off: reset crash-loop's trust state (used after the scorer crash
mid-run left an orphaned streak increment for an episode that never
finished scoring).
Run: python3 p3_trust_action/reset_test_state.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

conn = sqlite3.connect(DB_PATH)
conn.execute("DELETE FROM trust_state WHERE fault_class = 'crash-loop'")
conn.execute("DELETE FROM trust_history WHERE fault_class = 'crash-loop'")
conn.commit()
conn.close()
print("crash-loop trust state reset.")
