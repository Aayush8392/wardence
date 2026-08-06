"""
Phase H step 3, stage 1: replay recorded episode_snapshots through the
real, locked provider chain (model_backend.PROVIDER_CHAIN) and log the
result for comparison -- COMPARISON ONLY.

Locked scope, per direct user instruction (2026-07-30 session): until
every fault class has 150+ real episodes, an LLM diagnosis NEVER counts
toward trust-ladder promotion/demotion and NEVER drives a real action.
This script enforces that by construction, not by a flag someone could
forget to set -- it only ever reads from episode_snapshots (a real
completed episode's frozen tool_output) and writes to a NEW,
independent table (llm_diagnosis_log). It never touches `scores`,
`trust_state`, `trust_history`, or `episodes`, never calls
trust_engine.record_outcome, and never imports/calls run_batch_plan.py
or stub_diagnose (stub_diagnose keeps running forever as the permanent
rule-baseline comparison column -- untouched by this file).

This is the "does the LLM even work, single-episode, no hand-holding
about live cluster access" stage. The next stage (once this looks
healthy) points the same PROVIDER_CHAIN at a REAL live fault via the
real agent endpoints -- not built here, deliberately out of scope for
this file.

Usage:
    python3 llm_replay_test.py --episode-id <id>
    python3 llm_replay_test.py --sample-per-class 3
    python3 llm_replay_test.py --sample-per-class 3 --fault-class oom
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

from model_backend import call_chain  # noqa: E402
from trust_engine import DB_PATH  # noqa: E402

# Deliberately NOT imported from llm_smoke_test.py -- that file loads
# /tmp/smoke_test_cases.json at module import time (a one-off test-case
# file that may not exist on this machine anymore), which would crash
# this script's import for an unrelated reason. Kept in sync by hand;
# same list/template as llm_smoke_test.py's own copy.
FAULT_CLASSES = [
    "crash-loop", "oom", "disk-full", "network-latency", "memory-leak",
    "connection-pool-exhaustion", "network-partition", "init-failure",
    "session-cart-failure", "cpu-throttling", "under-provisioned-replicas",
    "bad-rollout", "none",
]

PROMPT_TEMPLATE = """You are an SRE diagnosing a fault in a Kubernetes microservices cluster (Sock Shop). You are given raw tool output from Prometheus queries. Diagnose which ONE of these fault classes is present, or "none" if no anomaly is present:

{classes}

Tool output (JSON):
{tool_output}

Field meanings, WITH the real thresholds used to interpret each one (a null/false/empty field means that signal did not fire):
- oom_pods: non-empty -> oom.
- evicted_pods: non-empty -> disk-full.
- crashlooping_pods: non-empty (and oom_pods/evicted_pods are empty) -> crash-loop.
- p95_latency_ms (orders only): >= 300ms -> network-latency.
- combined_throughput_bps (orders only): < 200 bytes/s -> network-partition (check this before network-latency).
- payment_stuck_not_ready: true -> init-failure.
- session_db_replicas_hit_zero: true -> session-cart-failure.
- peak_memory_mib (shipping only): >= 380 MiB -> memory-leak.
- peak_threads_connected (catalogue-db only): >= 100 -> connection-pool-exhaustion.
- cpu_throttle_periods_increase (user only): >= 100 -> cpu-throttling.
- front_end_image_pull_failing: true -> bad-rollout.
- catalogue_probe_p95_ms (only present when every other check found nothing): >= 200ms -> under-provisioned-replicas.
- If NONE of the above thresholds are met, diagnosis is "none".

Respond with ONLY a JSON object, no other text: {{"diagnosis": "<one of the classes above>", "confidence": <your genuine self-assessed probability that this diagnosis is correct, a real number between 0 and 1 to EXACTLY 4 decimal places, e.g. 0.8734 -- reflect real uncertainty in the digits, do not default to a round or repeated-digit value like 0.9000, 0.9500, or 0.9999>, "reasoning": "<one sentence>"}}"""


def build_prompt(tool_output: dict) -> str:
    return PROMPT_TEMPLATE.format(
        classes=", ".join(FAULT_CLASSES),
        tool_output=json.dumps(tool_output, indent=2),
    )


def ensure_llm_diagnosis_log_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_diagnosis_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT NOT NULL,
            actual_class TEXT NOT NULL,
            stub_predicted_class TEXT,
            stub_correct INTEGER,
            llm_diagnosis TEXT,
            llm_confidence REAL,
            llm_confidence_source TEXT,
            llm_reasoning TEXT,
            provider TEXT,
            model TEXT,
            tier TEXT,
            matches_ground_truth INTEGER,
            matches_stub INTEGER,
            failed_attempts_json TEXT,
            tested_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
        )
        """
    )
    # response_time_ms added 2026-08-05 -- real wall-clock duration of
    # the actual API call (not including any DB/dispatch overhead), so
    # different providers/models become comparable on speed, not just
    # accuracy. Nullable/additive -- existing rows keep NULL, never
    # backfilled.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_diagnosis_log)")}
    if "response_time_ms" not in existing_cols:
        conn.execute("ALTER TABLE llm_diagnosis_log ADD COLUMN response_time_ms REAL")
    # llm_version_fingerprint added 2026-08-1x -- real per-call backend-
    # version signal (Gemini's modelVersion / openai_compat's
    # system_fingerprint, see model_backend.py's LLMResult), captured for
    # a future model-version-drift check, not consumed by anything yet.
    # Nullable/additive, same convention as response_time_ms above --
    # existing rows keep NULL, never backfilled (the real value only
    # ever existed on the original live API response, not recoverable
    # after the fact).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_diagnosis_log)")}
    if "llm_version_fingerprint" not in existing_cols:
        conn.execute("ALTER TABLE llm_diagnosis_log ADD COLUMN llm_version_fingerprint TEXT")
    conn.commit()


