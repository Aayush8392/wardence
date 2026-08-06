"""
Zone 2 publisher: reads the local SQLite DB (episodes, scores,
episode_snapshots, trust_state/history, diagnoser_mode/history,
action_trust/history, llm_diagnosis_log) and pushes JSON snapshots to
Cloudflare R2, per the locked architecture (wardence_context.md Zone 2
-- "site builds ONCE, SPA fetches from R2 at runtime", no always-on DB).

Produces four objects in the bucket:
  - trust_ladder.json   -- current per-fault-class state across all THREE
                             trust dimensions (A/B/C), for the real 12-class
                             roster (the 13th, "none", is a control class,
                             excluded here same as everywhere else it's
                             filtered).
  - trust_history.json  -- every Dimension-A promotion/demotion event over
                             time (unchanged from before this rewrite).
  - episodes.json        -- full per-episode data (scores + episode_snapshots
                             joined), feeds both the case list and the
                             Replay Viewer. Also carries each episode's own
                             real dimension_{a,b,c}_before/after (2026-08-XX
                             addition) -- the exact trust_history/
                             diagnoser_mode_history/action_trust_history row
                             for THIS episode_id (each history table already
                             records episode_id directly, no timestamp
                             nearest-match needed), so the Replay Viewer can
                             show what a dimension's state genuinely was at
                             the moment this specific episode ran, not just
                             the class's current live state. NULL on any
                             dimension that never recorded a row for this
                             episode (e.g. report-only classes have no
                             Dimension A/C history at all).
  - system_status.json   -- global circuit-breaker state (read-only mirror
                             of circuit_breaker.py's own trip check, for the
                             landing page's "System Guard" indicator).

Rewritten 2026-08-06 for the new Trust Ladder frontend (mission-control
table, all 3 dimensions, max-streak, provider badge, recent-outcome bit
history, safety envelope) -- the previous version only ever covered
Dimension A for 3 hardcoded auto-fix classes. Real, deliberate scope
limit kept out of this rewrite: the tool-agreement veto (2026-08-05,
p3_agent.py) is never persisted to any table today, so the recent-
outcome bit history below can only distinguish correct/incorrect/flap,
not veto -- see wardence_frontend.md for the follow-up needed in
p3_agent.py before a veto bit can be added honestly.

Extended same day with 4 more real fields, each individually verified
against the actual code/data before being added (not invented to match
a design mockup): per-class `safety_hold` (misdispatch_guard.py's real
hold state), per-class `efficiency` (real avg tokens/Neurons per
diagnosis for the class's own primary provider, from
llm_token_usage.json), per-dimension `distance_to_peak` (current minus
max streak, pure arithmetic over data already published), and a global
`integrity_score_pct` in system_status.json (real total-failures vs.
total-episodes ratio -- NOT a reconstructed circuit-breaker trip count,
since trips aren't persisted as discrete events anywhere).

Deliberately NOT incremental/streaming -- this project's whole dataset
is small enough to just re-upload the full JSON each run rather than
build any diffing logic for it.

Usage:
    pip install -r p3_trust_action/requirements.txt
    python3 p3_trust_action/publish_to_r2.py
"""

import json
import sqlite3
import sys
from pathlib import Path

import boto3

from circuit_breaker import FAILURE_THRESHOLD, FAILURE_WINDOW_S, ensure_circuit_breaker_table
from tool_call_validator import MAX_MEMORY_LIMIT_MI, MAX_CPU_LIMIT_M, MAX_REPLICAS

sys.path.insert(0, str(Path(__file__).parent.parent / "p2_readonly_loop"))
from react_agent import FAULT_CLASSES  # noqa: E402

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"
ENV_PATH = Path(__file__).parent / ".env"
TOKEN_USAGE_PATH = Path(__file__).parent.parent / "p2_readonly_loop" / "llm_token_usage.json"

# "none" is a control class (no real fault), filtered out of every
# other published view (episodes.json, trust_ladder.json) -- same
# convention kept here.
REAL_FAULT_CLASSES = [c for c in FAULT_CLASSES if c != "none"]

# Dimension A's real promotion policy (trust_engine.PROMOTION_STREAK)
# is the authoritative list of which classes are auto-fix -- imported
# directly rather than re-hardcoded, so this file can't drift from the
# real policy the way the old AUTO_FIX_CLASSES = {3 classes} constant
# silently did.
sys.path.insert(0, str(Path(__file__).parent))
from trust_engine import PROMOTION_STREAK  # noqa: E402

AUTO_FIX_CLASSES = set(PROMOTION_STREAK.keys())

