"""
One-off: find the scores row(s) for a given fault_class closest to a given
timestamp -- needed when trust_history's episode_id is NULL for a
transition (happens for some demotion paths) so inspect_episode.py has
nothing to key off of directly.

Run: python3 find_episode_near_time.py <fault_class> "<YYYY-MM-DD HH:MM:SS>"
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

fault_class = sys.argv[1]
ts = sys.argv[2]

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT s.episode_id, s.actual_class, s.correct, s.action_taken, s.action_applied,
           s.durability_verdict, s.scored_at,
           ABS(strftime('%s', s.scored_at) - strftime('%s', ?)) AS diff_s
    FROM scores s
    WHERE s.actual_class = ?
    ORDER BY diff_s ASC
    LIMIT 5
    """,
    (ts, fault_class),
).fetchall()
conn.close()

for r in rows:
    print(dict(r))
