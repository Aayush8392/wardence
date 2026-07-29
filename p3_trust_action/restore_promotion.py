"""One-off: manually restore a fault class (or several) to can_act at
the real promotion floor (PROMOTION_STREAK, currently 5 for every
auto-fix class). Originally written narrow/hardcoded to crash-loop only
(after a demotion caused by the pod-name-prefix verifier bug, now fixed
in verifier.py) -- generalized 2026-07-29 to take any class list on the
command line, reused for restoring crash-loop/oom/disk-full together
after their real forced-demotion tests, rather than writing a new
one-off script that duplicated this exact purpose.

Uses the real manual_set_state function -- the same one
operator_api.py's admin-only /promote endpoint calls -- rather than a
raw SQL statement, so the mechanism matches the real, audited recovery
path.

Run: python3 p3_trust_action/restore_promotion.py crash-loop oom disk-full
(defaults to crash-loop only if no classes are given, matching the
original script's behavior).
"""

import sqlite3
import sys

sys.path.insert(0, "p3_trust_action")

from trust_engine import CAN_ACT, DB_PATH, PROMOTION_STREAK, ensure_trust_tables, get_trust_state, manual_set_state  # noqa: E402


def main():
    classes = sys.argv[1:] or ["crash-loop"]

    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)

    for fault_class in classes:
        if fault_class not in PROMOTION_STREAK:
            print(f"WARNING: '{fault_class}' has no known promotion streak -- skipping.")
            continue
        before = get_trust_state(conn, fault_class)
        streak = PROMOTION_STREAK[fault_class]
        manual_set_state(conn, fault_class, CAN_ACT, streak)
        after = get_trust_state(conn, fault_class)
        print(f"{fault_class}: {before} -> {after}")

    conn.close()


if __name__ == "__main__":
    main()
