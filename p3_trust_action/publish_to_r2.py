"""
Zone 2 publisher: reads the local SQLite DB (episodes, scores,
episode_snapshots, trust_state, trust_history) and pushes JSON snapshots
to Cloudflare R2, per the locked architecture (wardence_context.md Zone 2
-- "site builds ONCE, SPA fetches from R2 at runtime", no always-on DB).

Produces four objects in the bucket:
  - trust_ladder.json   -- current per-fault-class state (Trust Ladder board)
  - trust_history.json  -- every promotion/demotion event over time
  - episodes.json        -- full per-episode data (scores + episode_snapshots
                             joined), feeds both the case list and the
                             Replay Viewer
  - system_status.json   -- global circuit-breaker state (read-only mirror
                             of circuit_breaker.py's own trip check, for the
                             landing page's "System Guard" indicator)

Deliberately NOT incremental/streaming -- this project's whole dataset is
~150 episodes, small enough to just re-upload the full JSON each run
rather than build any diffing logic for it.

Usage:
    pip install -r p3_trust_action/requirements.txt
    python3 p3_trust_action/publish_to_r2.py
"""

import json
import sqlite3
from pathlib import Path

import boto3

from circuit_breaker import FAILURE_THRESHOLD, FAILURE_WINDOW_S, ensure_circuit_breaker_table

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"
ENV_PATH = Path(__file__).parent / ".env"

# Auto-fix classes only -- these are the only ones with a real promotion
# streak/state machine (trust_engine.PROMOTION_STREAK). Report-only classes
# are permanently report_only by design (no promotion path exists for
# them), so they're handled separately below via their scores alone.
AUTO_FIX_CLASSES = {"crash-loop", "oom", "disk-full"}


def load_env(path: Path) -> dict:
    """Minimal .env parser -- avoids adding python-dotenv as a dependency
    for four key=value lines."""
    env = {}
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- create it with the R2 credentials first")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def build_trust_ladder(conn: sqlite3.Connection) -> list[dict]:
    ladder = []

    auto_fix_rows = {
        row[0]: {"state": row[1], "streak": row[2], "updated_at": row[3]}
        for row in conn.execute("SELECT fault_class, state, streak, updated_at FROM trust_state")
    }

    # Every class that has ever been scored, auto-fix or report-only.
    all_classes = [
        row[0] for row in conn.execute("SELECT DISTINCT actual_class FROM scores")
    ]

    for fault_class in sorted(all_classes):
        # Phase E (2026-07-27): episodes marked phase_e_status='excluded'
        # had bad ground truth (mislabeled 'none' controls) and are
        # dropped from aggregate accuracy entirely -- not counted as
        # either correct or wrong. 'reclassified' episodes ARE counted,
        # using their corrected `correct` value (already updated in place
        # by phase_e_apply_corrections.py, with the original preserved in
        # original_correct for anyone auditing the change).
        total_correct_row = conn.execute(
            "SELECT COUNT(*), SUM(correct) FROM scores "
            "WHERE actual_class = ? AND (phase_e_status IS NULL OR phase_e_status != 'excluded')",
            (fault_class,),
        ).fetchone()
        total, correct = total_correct_row[0], total_correct_row[1] or 0
        diagnosis_accuracy = correct / total if total else None

        if fault_class in AUTO_FIX_CLASSES:
            state_row = auto_fix_rows.get(fault_class, {"state": "report_only", "streak": 0, "updated_at": None})
            ladder.append(
                {
                    "fault_class": fault_class,
                    "tier": "auto-fix",
                    "state": state_row["state"],
                    "streak": state_row["streak"],
                    "updated_at": state_row["updated_at"],
                    "diagnosis_accuracy": diagnosis_accuracy,
                    "episodes_scored": total,
                }
            )
        else:
            # Report-only classes have no promotion path -- state is
            # always report_only by design, not something earned or lost.
            ladder.append(
                {
                    "fault_class": fault_class,
                    "tier": "report-only",
                    "state": "report_only",
                    "streak": None,
                    "updated_at": None,
                    "diagnosis_accuracy": diagnosis_accuracy,
                    "episodes_scored": total,
                }
            )

    return ladder


def build_trust_history(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trust_history ORDER BY recorded_at ASC"
    ).fetchall()
    conn.row_factory = None
    return [dict(row) for row in rows]


def build_episodes(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            e.episode_id, e.fault_class, e.target, e.namespace, e.t0,
            s.predicted_class, s.correct, s.confidence AS score_confidence,
            s.scored_at, s.action_taken AS scores_action_taken,
            s.action_applied, s.durability_verdict AS scores_durability_verdict,
            s.trust_correct, s.phase_e_status, s.phase_e_note,
            snap.tool_output, snap.reasoning, snap.confidence AS snapshot_confidence,
            snap.action_result, snap.durability_verdict AS snapshot_durability_verdict,
            snap.durability_elapsed_s, snap.gate_substitution
        FROM episodes e
        JOIN scores s ON e.episode_id = s.episode_id
        LEFT JOIN episode_snapshots snap ON e.episode_id = snap.episode_id
        ORDER BY e.t0 DESC
        """
    ).fetchall()
    conn.row_factory = None

    episodes = []
    for row in rows:
        d = dict(row)
        # tool_output/action_result are stored as JSON text in SQLite --
        # decode them so the published JSON has real nested objects, not
        # doubly-escaped strings.
        if d.get("tool_output"):
            d["tool_output"] = json.loads(d["tool_output"])
        if d.get("action_result"):
            d["action_result"] = json.loads(d["action_result"])
        if d.get("gate_substitution"):
            d["gate_substitution"] = json.loads(d["gate_substitution"])
        episodes.append(d)
    return episodes


def build_system_status(conn: sqlite3.Connection) -> dict:
    """Read-only mirror of circuit_breaker.py's own trip check -- the
    publisher must never mutate trust state itself, so this recomputes the
    same FAILURE_THRESHOLD-within-FAILURE_WINDOW_S logic directly instead of
    importing/calling check_circuit_breaker (which trips the breaker as a
    side effect)."""
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM failure_log
        WHERE recorded_at >= datetime('now', '-{FAILURE_WINDOW_S} seconds')
        """
    ).fetchone()
    recent_failures = row[0]
    return {
        "recent_failures": recent_failures,
        "tripped": recent_failures >= FAILURE_THRESHOLD,
        "failure_window_s": FAILURE_WINDOW_S,
        "failure_threshold": FAILURE_THRESHOLD,
    }


def upload_json(client, bucket: str, key: str, data) -> None:
    body = json.dumps(data, indent=2).encode("utf-8")
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    print(f"Uploaded {key} ({len(body)} bytes)")


def main():
    env = load_env(ENV_PATH)
    for required in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT", "R2_BUCKET_NAME"):
        if not env.get(required):
            raise ValueError(f"{required} is missing/blank in {ENV_PATH}")

    client = boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = env["R2_BUCKET_NAME"]

    conn = sqlite3.connect(DB_PATH)
    ensure_circuit_breaker_table(conn)  # in case this DB has never had a failure recorded yet

    trust_ladder = build_trust_ladder(conn)
    trust_history = build_trust_history(conn)
    episodes = build_episodes(conn)
    system_status = build_system_status(conn)

    conn.close()

    upload_json(client, bucket, "trust_ladder.json", trust_ladder)
    upload_json(client, bucket, "trust_history.json", trust_history)
    upload_json(client, bucket, "episodes.json", episodes)
    upload_json(client, bucket, "system_status.json", system_status)


if __name__ == "__main__":
    main()
