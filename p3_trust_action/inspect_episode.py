"""One-off: print full episodes + scores + episode_snapshots + trust_history
rows for a given episode_id, plus its surrounding timeline (any class,
+/- 15 min) and, if given --target, that target's own real fault/action
history before this episode -- same "was this target genuinely still
disturbed" check used by Investigation 2 (bad-rollout none-mislabels) and
Investigation 4 (ccad4a97, under-provisioned-replicas none-mislabel).

Run: python3 p3_trust_action/inspect_episode.py <episode_id> [--target TARGET]
"""

import argparse
import json
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"


def pretty(label, value):
    print(f"\n--- {label} ---")
    if value is None:
        print("  (none)")
        return
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            print(json.dumps(parsed, indent=2))
            return
        except (json.JSONDecodeError, TypeError):
            pass
    print(f"  {value}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_id")
    parser.add_argument("--target", default=None,
                         help="also show this target's prior fault/action history before the episode")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    ep = conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (args.episode_id,)).fetchone()
    if ep is None:
        print("no such episode")
        return
    pretty("episodes row", dict(ep))

    sc = conn.execute("SELECT * FROM scores WHERE episode_id = ?", (args.episode_id,)).fetchone()
    pretty("scores row", dict(sc) if sc else None)

    snap = conn.execute(
        "SELECT * FROM episode_snapshots WHERE episode_id = ?", (args.episode_id,)
    ).fetchone()
    if snap:
        snap_d = dict(snap)
        for field in ("tool_output", "action_result"):
            pretty(f"episode_snapshots.{field}", snap_d.pop(field, None))
        pretty("episode_snapshots (remaining fields)", snap_d)
    else:
        print("\n--- episode_snapshots row ---\n  NOT FOUND (no snapshot captured for this episode)")

    th = conn.execute(
        "SELECT * FROM trust_history WHERE episode_id = ? ORDER BY recorded_at", (args.episode_id,)
    ).fetchall()
    pretty("trust_history rows", [dict(r) for r in th])

    if sc is not None:
        print("\n--- Surrounding scores (any class) within +/- 15 min of this episode's scored_at ---")
        window = conn.execute(
            """
            SELECT s.episode_id, s.actual_class, s.predicted_class, s.correct,
                   s.action_taken, s.action_applied, s.scored_at, e.target
            FROM scores s
            LEFT JOIN episodes e ON e.episode_id = s.episode_id
            WHERE s.scored_at BETWEEN
                (SELECT datetime(scored_at, '-15 minutes') FROM scores WHERE episode_id = ?)
                AND
                (SELECT datetime(scored_at, '+15 minutes') FROM scores WHERE episode_id = ?)
            ORDER BY s.scored_at
            """,
            (args.episode_id, args.episode_id),
        ).fetchall()
        for r in window:
            marker = "  <=== THIS EPISODE" if r["episode_id"] == args.episode_id else ""
            print(f"  {r['scored_at']}  {r['actual_class']:26s} -> predicted={r['predicted_class']:26s} "
                  f"target={r['target'] or '?':12s} action={r['action_taken']} applied={r['action_applied']}{marker}")

    if args.target and sc is not None:
        print(f"\n--- All prior episodes on target='{args.target}', before this one ---")
        prior = conn.execute(
            """
            SELECT s.episode_id, s.actual_class, s.correct, s.action_taken, s.action_applied,
                   s.durability_verdict, s.scored_at
            FROM scores s
            LEFT JOIN episodes e ON e.episode_id = s.episode_id
            WHERE e.target = ?
              AND s.scored_at < (SELECT scored_at FROM scores WHERE episode_id = ?)
            ORDER BY s.scored_at DESC
            LIMIT 10
            """,
            (args.target, args.episode_id),
        ).fetchall()
        for r in prior:
            print(f"  {r['scored_at']}  ep={r['episode_id']}  class={r['actual_class']} correct={r['correct']} "
                  f"action={r['action_taken']} applied={r['action_applied']} durability={r['durability_verdict']}")

    conn.close()


if __name__ == "__main__":
    main()
