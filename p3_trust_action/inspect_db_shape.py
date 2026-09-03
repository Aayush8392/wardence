#!/usr/bin/env python3
"""
One-off: dump a wardence.db's shape for a WSL-vs-Oracle comparison before
migrating. Read-only. No secrets printed (accounts: username/role/expiry/
active only, never the password hash or TOTP secret).

Run on BOTH hosts:
    python3 inspect_db.py ~/wardence_p2_data/wardence.db
"""
import hashlib
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "wardence_p2_data" / "wardence.db")).expanduser()
print(f"# {db}  (exists={db.exists()}, size={db.stat().st_size if db.exists() else 0} bytes)")
if not db.exists():
    sys.exit(1)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
).fetchall()

schema_blob = "\n".join((r["sql"] or "") for r in rows)
print(f"# schema fingerprint (sha256): {hashlib.sha256(schema_blob.encode()).hexdigest()[:16]}")
print(f"# user_version: {conn.execute('PRAGMA user_version').fetchone()[0]}")
print()

print("## tables (row count)")
for r in rows:
    if r["type"] != "table":
        continue
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM \"{r['name']}\"").fetchone()[0]
    except sqlite3.Error as e:
        n = f"ERR {e}"
    cols = [c["name"] for c in conn.execute(f"PRAGMA table_info(\"{r['name']}\")").fetchall()]
    print(f"  {r['name']:<34} {str(n):>8}   cols: {', '.join(cols)}")

print()
print("## indexes")
for r in rows:
    if r["type"] == "index" and r["sql"]:
        print(f"  {r['name']}")

# --- episode-ish counts that matter for the migration decision ---
print()
print("## episode / scoring counts")
for tbl, label in [
    ("scores", "scores"),
    ("episode_snapshots", "episode_snapshots"),
    ("comparison_sampling_log", "comparison_sampling_log"),
    ("llm_diagnosis_log", "llm_diagnosis_log"),
]:
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:<28} {n}")
    except sqlite3.Error:
        print(f"  {tbl:<28} (absent)")

try:
    print("\n  scores by correct flag:")
    for row in conn.execute("SELECT correct, COUNT(*) c FROM scores GROUP BY correct ORDER BY correct"):
        print(f"    correct={row['correct']}: {row['c']}")
except sqlite3.Error as e:
    print(f"    (scores.correct not queryable: {e})")

try:
    print("\n  scores date range (t0):")
    row = conn.execute("SELECT MIN(t0), MAX(t0) FROM scores").fetchone()
    print(f"    {row[0]}  ..  {row[1]}")
except sqlite3.Error:
    pass

# --- accounts: non-secret columns only ---
print()
print("## accounts (no hashes / no TOTP secrets)")
try:
    acc_cols = [c["name"] for c in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    safe = [c for c in acc_cols if c in ("username", "role", "expires_at", "active", "created_at", "totp_secret")]
    for row in conn.execute(f"SELECT {', '.join(c for c in safe if c != 'totp_secret')}, "
                            f"(totp_secret IS NOT NULL) AS has_totp FROM accounts"
                            if "totp_secret" in acc_cols else
                            f"SELECT {', '.join(safe)} FROM accounts"):
        print("  " + "  ".join(f"{k}={row[k]}" for k in row.keys()))
except sqlite3.Error as e:
    print(f"  (accounts table issue: {e})")

# --- trust ladder snapshot ---
print()
print("## trust dimension A (class -> can_act / report_only)")
try:
    for row in conn.execute("SELECT * FROM trust_state ORDER BY fault_class"):
        d = dict(row)
        print(f"  {d.get('fault_class'):<28} state={d.get('state')} streak={d.get('consecutive_correct', d.get('streak'))}")
except sqlite3.Error as e:
    print(f"  (trust_state issue: {e})")

conn.close()
