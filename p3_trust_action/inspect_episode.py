"""One-off: print full scores row for a given episode_id.
Run: python3 p3_trust_action/inspect_episode.py <episode_id>
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

episode_id = sys.argv[1]
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT * FROM scores WHERE episode_id = ?", (episode_id,)).fetchone()
conn.close()

if row is None:
    print("no such episode")
else:
    for k in row.keys():
        print(f"{k}: {row[k]}")
