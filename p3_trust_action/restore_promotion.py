"""One-off: manually restore crash-loop to can_act/streak=5 after a
demotion that was caused by the pod-name-prefix verifier bug (now fixed
in verifier.py), not a genuine flap.
Run: python3 p3_trust_action/restore_promotion.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

conn = sqlite3.connect(DB_PATH)
conn.execute(
    """
    INSERT INTO trust_state (fault_class, state, streak, updated_at)
    VALUES ('crash-loop', 'can_act', 5, datetime('now'))
    ON CONFLICT(fault_class) DO UPDATE SET
        state = excluded.state, streak = excluded.streak, updated_at = excluded.updated_at
    """
)
conn.commit()
conn.close()
print("crash-loop restored to can_act, streak=5")
