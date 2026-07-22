"""
P3 scorer: the ONLY piece allowed to see ground truth (same blinding
rule as P2). Drives trust_engine updates -- the agent itself never
judges its own correctness, this does.

Correctness definition depends on the trust state AT THE TIME the
episode was handled:
  - Report-Only: correct = diagnosis matched ground truth. No action
    was taken, so there's nothing else to verify -- this diagnosis-only
    track record is how a class earns its way to Can-Act in the first
    place.
  - Can-Act: an action was taken. correct = diagnosis matched ground
    truth AND the action applied AND the fix held for the full
    durability window (verified via verifier.verify_durability, keyed
    on the TRUE fault class, since that's the real symptom present).

Any failure (action didn't apply, or the fix flapped) also feeds the
circuit breaker.

Usage:
    Agent must be running: uvicorn p3_agent:app --reload --app-dir p3_trust_action --port 8001
    python3 p3_scorer.py
"""

import datetime
import sqlite3
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from circuit_breaker import ensure_circuit_breaker_table, record_failure  # noqa: E402
from trust_engine import DB_PATH, PROMOTION_STREAK, ensure_trust_tables, record_outcome  # noqa: E402
from verifier import verify_durability  # noqa: E402

AGENT_URL = "http://localhost:8001/handle"

# Found the hard way (2026-07-21): get_unscored_episode always picked
# the most recent unscored episode by t0, with no check on how OLD
# "most recent" actually was. A day-old leftover crash-loop episode
# (from the original 2026-07-20 P3 session, never scored) got picked
# up and scored against TODAY's live cluster state -- guaranteed to
# read as "no anomaly" since that fault's window was long gone,
# producing a false WRONG and a real (if harmless that time, since the
# class was at streak 0) trust-state write. Caught only because it
# happened to hit a class with nothing to lose -- the SAME backlog
# also contains 2 stale oom episodes, and oom had JUST been promoted
# to can_act, so the next scorer run could have used one of those to
# trigger a FALSE DEMOTION from garbage data. 10 minutes is
# comfortably above the longest durability window (5 min, oom/
# cascading) plus normal settle/scorer overhead -- anything older is a
# stale leftover, not a live fault.
MAX_EPISODE_AGE_MINUTES = 10


def ensure_scores_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            episode_id TEXT PRIMARY KEY,
            predicted_class TEXT NOT NULL,
            actual_class TEXT NOT NULL,
            correct INTEGER NOT NULL,
            confidence REAL,
            scored_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
        )
        """
    )
    # Table may already exist from P2 without these P3 columns --
    # CREATE TABLE IF NOT EXISTS doesn't retroactively add columns.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
    new_cols = {
        "action_taken": "TEXT",
        "action_applied": "INTEGER",
        "durability_verdict": "TEXT",
        "trust_correct": "INTEGER",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE scores ADD COLUMN {col} {col_type}")
    conn.commit()


def get_unscored_episode(conn: sqlite3.Connection):
    row = conn.execute(
        """
        SELECT e.episode_id, e.fault_class, e.target, e.namespace, e.t0
        FROM episodes e
        LEFT JOIN scores s ON e.episode_id = s.episode_id
        WHERE s.episode_id IS NULL
        ORDER BY e.t0 DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    episode_id, fault_class, target, namespace, t0_str = row
    t0 = datetime.datetime.fromisoformat(t0_str)
    age_minutes = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds() / 60
    if age_minutes > MAX_EPISODE_AGE_MINUTES:
        print(
            f"WARNING: most recent unscored episode ({episode_id}, {fault_class}) is "
            f"{age_minutes:.1f} minutes old -- refusing to score it. Its ground-truth "
            f"window is long gone; scoring it now would falsely read as 'no anomaly' "
            f"against the CURRENT live cluster and corrupt trust with stale data, same "
            f"principle as injector.py refusing to record ground truth on total failure "
            f"rather than silently recording bad data."
        )
        return None
    return episode_id, fault_class, target, namespace


def diagnosis_matches(predicted: str, actual: str) -> bool:
    return predicted.strip().lower() == actual.strip().lower()


def main():
    conn = sqlite3.connect(DB_PATH)
    ensure_scores_table(conn)
    ensure_trust_tables(conn)
    ensure_circuit_breaker_table(conn)

    episode = get_unscored_episode(conn)
    if episode is None:
        print("No unscored episodes found.")
        return

    episode_id, actual_class, target, namespace = episode

    # 180s, not 15s -- restore_from_disk_full (2026-07-22) now genuinely
    # polls for pod replacement instead of returning the instant the API
    # accepts a patch: up to POD_TERMINATE_TIMEOUT_S (60s) waiting for
    # the old pod to actually go, plus up to POD_START_TIMEOUT_S (60s)
    # for the replacement to reach Running = 120s worst case, plus
    # diagnosis time. A real disk-full fix legitimately takes far longer
    # than crash-loop/oom's near-instant actions.
    resp = requests.post(AGENT_URL, json={"target": target, "namespace": namespace}, timeout=180)
    resp.raise_for_status()
    result = resp.json()

    predicted_class = result["diagnosis"]
    confidence = result.get("confidence")
    diagnosis_correct = diagnosis_matches(predicted_class, actual_class)

    action_taken = result.get("action_taken")
    action_result = result.get("action_result")
    action_applied = action_result["applied"] if action_result else None
    durability_verdict = None
    trust_correct = None

    breaker_result = None
    if action_taken is not None:
        # An action was taken -- the trust state was Can-Act at handle time.
        if not diagnosis_correct or not action_applied:
            trust_correct = False
            breaker_result = record_failure(
                conn, reason="action failed or misdiagnosed", fault_class=actual_class
            )
        else:
            verdict = verify_durability(actual_class, target, namespace)
            durability_verdict = verdict["verdict"]
            trust_correct = durability_verdict == "confirmed"
            if not trust_correct:
                breaker_result = record_failure(conn, reason="fix flapped", fault_class=actual_class)
    elif actual_class in PROMOTION_STREAK:
        # Report-Only (or Demoted) auto-fix class: diagnosis-only track record.
        trust_correct = diagnosis_correct

    # Insert the scores row FIRST, marking this episode processed, before
    # touching the trust engine -- record_outcome/check_circuit_breaker
    # commit internally, so if anything after them failed, a rerun would
    # re-fetch this same "unscored" episode and double-count the trust
    # update. Marking it scored first means a later failure loses that
    # one episode's trust effect at worst, never double-counts it.
    conn.execute(
        """
        INSERT INTO scores
            (episode_id, predicted_class, actual_class, correct, confidence,
             action_taken, action_applied, durability_verdict, trust_correct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode_id,
            predicted_class,
            actual_class,
            int(diagnosis_correct),
            confidence,
            action_taken,
            None if action_applied is None else int(action_applied),
            durability_verdict,
            None if trust_correct is None else int(trust_correct),
        ),
    )
    conn.commit()

    if trust_correct is not None:
        trust_result = record_outcome(conn, actual_class, trust_correct, episode_id=episode_id)
        print("trust update:", trust_result)
        if breaker_result and breaker_result["tripped"]:
            print("CIRCUIT BREAKER TRIPPED:", breaker_result)

    conn.close()

    verdict = "CORRECT" if diagnosis_correct else "WRONG"
    print(f"Episode {episode_id}: predicted='{predicted_class}' actual='{actual_class}' -> {verdict}")


if __name__ == "__main__":
    main()
