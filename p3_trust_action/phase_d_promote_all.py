"""
One-off: force-promotes all 6 real auto-fix classes to can_act/streak=5,
run BEFORE phase_d_run.py.

Why: 3 of the 6 classes (cpu-throttling, under-provisioned-replicas,
bad-rollout) are currently sitting at report_only/streak=0 -- each
demoted DELIBERATELY by its own Phase 4 forced-demotion test
(2026-07-24/25), left as-is on purpose at the time ("re-earn naturally").
If left that way for Phase D, those classes' episodes would only ever
exercise diagnosis matching, never the real fix action or durability
check -- but Phase D's whole point is catching post-fix STATE POLLUTION
between classes (e.g. does under-provisioned-replicas' real scale-up
leave catalogue in a state that corrupts oom's next diagnosis), which is
invisible unless the real action actually runs.

Same manual-override mechanism already used for oom/disk-full's known-
bogus demotions (see restore_promotion.py) -- and Phase E's already-
planned full data cleanup/regeneration pass means nothing real is being
protected by leaving these three un-promoted for this one test run.

Run: python3 phase_d_promote_all.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

REAL_CLASSES = [
    "crash-loop", "oom", "disk-full",
    "cpu-throttling", "under-provisioned-replicas", "bad-rollout",
]
STREAK = 5  # PROMOTION_STREAK is 5 for all 6, per trust_engine.py


def main():
    conn = sqlite3.connect(DB_PATH)
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
    print("--- Promoting all 6 auto-fix classes to can_act for Phase D ---")
    for fc in REAL_CLASSES:
        before = conn.execute(
            "SELECT state, streak FROM trust_state WHERE fault_class = ?", (fc,)
        ).fetchone()
        before_str = f"{before[0]}/streak={before[1]}" if before else "report_only/streak=0 (no row yet)"

        conn.execute(
            """
            INSERT INTO trust_state (fault_class, state, streak, updated_at)
            VALUES (?, 'can_act', ?, datetime('now'))
            ON CONFLICT(fault_class) DO UPDATE SET
                state = excluded.state, streak = excluded.streak, updated_at = excluded.updated_at
            """,
            (fc, STREAK),
        )
        conn.commit()
        print(f"{fc}: {before_str} -> can_act/streak={STREAK}")

    conn.close()
    print("\nDone. All 6 classes are now can_act -- Phase D will exercise real fixes for every class.")


if __name__ == "__main__":
    main()
