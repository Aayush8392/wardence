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

import sqlite3
from pathlib import Path

import requests

AGENT_URL = "http://localhost:8000/diagnose"
# Must match injector.py's DB_PATH -- see injector.py for why this lives
# on WSL2's native filesystem instead of the Windows-mounted C:/ path.
OUTPUT_DIR = Path.home() / "wardence_p2_data"
DB_PATH = OUTPUT_DIR / "wardence.db"


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