# Real ground-truth tool mapping per auto-fix class (action_proposer.
# DETERMINISTIC_ACTION_MAP without the lambda kwargs-builders, which
# this file has no use for) -- used only to know which 3 classes are
# magnitude-sensitive and what their real safety ceiling is.
MAGNITUDE_ACTION_CEILING = {
    "oom": ("patch_memory_limit", "peak_memory_mib", "limit", MAX_MEMORY_LIMIT_MI, "Mi"),
    "cpu-throttling": ("patch_cpu_limit", None, "limit", MAX_CPU_LIMIT_M, "m"),
    "under-provisioned-replicas": ("scale_deployment", None, "replicas", MAX_REPLICAS, ""),
}

# Recent-outcome bit history length shown per dimension in the mission-
# control table's sparkline -- matches the mockup's 12-bit field.
BIT_HISTORY_LEN = 12


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


def _max_and_current(conn, history_table, state_table, streak_col="streak"):
    max_rows = dict(conn.execute(
        f"SELECT fault_class, MAX(streak_after) FROM {history_table} GROUP BY fault_class"
    ).fetchall())
    current_rows = dict(conn.execute(
        f"SELECT fault_class, {streak_col} FROM {state_table}"
    ).fetchall())
    return max_rows, current_rows


def _recent_bits(conn, history_table, fault_class: str, correct_col: str = "correct") -> list[str]:
    """Real last-N outcomes for one class from an append-only history
    table, oldest-first (matches TrustMatrix.jsx's existing streak-strip
    convention: recorded_at DESC then reversed for display). "flap" is
    layered on top of a genuinely correct outcome for Dimension A only
    (durability can revert an initially-correct-looking fix); B/C have
    no durability concept of their own, so their bits are just
    correct/incorrect. Veto is NOT represented -- not persisted anywhere
    today, see module docstring."""
    rows = conn.execute(
        f"""
        SELECT episode_id, {correct_col} FROM {history_table}
        WHERE fault_class = ?
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        (fault_class, BIT_HISTORY_LEN),
    ).fetchall()
    rows = list(reversed(rows))

    bits = []
    for episode_id, correct in rows:
        if not correct:
            bits.append("incorrect")
            continue
        flapped = False
        if episode_id:
            verdict_row = conn.execute(
                "SELECT durability_verdict FROM episode_snapshots WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if verdict_row and verdict_row[0] == "flapped":
                flapped = True
        bits.append("flap" if flapped else "correct")
    return bits


def _provider_badge(conn, fault_class: str) -> "str | None":
    """Most historically-frequent provider for this class, from the
    real per-episode llm_diagnosis_log (background-comparison + live
    LLM-driven episodes both land here) -- purely retrospective, no
    claim of active routing/optimization (Stitch's original framing was
    corrected for exactly this reason, see wardence_frontend.md)."""
    row = conn.execute(
        """
        SELECT provider, COUNT(*) AS n FROM llm_diagnosis_log
        WHERE actual_class = ? AND provider IS NOT NULL
        GROUP BY provider ORDER BY n DESC LIMIT 1
        """,
        (fault_class,),
    ).fetchone()
    return row[0] if row else None


def _safety_hold(conn, fault_class: str) -> dict:
    """Real Misdispatch Safety Hold state (misdispatch_guard.py) --
    active is 1 after MISDISPATCH_HOLD_THRESHOLD (3) cross-class
    misdispatches within MISDISPATCH_HOLD_WINDOW_HOURS (24), a distinct
    mechanism from Dimension A's own demotion (never touches
    trust_state/streak). Table may not have a row yet for a class that's
    never been misdispatched -- defaults to inactive, not missing."""
    row = conn.execute(
        "SELECT active, reason, held_since FROM safety_hold WHERE fault_class = ?", (fault_class,)
    ).fetchone()
    if row is None:
        return {"active": False, "reason": None, "held_since": None}
    return {"active": bool(row[0]), "reason": row[1], "held_since": row[2]}


def _load_token_usage() -> dict:
    """Real per-class-per-provider token/Neuron usage, written by a
    separate process (llm_token_usage.json, updated every real live-LLM
    episode) -- read-only here, this publisher never writes to it."""
    if not TOKEN_USAGE_PATH.exists():
        return {}
    return json.loads(TOKEN_USAGE_PATH.read_text())


def _efficiency_index(token_usage: dict, fault_class: str, primary_provider: "str | None") -> "dict | None":
    """Real avg tokens/Neurons per diagnosis call, for this class's own
    most-frequent historical provider (the same one shown as the
    provider badge) -- deliberately NOT blended across providers, since
    Cloudflare reports Neurons and DeepInfra/Gemini/Groq report tokens,
    incompatible units that would produce a meaningless combined number.
    None if this class has no token-usage data yet, or its primary
    provider isn't a real key in llm_token_usage.json's per-class
    breakdown (e.g. provider badge derived from llm_diagnosis_log rows
    that happen to have no matching token-usage entry)."""
    class_usage = token_usage.get(fault_class)
    if not class_usage or not primary_provider:
        return None
    # llm_token_usage.json keys are "provider:model" (e.g.
    # "cloudflare:@cf/google/gemma-4-26b-a4b-it") -- _provider_badge
    # only knows the bare provider name, so match on prefix.
    matches = [v for k, v in class_usage.items() if k.startswith(f"{primary_provider}:")]
    if not matches:
        return None
    # Multiple models under the same provider (rare) -- combine by
    # weighted average over real call counts, still a single provider's
    # unit system, not a cross-provider blend.
    total_count = sum(m["count"] for m in matches)
    if total_count == 0:
        return None
    avg_tokens = sum(m["avg_tokens"] * m["count"] for m in matches) / total_count
    avg_neurons = sum(m["avg_neurons"] * m["count"] for m in matches) / total_count
    return {
        "provider": primary_provider,
        "avg_tokens": round(avg_tokens, 1),
        "avg_neurons": round(avg_neurons, 2) if avg_neurons > 0 else None,
        "sample_count": total_count,
    }


def _safety_envelope(conn, fault_class: str) -> "dict | None":
    """Real peak-usage / LLM-proposal / node-safety-ceiling triple for
    the 3 magnitude-sensitive classes, from the most recent episode that
    has both a real tool_output peak reading (where one exists) and a
    real action_result carrying the proposed magnitude. None if no such
    episode exists yet -- the frontend must not fabricate a placeholder
    envelope in that case."""
    cfg = MAGNITUDE_ACTION_CEILING.get(fault_class)
    if cfg is None:
        return None
    tool_name, peak_field, proposed_field, ceiling, unit = cfg

    row = conn.execute(
        """
        SELECT snap.tool_output, snap.action_result FROM episode_snapshots snap
        JOIN episodes e ON e.episode_id = snap.episode_id
        WHERE e.fault_class = ? AND snap.action_result IS NOT NULL
        ORDER BY e.t0 DESC LIMIT 1
        """,
        (fault_class,),
    ).fetchone()
    if row is None:
        return None

    tool_output = json.loads(row[0]) if row[0] else {}
    action_result = json.loads(row[1]) if row[1] else {}

    peak = tool_output.get(peak_field) if peak_field else None
    proposed_raw = action_result.get(proposed_field)
    if proposed_raw is None:
        return None

    return {
        "tool_name": tool_name,
        "peak_observed": peak,
        "proposed": proposed_raw,
        "ceiling": ceiling,
        "unit": unit,
    }


def _distance_to_peak(current: "int | None", peak: "int | None") -> "int | None":
    """current - peak, always <= 0 -- 0 means currently AT its all-time
    high, a negative number is real real distance fallen from it. None
    if either side is unknown (report-only classes have no Dimension-A
    streak at all)."""
    if current is None or peak is None:
        return None
    return current - peak


def build_trust_ladder(conn: sqlite3.Connection) -> list[dict]:
    ladder = []
    token_usage = _load_token_usage()

    a_max, a_cur = _max_and_current(conn, "trust_history", "trust_state", "streak")
    trust_state_rows = dict(conn.execute("SELECT fault_class, state FROM trust_state").fetchall())

    b_max, b_cur = _max_and_current(conn, "diagnoser_mode_history", "diagnoser_mode", "streak")
    diagnoser_mode_rows = dict(conn.execute("SELECT fault_class, mode FROM diagnoser_mode").fetchall())

    c_max, c_cur = _max_and_current(conn, "action_trust_history", "action_trust", "streak")
    action_trust_rows = dict(conn.execute("SELECT fault_class, state FROM action_trust").fetchall())

    for fault_class in REAL_FAULT_CLASSES:
        total_correct_row = conn.execute(
            "SELECT COUNT(*), SUM(correct) FROM scores "
            "WHERE actual_class = ? AND (phase_e_status IS NULL OR phase_e_status != 'excluded')",
            (fault_class,),
        ).fetchone()
        total, correct = total_correct_row[0], total_correct_row[1] or 0
        diagnosis_accuracy = correct / total if total else None
        is_auto_fix = fault_class in AUTO_FIX_CLASSES
        provider = _provider_badge(conn, fault_class)

        a_streak = a_cur.get(fault_class, 0) if is_auto_fix else None
        a_peak = a_max.get(fault_class, 0) if is_auto_fix else None
        b_streak = b_cur.get(fault_class, 0)
        b_peak = b_max.get(fault_class, 0)
        c_streak = c_cur.get(fault_class, 0)
        c_peak = c_max.get(fault_class, 0)

        entry = {
            "fault_class": fault_class,
            "tier": "auto-fix" if is_auto_fix else "report-only",
            "diagnosis_accuracy": diagnosis_accuracy,
            "episodes_scored": total,
            "provider": provider,
            "efficiency": _efficiency_index(token_usage, fault_class, provider),
            "safety_hold": _safety_hold(conn, fault_class),
            "dimension_a": {
                "state": trust_state_rows.get(fault_class, "report_only") if is_auto_fix else "report_only",
                "streak": a_streak,
                "max_streak": a_peak,
                "distance_to_peak": _distance_to_peak(a_streak, a_peak),
                "recent": _recent_bits(conn, "trust_history", fault_class) if is_auto_fix else [],
            },
            "dimension_b": {
                "mode": diagnoser_mode_rows.get(fault_class, "stub"),
                "streak": b_streak,
                "max_streak": b_peak,
                "distance_to_peak": _distance_to_peak(b_streak, b_peak),
                "recent": _recent_bits(conn, "diagnoser_mode_history", fault_class),
            },
            "dimension_c": {
                "state": action_trust_rows.get(fault_class, "deterministic_fallback"),
                "streak": c_streak,
                "max_streak": c_peak,
                "distance_to_peak": _distance_to_peak(c_streak, c_peak),
                "recent": _recent_bits(conn, "action_trust_history", fault_class),
            },
            "safety_envelope": _safety_envelope(conn, fault_class) if is_auto_fix else None,
        }
        ladder.append(entry)

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
            snap.durability_elapsed_s, snap.gate_substitution,
            snap.provider, snap.model, snap.tier,
            snap.transcript_json, snap.observations_json, snap.failed_attempts_json,
            (SELECT state_before FROM trust_history WHERE episode_id = e.episode_id ORDER BY id DESC LIMIT 1) AS dimension_a_before,
            (SELECT state_after  FROM trust_history WHERE episode_id = e.episode_id ORDER BY id DESC LIMIT 1) AS dimension_a_after,
            (SELECT mode_before FROM diagnoser_mode_history WHERE episode_id = e.episode_id ORDER BY id DESC LIMIT 1) AS dimension_b_before,
            (SELECT mode_after  FROM diagnoser_mode_history WHERE episode_id = e.episode_id ORDER BY id DESC LIMIT 1) AS dimension_b_after,
            (SELECT state_before FROM action_trust_history WHERE episode_id = e.episode_id ORDER BY id DESC LIMIT 1) AS dimension_c_before,
            (SELECT state_after  FROM action_trust_history WHERE episode_id = e.episode_id ORDER BY id DESC LIMIT 1) AS dimension_c_after
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
        # Real turn-by-turn trace (2026-08-05 addition) -- None on every
        # episode scored before this shipped, never backfilled/faked.
        # The frontend must check for these before rendering the
        # enriched (multi-turn) Replay Viewer story, falling back to the
        # lite (flat tool_output + final reasoning) story otherwise.
        if d.get("transcript_json"):
            d["transcript"] = json.loads(d.pop("transcript_json"))
        else:
            d["transcript"] = None
            d.pop("transcript_json", None)
        if d.get("observations_json"):
            d["observations"] = json.loads(d.pop("observations_json"))
        else:
            d["observations"] = None
            d.pop("observations_json", None)
        if d.get("failed_attempts_json"):
            d["failed_attempts"] = json.loads(d.pop("failed_attempts_json"))
        else:
            d["failed_attempts"] = None
            d.pop("failed_attempts_json", None)
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

    # Real System Integrity Score -- deliberately NOT a reconstructed
    # "number of times the breaker actually tripped" (failure_log has no
    # discrete trip-event table; a trip is a computed threshold check,
    # not a stored row -- reconstructing exact trip windows would need
    # non-trivial sliding-window replay logic for a number this project
    # doesn't already track anywhere). Instead: real total failure-log
    # rows vs. real total scored episodes, i.e. "what fraction of all
    # episodes ever run did NOT produce a circuit-breaker-relevant
    # failure." A simpler, equally honest global signal, not a proxy for
    # a number that isn't actually persisted.
    total_failures = conn.execute("SELECT COUNT(*) FROM failure_log").fetchone()[0]
    total_episodes = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    integrity_score = (1 - total_failures / total_episodes) * 100 if total_episodes else None

    return {
        "recent_failures": recent_failures,
        "tripped": recent_failures >= FAILURE_THRESHOLD,
        "failure_window_s": FAILURE_WINDOW_S,
        "failure_threshold": FAILURE_THRESHOLD,
        "total_failures": total_failures,
        "total_episodes": total_episodes,
        "integrity_score_pct": round(integrity_score, 2) if integrity_score is not None else None,
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
