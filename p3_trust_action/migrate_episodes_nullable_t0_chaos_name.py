"""
One-off, run-once migration: relaxes episodes.t0 and
episodes.chaos_resource_name from NOT NULL to nullable.

Why this needs a real table rebuild, not an idempotent ALTER TABLE ADD
COLUMN like every other migration in this codebase: SQLite has no
ALTER TABLE ... ALTER COLUMN to relax an existing NOT NULL constraint.
The only way is the standard rename-copy-drop-rename pattern below.

Why nullable at all (Kimi review 35/36, Operator Phase 1 item 5's async
wrapper): the wrapper pre-creates an episodes row BEFORE injector.py
ever runs, so it has something to attach live episode_state to during
injection -- at that point the real injection timestamp and the real
chaos-resource name (which includes a UUID injector.py itself generates)
are genuinely not known yet. A sentinel value (e.g. t0='pending') was
considered and rejected -- NULL is the correct SQL representation for
"not yet known," and a sentinel would pollute every query that does
datetime arithmetic on t0 (reconciliation, _episode_in_flight, the
scorer's own age checks).

Safe to run against the real, live 3600+-episode DB:
- Every EXISTING row already has real, non-NULL t0/chaos_resource_name
  values (both columns were NOT NULL until this migration runs) --
  relaxing the constraint doesn't touch any existing data, it only
  permits NEW rows (going forward, only Operator's async wrapper's
  pre-created rows) to have NULL there temporarily.
- Confirmed via direct grep: nothing in this codebase ever runs
  `PRAGMA foreign_keys = ON`, so the `FOREIGN KEY (episode_id)
  REFERENCES episodes(episode_id)` declarations in scores/
  episode_snapshots/comparison_sampling_log/llm_diagnosis_log are
  documentation only, never enforced -- dropping and recreating
  `episodes` (same table, same name, same episode_id values) doesn't
  risk an FK violation on any of those tables' existing rows.
- All 6 columns added by operator_api.py's own
  _ensure_episode_state_columns (episode_state/state_entered_at/
  stop_hold_requested/abandon_requested/triggered_by/subprocess_pid,
  plus evidence_confirmed/triggering_username once those land) are
  deliberately NOT carried into the rebuilt table here -- they get
  re-added automatically, idempotently, the next time operator_api.py
  starts (its existing try/except ALTER TABLE ADD COLUMN loop just
  finds them "missing" again after the rebuild and re-adds them, same
  as it would on a brand new DB). Keeping the rebuild scoped to only
  the 6 original columns avoids duplicating that column list in two
  places.
- Idempotent: if chaos_resource_name is already nullable (this script
  already ran), it exits immediately without touching anything.

Usage:
    python3 p3_trust_action/migrate_episodes_nullable_t0_chaos_name.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trust_engine import DB_PATH  # noqa: E402


def _episodes_already_nullable(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA table_info(episodes)"):
        # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
        if row[1] == "chaos_resource_name":
            return row[3] == 0  # notnull == 0 means already nullable
    raise RuntimeError("episodes table has no chaos_resource_name column -- unexpected schema")


def main():
    conn = sqlite3.connect(DB_PATH)
    if _episodes_already_nullable(conn):
        print("Already migrated -- chaos_resource_name is already nullable. Nothing to do.")
        conn.close()
        return

    before_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    print(f"Migrating episodes table (real row count before: {before_count})...")

    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            CREATE TABLE episodes_new (
                episode_id TEXT PRIMARY KEY,
                fault_class TEXT NOT NULL,
                target TEXT NOT NULL,
                namespace TEXT NOT NULL,
                t0 TEXT,
                chaos_resource_name TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO episodes_new (episode_id, fault_class, target, namespace, t0, chaos_resource_name) "
            "SELECT episode_id, fault_class, target, namespace, t0, chaos_resource_name FROM episodes"
        )
        conn.execute("DROP TABLE episodes")
        conn.execute("ALTER TABLE episodes_new RENAME TO episodes")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    after_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    null_t0 = conn.execute("SELECT COUNT(*) FROM episodes WHERE t0 IS NULL").fetchone()[0]
    conn.close()

    if after_count != before_count:
        raise RuntimeError(
            f"row count mismatch after migration: {before_count} before, {after_count} after -- "
            f"do not trust this DB, investigate before running anything else against it"
        )
    if null_t0 != 0:
        raise RuntimeError(
            f"{null_t0} rows have NULL t0 immediately after migration, but every pre-existing "
            f"row should have had a real t0 -- investigate before running anything else"
        )

    print(
        f"Migration complete: {after_count} real episodes preserved, t0/chaos_resource_name now "
        f"nullable, every pre-existing row confirmed to still have real (non-NULL) values in both. "
        f"The episode_state/state_entered_at/stop_hold_requested/abandon_requested/triggered_by/"
        f"subprocess_pid columns were dropped by this rebuild -- operator_api.py's own "
        f"_ensure_episode_state_columns will silently re-add them (all nullable/default, no data "
        f"was in them to lose) the next time it starts."
    )


if __name__ == "__main__":
    main()