def fetch_episode(conn: sqlite3.Connection, episode_id: str):
    row = conn.execute(
        """
        SELECT e.episode_id, e.fault_class AS actual_class,
               es.tool_output, s.predicted_class AS stub_predicted, s.correct AS stub_correct
        FROM episodes e
        JOIN episode_snapshots es ON es.episode_id = e.episode_id
        LEFT JOIN scores s ON s.episode_id = e.episode_id
        WHERE e.episode_id = ?
        """,
        (episode_id,),
    ).fetchone()
    return row


def sample_episode_ids(conn: sqlite3.Connection, n_per_class: int, only_class: str | None):
    classes = [only_class] if only_class else FAULT_CLASSES
    ids = []
    for fc in classes:
        rows = conn.execute(
            """
            SELECT e.episode_id
            FROM episodes e
            JOIN episode_snapshots es ON es.episode_id = e.episode_id
            WHERE e.fault_class = ?
            ORDER BY e.t0 DESC
            LIMIT ?
            """,
            (fc, n_per_class),
        ).fetchall()
        ids.extend(r[0] for r in rows)
    return ids


# stub_diagnose (agent.py) labels the control case "no anomaly detected";
# this script's own prompt/FAULT_CLASSES list (matching llm_smoke_test.py
# and episodes.fault_class) uses "none" for the same real outcome. Same
# thing, different string -- normalize before comparing, or every real
# control episode falsely looks like a disagreement.
_NO_FAULT_LABELS = {"none", "no anomaly detected"}


def _same_diagnosis(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    return a in _NO_FAULT_LABELS and b in _NO_FAULT_LABELS


def replay_one(conn: sqlite3.Connection, episode_id: str) -> dict:
    row = fetch_episode(conn, episode_id)
    if row is None:
        return {"episode_id": episode_id, "error": "no episode_snapshots row for this episode_id"}
    ep_id, actual_class, tool_output_json, stub_predicted, stub_correct = row
    tool_output = json.loads(tool_output_json) if tool_output_json else {}

    prompt = build_prompt(tool_output)
    result, attempts = call_chain(prompt)

    if result is None:
        record = {
            "episode_id": ep_id, "actual_class": actual_class,
            "stub_predicted_class": stub_predicted, "stub_correct": stub_correct,
            "llm_diagnosis": None, "llm_confidence": None, "llm_confidence_source": None,
            "llm_reasoning": None, "provider": None, "model": None, "tier": None,
            "matches_ground_truth": None, "matches_stub": None,
            "failed_attempts_json": json.dumps([a.__dict__ for a in attempts]),
        }
    else:
        parsed = result.parsed or {}
        llm_diagnosis = parsed.get("diagnosis")
        record = {
            "episode_id": ep_id, "actual_class": actual_class,
            "stub_predicted_class": stub_predicted, "stub_correct": stub_correct,
            "llm_diagnosis": llm_diagnosis, "llm_confidence": result.confidence,
            "llm_confidence_source": result.confidence_source,
            "llm_reasoning": parsed.get("reasoning"),
            "provider": result.provider, "model": result.model, "tier": result.tier,
            "matches_ground_truth": int(_same_diagnosis(llm_diagnosis, actual_class)) if llm_diagnosis else None,
            "matches_stub": int(_same_diagnosis(llm_diagnosis, stub_predicted)) if llm_diagnosis and stub_predicted else None,
            "failed_attempts_json": json.dumps([a.__dict__ for a in attempts]),
        }

    conn.execute(
        """
        INSERT INTO llm_diagnosis_log (
            episode_id, actual_class, stub_predicted_class, stub_correct,
            llm_diagnosis, llm_confidence, llm_confidence_source, llm_reasoning,
            provider, model, tier, matches_ground_truth, matches_stub, failed_attempts_json
        ) VALUES (
            :episode_id, :actual_class, :stub_predicted_class, :stub_correct,
            :llm_diagnosis, :llm_confidence, :llm_confidence_source, :llm_reasoning,
            :provider, :model, :tier, :matches_ground_truth, :matches_stub, :failed_attempts_json
        )
        """,
        record,
    )
    conn.commit()
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-id", help="replay one specific episode_id")
    parser.add_argument("--sample-per-class", type=int, help="replay N most recent episodes per fault class")
    parser.add_argument("--fault-class", help="restrict --sample-per-class to one class")
    args = parser.parse_args()

    if not args.episode_id and not args.sample_per_class:
        parser.error("pass --episode-id or --sample-per-class")

    conn = sqlite3.connect(DB_PATH)
    ensure_llm_diagnosis_log_table(conn)

    episode_ids = [args.episode_id] if args.episode_id else sample_episode_ids(
        conn, args.sample_per_class, args.fault_class
    )

    results = [replay_one(conn, ep_id) for ep_id in episode_ids]
    conn.close()

    total = len(results)
    correct = sum(1 for r in results if r.get("matches_ground_truth"))
    beat_stub = sum(
        1 for r in results
        if r.get("matches_ground_truth") and r.get("stub_correct") == 0
    )
    print(json.dumps(results, indent=2))
    print(f"\n{correct}/{total} correct vs ground truth. "
          f"{beat_stub} case(s) where the LLM was right and the stub wasn't. "
          f"Logged to llm_diagnosis_log -- not scores, not trust_state.")
