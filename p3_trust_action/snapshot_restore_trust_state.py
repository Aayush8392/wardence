"""
One-off, reusable safety net for manual live-trigger testing sessions:
snapshots a fault class's real Dimension A/B/C state to a JSON file
before a risky test, and restores it exactly on request if the test
corrupts something.

Built after tonight's crash-loop demotion incident (Kimi review 35/36's
async wrapper found and fixed a real 8-minute evidence-aging bug via a
live test that DID demote crash-loop's real A/196 and B/182 streaks) --
manually reconstructing the real prior values from diagnoser_mode_history/
action_trust_history after the fact worked, but only because those
tables happened to have the right rows. This script removes the need
for that archaeology: snapshot BEFORE testing, restore instantly if
needed.

Usage:
    python3 p3_trust_action/snapshot_restore_trust_state.py snapshot <fault_class>
        Writes trust_snapshot_<fault_class>_<timestamp>.json to the
        current directory with real current A/B/C state+streak.

    python3 p3_trust_action/snapshot_restore_trust_state.py restore <snapshot_file>
        Reads the file back and applies it via manual_set_state/
        manual_set_diagnoser_mode/manual_set_action_trust -- exact
        restoration, not a guess.
"""

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sqlite3  # noqa: E402

from llm_trust_state import (  # noqa: E402
    ensure_llm_trust_tables,
    get_action_trust,
    get_diagnoser_mode,
    manual_set_action_trust,
    manual_set_diagnoser_mode,
)
from trust_engine import DB_PATH, ensure_trust_tables, get_trust_state, manual_set_state  # noqa: E402


def snapshot(fault_class: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    ensure_llm_trust_tables(conn)

    data = {
        "fault_class": fault_class,
        "taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dimension_a": get_trust_state(conn, fault_class),
        "dimension_b": get_diagnoser_mode(conn, fault_class),
        "dimension_c": get_action_trust(conn, fault_class),
    }
    conn.close()

    filename = f"trust_snapshot_{fault_class}_{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    Path(filename).write_text(json.dumps(data, indent=2))
    print(f"Snapshotted {fault_class}: A={data['dimension_a']}, B={data['dimension_b']}, C={data['dimension_c']}")
    print(f"Saved to {filename}")
    return filename


def restore(snapshot_file: str) -> None:
    data = json.loads(Path(snapshot_file).read_text())
    fault_class = data["fault_class"]

    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    ensure_llm_trust_tables(conn)

    a = data["dimension_a"]
    b = data["dimension_b"]
    c = data["dimension_c"]

    manual_set_state(conn, fault_class, a["state"], streak=a["streak"])
    manual_set_diagnoser_mode(conn, fault_class, b["mode"], streak=b["streak"])
    manual_set_action_trust(conn, fault_class, c["state"], streak=c["streak"])
    conn.close()

    print(f"Restored {fault_class} from {snapshot_file} (taken {data['taken_at']}):")
    print(f"  A -> {a}")
    print(f"  B -> {b}")
    print(f"  C -> {c}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("snapshot", "restore"):
        print("Usage:")
        print("  python3 snapshot_restore_trust_state.py snapshot <fault_class>")
        print("  python3 snapshot_restore_trust_state.py restore <snapshot_file>")
        sys.exit(1)

    if sys.argv[1] == "snapshot":
        snapshot(sys.argv[2])
    else:
        restore(sys.argv[2])
