"""One-off: manually set a class's Dimension B (diagnoser_mode) or
Dimension C (action_trust) state/streak -- same purpose as manually
promoting/demoting a Dimension A class via the trust_engine, for
testing the streak-5 promotion boundary without waiting for 5 real
episodes. Logs a history row so the override is auditable, same
"reasoned overrides get logged transparently" rule already applied to
Dimension A corrections in this session (see restore_after_cb_test_bug.py).

Usage:
    python3 p3_trust_action/set_llm_trust_state.py --dimension b --class crash-loop --mode llm --streak 5
    python3 p3_trust_action/set_llm_trust_state.py --dimension c --class oom --state llm_can_act --streak 5
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from llm_trust_state import (  # noqa: E402
    DB_PATH, ensure_llm_trust_tables, get_action_trust, get_diagnoser_mode,
    manual_set_action_trust, manual_set_diagnoser_mode,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dimension", required=True, choices=["b", "c"])
    parser.add_argument("--class", dest="fault_class", required=True)
    parser.add_argument("--mode", choices=["stub", "llm"], help="Dimension B only")
    parser.add_argument("--state", choices=["deterministic_fallback", "llm_can_act"], help="Dimension C only")
    parser.add_argument("--streak", type=int, required=True)
    parser.add_argument("--reason", default="manual test of streak-5 promotion boundary")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_llm_trust_tables(conn)

    if args.dimension == "b":
        if not args.mode:
            parser.error("--mode is required for --dimension b")
        before = get_diagnoser_mode(conn, args.fault_class)
        manual_set_diagnoser_mode(conn, args.fault_class, args.mode, args.streak)
        conn.execute(
            """
            INSERT INTO diagnoser_mode_history
                (episode_id, fault_class, correct, mode_before, mode_after, streak_before, streak_after)
            VALUES (NULL, ?, 1, ?, ?, ?, ?)
            """,
            (args.fault_class, before["mode"], args.mode, before["streak"], args.streak),
        )
        conn.commit()
        after = get_diagnoser_mode(conn, args.fault_class)
        print(f"Dimension B, {args.fault_class}: {before} -> {after}  (reason: {args.reason})")
    else:
        if not args.state:
            parser.error("--state is required for --dimension c")
        before = get_action_trust(conn, args.fault_class)
        manual_set_action_trust(conn, args.fault_class, args.state, args.streak)
        conn.execute(
            """
            INSERT INTO action_trust_history
                (episode_id, fault_class, correct, state_before, state_after, streak_before, streak_after)
            VALUES (NULL, ?, 1, ?, ?, ?, ?)
            """,
            (args.fault_class, before["state"], args.state, before["streak"], args.streak),
        )
        conn.commit()
        after = get_action_trust(conn, args.fault_class)
        print(f"Dimension C, {args.fault_class}: {before} -> {after}  (reason: {args.reason})")

    conn.close()


if __name__ == "__main__":
    main()
