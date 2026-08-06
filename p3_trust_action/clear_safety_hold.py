"""Real, reusable admin tool: clear a fault class's misdispatch_guard.py
safety hold. Does NOT touch Dimension A/B/C's own trust_state/streak --
a safety hold was never a demotion in the first place (see
misdispatch_guard.py's own module docstring).

Usage:
  python3 clear_safety_hold.py <fault_class> [--reason "why"]
"""
import argparse
import sqlite3

from publish_to_r2 import DB_PATH
from misdispatch_guard import ensure_misdispatch_tables, get_safety_hold, clear_safety_hold


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fault_class")
    parser.add_argument("--reason", default="manually cleared -- stale hold, no real misdispatch since")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_misdispatch_tables(conn)

    before = get_safety_hold(conn, args.fault_class)
    print(f"Before: {before}")

    if not before["active"]:
        print(f"{args.fault_class} has no active hold -- nothing to clear.")
        conn.close()
        return

    clear_safety_hold(conn, args.fault_class, reason=args.reason)
    after = get_safety_hold(conn, args.fault_class)
    print(f"After:  {after}")
    conn.close()


if __name__ == "__main__":
    main()
