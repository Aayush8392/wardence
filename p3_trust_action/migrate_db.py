#!/usr/bin/env python3
"""
One-off: migrate the WSL episode DB onto Oracle, OVERWRITING Oracle's DB.

The WSL DB (~3800 episodes, clean trust ladder) becomes authoritative on
Oracle. Oracle's own ~150 episodes (many failures from the migration/testing
period) are discarded -- the pre-swap Oracle DB is kept as a timestamped
backup, never deleted.

Two steps, run on two hosts, with an scp in between:

  1. WSL:
       python3 p3_trust_action/migrate_db.py snapshot
     -> writes  ~/wardence_db_snapshot.db  (a consistent online copy), and
        prints its size, sha256, and key row counts.

  2. copy the snapshot to Oracle (from WSL, or via your Windows box):
       scp ~/wardence_db_snapshot.db  ubuntu@<oracle-host>:~/

  3. Oracle (services stopped -- see below):
       python3 p3_trust_action/migrate_db.py apply ~/wardence_db_snapshot.db

APPLY does, in order:
  - sanity-check the snapshot (opens, has the tables, episodes >= 3000)
  - refuse to run while any wardence-* service is active (pass --force to override)
  - back up the live DB  ->  <db>.bak-<UTC-timestamp>
  - swap the snapshot in  (copy to <db>.new, then os.replace)
  - FIXUPS on the new live DB:
      * restore the live 'Aayush' admin password_hash + totp_secret from the
        backup (Oracle's authenticator entry is separate from WSL's). Demo +
        test accounts come from the snapshot unchanged.
      * reset system_lock to idle (holder / acquired_at = NULL)
      * clear safety_hold (a WSL hold is not an Oracle hold)
      * any non-terminal episode_state -> 'failed', subprocess_pid -> NULL
        (WSL pids mean nothing on Oracle; keeps startup reconciliation quiet)
      * drop the 'test-fault-class' cruft rows from trust_state / action_trust /
        diagnoser_mode
  - print a verification summary

AFTER apply (manual):
  sudo systemctl start wardence-operator-api wardence-p3-agent wardence-p2-agent wardence-detector-service
  cd ~/wardence/p3_trust_action && python3 publish_to_r2.py
  then load the dashboard and spot-check the Trust Ladder / Replay Viewer.
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LIVE_DB = Path.home() / "wardence_p2_data" / "wardence.db"
SNAPSHOT_DEFAULT = Path.home() / "wardence_db_snapshot.db"
SERVICES = [
    "wardence-operator-api",
    "wardence-p3-agent",
    "wardence-p2-agent",
    "wardence-detector-service",
]
EXPECTED_TABLES = {
    "accounts", "scores", "episodes", "episode_snapshots", "trust_state",
    "action_trust", "diagnoser_mode", "trust_history", "diagnoser_mode_history",
    "action_trust_history", "system_lock", "safety_hold",
}
MIN_EPISODES = 3000
CRUFT_CLASS = "test-fault-class"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = {}
        for t in ("episodes", "scores", "episode_snapshots", "accounts"):
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = None
        try:
            out["scores_correct_1"] = conn.execute(
                "SELECT COUNT(*) FROM scores WHERE correct=1"
            ).fetchone()[0]
        except sqlite3.Error:
            out["scores_correct_1"] = None
        return out
    finally:
        conn.close()


def cmd_snapshot() -> None:
    if not LIVE_DB.exists():
        sys.exit(f"no DB at {LIVE_DB}")
    dst = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else SNAPSHOT_DEFAULT
    print(f"source : {LIVE_DB}  ({LIVE_DB.stat().st_size:,} bytes)")
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)  # consistent even if something is mid-write
    out.close()
    src.close()
    print(f"snapshot: {dst}  ({dst.stat().st_size:,} bytes)")
    print(f"sha256  : {_sha256(dst)}")
    print(f"counts  : {_counts(dst)}")
    print("\nnext: scp this file to Oracle, then run `migrate_db.py apply <path>` there")


def _services_active() -> list:
    active = []
    for svc in SERVICES:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc], capture_output=True, text=True
            )
            if r.stdout.strip() == "active":
                active.append(svc)
        except FileNotFoundError:
            pass
    return active


def cmd_apply() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: migrate_db.py apply <snapshot.db> [--force]")
    snap = Path(sys.argv[2]).expanduser()
    force = "--force" in sys.argv[3:]

    if not snap.exists():
        sys.exit(f"snapshot not found: {snap}")
    try:
        c = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ep = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        c.close()
    except sqlite3.Error as e:
        sys.exit(f"snapshot is not a readable sqlite DB: {e}")
    missing = EXPECTED_TABLES - names
    if missing:
        sys.exit(f"snapshot missing expected tables: {sorted(missing)}")
    if ep < MIN_EPISODES:
        sys.exit(f"snapshot has only {ep} episodes (< {MIN_EPISODES}) -- refusing, looks wrong")

    active = _services_active()
    if active and not force:
        sys.exit(
            "these services are still running: " + ", ".join(active) +
            "\nstop them first:\n  sudo systemctl stop " + " ".join(SERVICES) +
            "\n(or re-run with --force if you know what you're doing)"
        )

    if not LIVE_DB.exists():
        sys.exit(f"no live DB to replace at {LIVE_DB} -- create the dir/file path first")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = LIVE_DB.with_name(f"wardence.db.bak-{stamp}")
    shutil.copy2(LIVE_DB, backup)
    print(f"backed up live DB -> {backup}  ({backup.stat().st_size:,} bytes)")
    print(f"  live counts (pre-swap): {_counts(LIVE_DB)}")
    print(f"  snap counts           : {_counts(snap)}")

    # --- preserve Oracle's own admin credentials before the swap ---
    b = sqlite3.connect(str(backup))
    admin_row = b.execute(
        "SELECT username, password_hash, totp_secret, role, active, expires_at, created_at "
        "FROM accounts WHERE role='admin'"
    ).fetchone()
    b.close()
    if not admin_row:
        sys.exit("no admin account found in the live DB backup -- aborting before swap")
    print(f"  preserving admin account: {admin_row[0]}")

    # --- swap in the snapshot ---
    new = LIVE_DB.with_name("wardence.db.new")
    shutil.copy2(snap, new)
    os.replace(new, LIVE_DB)
    print(f"swapped snapshot into {LIVE_DB}")

    # --- fixups ---
    conn = sqlite3.connect(str(LIVE_DB))
    with conn:
        # admin creds: keep Oracle's. If the snapshot has this username, update
        # it in place; otherwise insert the whole Oracle admin row.
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE username=?", (admin_row[0],)
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE accounts SET password_hash=?, totp_secret=?, role='admin', active=1 "
                "WHERE username=?",
                (admin_row[1], admin_row[2], admin_row[0]),
            )
            print(f"  admin '{admin_row[0]}': restored Oracle password_hash + totp_secret")
        else:
            conn.execute(
                "INSERT INTO accounts (username, password_hash, totp_secret, role, active, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                admin_row,
            )
            print(f"  admin '{admin_row[0]}': inserted Oracle admin row (was absent from snapshot)")

        conn.execute("UPDATE system_lock SET holder=NULL, acquired_at=NULL")
        print("  system_lock -> idle")

        held = conn.execute("SELECT COUNT(*) FROM safety_hold WHERE active=1").fetchone()[0]
        conn.execute("DELETE FROM safety_hold")
        print(f"  safety_hold -> cleared ({held} active row(s) dropped)")

        nonterm = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE episode_state IN "
            "('injecting','holding','awaiting_fix','resolving')"
        ).fetchone()[0]
        conn.execute(
            "UPDATE episodes SET episode_state='failed', subprocess_pid=NULL, "
            "stop_hold_requested=0, abandon_requested=0 "
            "WHERE episode_state IN ('injecting','holding','awaiting_fix','resolving')"
        )
        print(f"  episodes: {nonterm} non-terminal row(s) -> 'failed'")

        for tbl in ("trust_state", "action_trust", "diagnoser_mode",
                    "trust_history", "action_trust_history", "diagnoser_mode_history"):
            try:
                cur = conn.execute(f"DELETE FROM {tbl} WHERE fault_class=?", (CRUFT_CLASS,))
                if cur.rowcount:
                    print(f"  {tbl}: dropped {cur.rowcount} '{CRUFT_CLASS}' row(s)")
            except sqlite3.Error:
                pass

    # --- verify ---
    print("\n--- post-migration state ---")
    print(f"counts: {_counts(LIVE_DB)}")
    for row in conn.execute(
        "SELECT username, role, active, (totp_secret IS NOT NULL) FROM accounts ORDER BY role"
    ):
        print(f"  account: {row[0]:<10} role={row[1]:<13} active={row[2]} totp={row[3]}")
    print("  trust dimension A:")
    for row in conn.execute("SELECT fault_class, state, streak FROM trust_state ORDER BY fault_class"):
        print(f"    {row[0]:<28} {row[1]:<12} streak={row[2]}")
    conn.close()

    print(
        "\nDONE. Next:\n"
        f"  sudo systemctl start {' '.join(SERVICES)}\n"
        "  cd ~/wardence/p3_trust_action && python3 publish_to_r2.py\n"
        "  then load the dashboard and spot-check.\n"
        f"  rollback if needed:  cp {backup} {LIVE_DB}"
    )


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("snapshot", "apply"):
        sys.exit(__doc__)
    if sys.argv[1] == "snapshot":
        cmd_snapshot()
    else:
        cmd_apply()


if __name__ == "__main__":
    main()
