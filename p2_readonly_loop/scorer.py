"""
P2 scorer: compares the agent's diagnosis against the ground-truth
fault label recorded by the injector, and logs the verdict.

Picks the most recent episode in wardence.db that hasn't been scored
yet, calls the running agent's /diagnose endpoint, and records the
result. The agent itself never touches this DB -- only the scorer
reads ground truth, preserving the blinding requirement.

Usage:
    Agent must already be running (uvicorn agent:app --reload).
    python3 scorer.py
"""

import datetime
import sqlite3
from pathlib import Path

import requests

AGENT_URL = "http://localhost:8000/diagnose"
# Must match injector.py's DB_PATH -- see injector.py for why this lives
# on WSL2's native filesystem instead of the Windows-mounted C:/ path.
OUTPUT_DIR = Path.home() / "wardence_p2_data"
DB_PATH = OUTPUT_DIR / "wardence.db"

# Found the hard way in p3_scorer.py (2026-07-21), same bug applies
# here: picking "most recent unscored" with no staleness check means a
# day-old leftover episode can get scored against TODAY's live cluster
# state, guaranteed to read as "no anomaly" and corrupt the accuracy
# record with a false WRONG. 10 minutes comfortably covers normal
# injector->settle->scorer timing with margin.
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
            f"{age_minutes:.1f} minutes old -- refusing to score it (stale ground truth, "
            f"would falsely read as 'no anomaly' against the current cluster)."
        )
        return None
    return episode_id, fault_class, target, namespace


def diagnosis_matches(predicted: str, actual: str) -> bool:
    predicted = predicted.strip().lower()
    actual = actual.strip().lower()
    if actual == "none":
        return predicted == "no anomaly detected"
    return predicted == actual


def main():
    conn = sqlite3.connect(DB_PATH)
    ensure_scores_table(conn)

    episode = get_unscored_episode(conn)
    if episode is None:
        print("No unscored episodes found.")
        return

    episode_id, actual_class, target, namespace = episode

    # 150s, not 15s -- REAL BUG FIXED (2026-07-27): under-provisioned-
    # replicas' diagnosis fallback (probe_catalogue_capacity, agent.py)
    # fires a real k6 burst (~20-30s incl. kubectl pod overhead) and, as
    # of today's retry fix (PROBE_MAX_ATTEMPTS=3, added after 2 real
    # Phase D misdiagnoses caused by a single flaky probe attempt), can
    # now take up to ~90-100s worst case on retries. 15s was already
    # borderline-too-tight for a single attempt; with retries it's
    # guaranteed to time out. Confirmed via a real ReadTimeout during
    # this exact class's verification run.
    resp = requests.post(AGENT_URL, json={"target": target, "namespace": namespace}, timeout=150)
    resp.raise_for_status()
    result = resp.json()

    predicted_class = result["diagnosis"]
    confidence = result.get("confidence")
    correct = diagnosis_matches(predicted_class, actual_class)

    conn.execute(
        "INSERT INTO scores (episode_id, predicted_class, actual_class, correct, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (episode_id, predicted_class, actual_class, int(correct), confidence),
    )
    conn.commit()
    conn.close()

    verdict = "CORRECT" if correct else "WRONG"
    print(f"Episode {episode_id}: predicted='{predicted_class}' actual='{actual_class}' -> {verdict}")


if __name__ == "__main__":
    main()
