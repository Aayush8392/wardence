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

import sqlite3
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from circuit_breaker import (  # noqa: E402
    check_circuit_breaker,
    ensure_circuit_breaker_table,
    record_failure,
)
from trust_engine import DB_PATH, PROMOTION_STREAK, ensure_trust_tables, record_outcome  # noqa: E402
from verifier import verify_durability  # noqa: E402

AGENT_URL = "http://localhost:8001/handle"


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
        SELECT e.episode_id, e.fault_class, e.target, e.namespace
        FROM episodes e
        LEFT JOIN scores s ON e.episode_id = s.episode_id
        WHERE s.episode_id IS NULL
        ORDER BY e.t0 DESC
        LIMIT 1
        """
    ).fetchone()
    return row


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

    resp = requests.post(AGENT_URL, json={"target": target, "namespace": namespace}, timeout=15)
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

    if action_taken is not None:
        # An action was taken -- the trust state was Can-Act at handle time.
        if not diagnosis_correct or not action_applied:
            trust_correct = False
            record_failure(conn, reason="action failed or misdiagnosed", fault_class=actual_class)
        else:
            verdict = verify_durability(actual_class, target, namespace)
            durability_verdict = verdict["verdict"]
            trust_correct = durability_verdict == "confirmed"
            if not trust_correct:
                record_failure(conn, reason="fix flapped", fault_class=actual_class)
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
        if not trust_correct:
            breaker_result = check_circuit_breaker(conn)
            if breaker_result["tripped"]:
                print("CIRCUIT BREAKER TRIPPED:", breaker_result)

    conn.close()

    verdict = "CORRECT" if diagnosis_correct else "WRONG"
    print(f"Episode {episode_id}: predicted='{predicted_class}' actual='{actual_class}' -> {verdict}")


if __name__ == "__main__":
    main()
