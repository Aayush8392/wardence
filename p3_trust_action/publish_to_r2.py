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
  - model_scorecard.json -- per-(provider,model) data for the Calibration/
                             Model Scorecard redesign (Kimi review 27,
                             2026-08-1x): confusion matrix, overall
                             accuracy, per-tier (primary/fallback)
                             resolution share + accuracy, report-only-class
                             overreach events, confidence stdev + mean-
                             deviation calibration error (both split
                             logprob-vs-self-reported), cross-class
                             token/Neuron efficiency (with a real zero-
                             token-entry exclusion fix -- see
                             build_model_efficiency's docstring), real
                             cost-per-correct-diagnosis, real avg response
                             time, and real auto-fix-class action
                             durability rate.
  - radar_dossier.json    -- per-fault-class 6-axis data for the Operator
                             tab's Trust Dossier radar chart (see
                             build_radar_dossier's own docstring for the
                             locked axis set and what was rejected).

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
CALIBRATION_CURVES_PATH = Path(__file__).parent.parent / "p2_readonly_loop" / "calibration_curves.json"

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


def _load_calibration_curves() -> dict:
    """Real empirical calibration curves for self-reported-confidence
    providers (Groq/OpenRouter), written by a separate offline process
    (p2_readonly_loop/calibration_refit.py, run manually, not on every
    publish) -- read-only here, same convention as _load_token_usage.
    Empty dict (not an error) if the refit script hasn't been run yet on
    this machine, or hasn't found any real self-reported data."""
    if not CALIBRATION_CURVES_PATH.exists():
        return {}
    return json.loads(CALIBRATION_CURVES_PATH.read_text())


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


def _is_correct(predicted: "str | None", actual: str) -> bool:
    """Same equivalence rule used everywhere else in this project
    (calibration_refit.py, diag_4way_provider_accuracy_by_class.py) --
    "none" (ground truth) and the diagnoser's own "no anomaly detected"
    string are the same real outcome."""
    if predicted == actual:
        return True
    return predicted in ("none", "no anomaly detected") and actual in ("none", "no anomaly detected")


def _model_rows(conn: sqlite3.Connection) -> list[dict]:
    """Real per-episode (provider, model, actual_class, predicted,
    confidence, confidence_source, response_time_ms) rows from BOTH real
    sources, unioned -- same join pattern as
    diag_4way_provider_accuracy_by_class.py (verified directly against
    that script before reuse here): llm_diagnosis_log already carries
    ground truth inline; comparison_sampling_log is deliberately
    blinded at write time and needs a join to episodes.fault_class for
    the real answer. response_time_ms confirmed present on both tables
    (llm_replay_test.py / p3_agent.py, both added 2026-08-05) before
    being read here."""
    rows = []
    for actual, provider, model, predicted, confidence, conf_source, rt_ms in conn.execute(
        """
        SELECT actual_class, provider, model, llm_diagnosis, llm_confidence, llm_confidence_source,
               response_time_ms
        FROM llm_diagnosis_log
        WHERE provider IS NOT NULL AND model IS NOT NULL AND llm_diagnosis IS NOT NULL
        """
    ).fetchall():
        rows.append({
            "actual_class": actual, "provider": provider, "model": model,
            "predicted": predicted, "confidence": confidence, "confidence_source": conf_source,
            "response_time_ms": rt_ms,
        })

    for actual, provider, model, predicted, confidence, conf_source, rt_ms in conn.execute(
        """
        SELECT e.fault_class AS actual_class, c.provider, c.model, c.diagnosis,
               c.confidence, c.confidence_source, c.response_time_ms
        FROM comparison_sampling_log c
        JOIN episodes e ON e.episode_id = c.episode_id
        WHERE c.provider IS NOT NULL AND c.model IS NOT NULL AND c.diagnosis IS NOT NULL
        """
    ).fetchall():
        rows.append({
            "actual_class": actual, "provider": provider, "model": model,
            "predicted": predicted, "confidence": confidence, "confidence_source": conf_source,
            "response_time_ms": rt_ms,
        })

    return rows


def build_confusion_matrix(conn: sqlite3.Connection) -> dict:
    """Real predicted-vs-actual counts per (provider, model), from
    diagnosis-only data (no action outcome involved) -- Kimi review 27
    item A. Source tables/join verified directly against
    diag_4way_provider_accuracy_by_class.py before building (no source
    table was pinned in the original review). Keyed by "provider:model"
    -> {actual_class: {predicted_class: count}}, sparse (only real
    observed pairs are present, no zero-filled grid) since the frontend
    already knows the full class roster and can zero-fill for display."""
    matrix: dict[str, dict[str, dict[str, int]]] = {}
    for r in _model_rows(conn):
        key = f"{r['provider']}:{r['model']}"
        matrix.setdefault(key, {}).setdefault(r["actual_class"], {})
        row = matrix[key][r["actual_class"]]
        row[r["predicted"]] = row.get(r["predicted"], 0) + 1
    return matrix


def build_tier_accuracy(conn: sqlite3.Connection) -> dict:
    """Real % of episodes resolved per tier (primary/fallback) + real
    accuracy within each tier, from episode_snapshots.tier (shipped
    2026-08-05) -- Kimi review 27 item D. Only episodes with a real,
    non-NULL tier are counted (pre-2026-08-05 episodes never got this
    column populated; NULL is excluded, never treated as a real tier).
    Also broken down per (tier, provider) since two providers can share
    a tier (Gemma/Nemotron are both tier=primary)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT snap.tier, snap.provider, s.predicted_class, s.actual_class
        FROM episode_snapshots snap
        JOIN scores s ON s.episode_id = snap.episode_id
        WHERE snap.tier IS NOT NULL AND snap.provider IS NOT NULL
        """
    ).fetchall()
    conn.row_factory = None

    by_tier: dict[str, dict] = {}
    by_tier_provider: dict[str, dict] = {}
    total_resolved = len(rows)

    for row in rows:
        tier, provider = row["tier"], row["provider"]
        correct = _is_correct(row["predicted_class"], row["actual_class"])

        t = by_tier.setdefault(tier, {"resolved": 0, "correct": 0})
        t["resolved"] += 1
        t["correct"] += 1 if correct else 0

        tp_key = f"{tier}:{provider}"
        tp = by_tier_provider.setdefault(tp_key, {"tier": tier, "provider": provider, "resolved": 0, "correct": 0})
        tp["resolved"] += 1
        tp["correct"] += 1 if correct else 0

    return {
        "total_resolved_with_tier": total_resolved,
        "by_tier": {
            tier: {
                "resolved": v["resolved"],
                "pct_of_total": round(100 * v["resolved"] / total_resolved, 1) if total_resolved else None,
                "accuracy": round(v["correct"] / v["resolved"], 4) if v["resolved"] else None,
            }
            for tier, v in by_tier.items()
        },
        "by_tier_provider": {
            k: {
                "tier": v["tier"], "provider": v["provider"], "resolved": v["resolved"],
                "pct_of_total": round(100 * v["resolved"] / total_resolved, 1) if total_resolved else None,
                "accuracy": round(v["correct"] / v["resolved"], 4) if v["resolved"] else None,
            }
            for k, v in by_tier_provider.items()
        },
    }


def build_overreach_flags(conn: sqlite3.Connection, report_only_classes: set) -> dict:
    """Real overreach events per (provider, model) -- Kimi review 27
    item F, exact wording "report-only classes where action_result !=
    'none'". Checked directly against real schema: there is no literal
    string "none" written anywhere -- a no-action episode has
    action_result IS NULL (p3_agent.py's response dict default,
    p3_scorer.py's INSERT). The real, equivalent check is: for a
    report-only-class episode, action_result IS NOT NULL at all --
    since eligibility for dispatch (p3_agent.py's
    eligible_for_action = trust_state=="can_act" and not safety_hold)
    is gated on Dimension A, and report-only classes never reach
    can_act, any non-NULL action_result there is a genuine overreach,
    not a labeling quirk."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT snap.provider, snap.model, e.fault_class, snap.action_result
        FROM episode_snapshots snap
        JOIN episodes e ON e.episode_id = snap.episode_id
        WHERE snap.provider IS NOT NULL AND snap.model IS NOT NULL
        """
    ).fetchall()
    conn.row_factory = None

    tally: dict[str, dict] = {}
    for row in rows:
        if row["fault_class"] not in report_only_classes:
            continue
        key = f"{row['provider']}:{row['model']}"
        t = tally.setdefault(key, {"report_only_episodes": 0, "overreach_events": 0})
        t["report_only_episodes"] += 1
        if row["action_result"] is not None:
            t["overreach_events"] += 1

    return {
        key: {
            "report_only_episodes": v["report_only_episodes"],
            "overreach_events": v["overreach_events"],
            "overreach_rate": round(v["overreach_events"] / v["report_only_episodes"], 4) if v["report_only_episodes"] else None,
        }
        for key, v in tally.items()
    }


def build_confidence_variance(conn: sqlite3.Connection) -> dict:
    """Real stdev(confidence) per (provider, model), split logprob vs.
    self-reported (never blended -- same rule already locked for
    ECE/Brier elsewhere in this project) -- Kimi review 27 item H.
    Population stdev (not sample), consistent with this project not
    treating these episodes as a sample of a larger unobserved
    population -- they ARE the full observed set for this cohort so
    far. None (not 0.0) for a group with <2 real observations, since a
    variance computed on 0-1 points is not a real signal."""
    import statistics

    groups: dict[str, dict[str, list[float]]] = {}
    for r in _model_rows(conn):
        if r["confidence"] is None or r["confidence_source"] not in ("logprob", "self_reported"):
            continue
        key = f"{r['provider']}:{r['model']}"
        groups.setdefault(key, {"logprob": [], "self_reported": []})
        groups[key][r["confidence_source"]].append(r["confidence"])

    out = {}
    for key, by_source in groups.items():
        out[key] = {}
        for source, values in by_source.items():
            out[key][source] = {
                "n": len(values),
                "mean": round(statistics.mean(values), 4) if values else None,
                "stdev": round(statistics.pstdev(values), 4) if len(values) >= 2 else None,
            }
    return out


def build_model_efficiency(conn: sqlite3.Connection) -> dict:
    """Real cross-provider token/Neuron efficiency, all classes
    combined per (provider, model) -- distinct from the existing
    per-class _efficiency_index (which only ever resolves one class's
    single primary provider). Reads the same llm_token_usage.json file,
    but aggregates across every class a (provider, model) appears in.

    Real bug fix (found live, 2026-08-1x session): several
    provider:model entries in llm_token_usage.json have count > 0 but
    total_tokens == 0 (confirmed instance: network-partition's
    groq:openai/gpt-oss-120b, 9 real attempts, all logged tokens=0) --
    these are calls whose response never carried a usage field (almost
    always a failed/quota-exhausted call, per quota_tracker.py's
    record_call docstring), NOT a genuine zero-cost diagnosis. Any such
    entry is excluded from the average entirely, not counted as free."""
    token_usage = _load_token_usage()

    totals: dict[str, dict] = {}
    for fault_class, models in token_usage.items():
        for key, stats in models.items():
            count = stats.get("count", 0)
            total_tokens = stats.get("total_tokens", 0)
            total_neurons = stats.get("avg_neurons", 0) * count
            if count <= 0:
                continue
            if total_tokens == 0 and total_neurons == 0:
                # No real cost data for this cohort -- excluded, not zero.
                continue
            t = totals.setdefault(key, {"count": 0, "total_tokens": 0, "total_neurons": 0.0})
            t["count"] += count
            t["total_tokens"] += total_tokens
            t["total_neurons"] += total_neurons

    return {
        key: {
            "sample_count": v["count"],
            "avg_tokens": round(v["total_tokens"] / v["count"], 1) if v["total_tokens"] else None,
            "avg_neurons": round(v["total_neurons"] / v["count"], 2) if v["total_neurons"] else None,
        }
        for key, v in totals.items()
    }


def build_overall_accuracy(confusion_matrix: dict) -> dict:
    """Real accuracy per (provider, model), diagonal-sum over the real
    confusion matrix already built above (no new query needed -- pure
    arithmetic over data this file already computed). "Diagonal" here
    uses the same _is_correct equivalence as everywhere else (a
    predicted "none"/"no anomaly detected" against an actual "none"
    counts as correct), not literal predicted==actual string match."""
    out = {}
    for key, by_actual in confusion_matrix.items():
        total = 0
        correct = 0
        for actual_class, by_predicted in by_actual.items():
            for predicted_class, count in by_predicted.items():
                total += count
                if _is_correct(predicted_class, actual_class):
                    correct += count
        out[key] = {
            "total_episodes": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total else None,
        }
    return out


def build_calibration_error(conn: sqlite3.Connection, overall_accuracy: dict) -> dict:
    """Real Kimi review 27 item 27's "ranked deviation from zero"
    metric: |mean(self-reported confidence) - accuracy| per (provider,
    model), split logprob vs. self-reported (never blended, same rule
    already used for confidence_variance/ECE elsewhere). accuracy here
    is the model's OVERALL accuracy (from build_overall_accuracy), not
    a per-source accuracy -- Kimi's own definition compares confidence
    against real outcome accuracy, not against a confidence-source
    subgroup's own accuracy (which would be circular for a
    single-source model)."""
    import statistics

    groups: dict[str, dict[str, list[float]]] = {}
    for r in _model_rows(conn):
        if r["confidence"] is None or r["confidence_source"] not in ("logprob", "self_reported"):
            continue
        key = f"{r['provider']}:{r['model']}"
        groups.setdefault(key, {"logprob": [], "self_reported": []})
        groups[key][r["confidence_source"]].append(r["confidence"])

    out = {}
    for key, by_source in groups.items():
        acc = overall_accuracy.get(key, {}).get("accuracy")
        out[key] = {}
        for source, values in by_source.items():
            if not values or acc is None:
                out[key][source] = {"n": len(values), "mean_confidence": None, "deviation": None}
                continue
            mean_conf = statistics.mean(values)
            out[key][source] = {
                "n": len(values),
                "mean_confidence": round(mean_conf, 4),
                "deviation": round(abs(mean_conf - acc), 4),
            }
    return out


def build_avg_response_time(conn: sqlite3.Connection) -> dict:
    """Real avg wall-clock response_time_ms per (provider, model),
    across all classes -- powers a real accuracy-vs-latency efficiency
    scatter instead of the mockups' fabricated dollar-cost axis."""
    times: dict[str, list[float]] = {}
    for r in _model_rows(conn):
        if r["response_time_ms"] is None:
            continue
        key = f"{r['provider']}:{r['model']}"
        times.setdefault(key, []).append(r["response_time_ms"])

    return {
        key: {"n": len(values), "avg_response_time_ms": round(sum(values) / len(values), 1)}
        for key, values in times.items()
    }


def build_avg_response_time_by_class(conn: sqlite3.Connection) -> dict:
    """Real avg wall-clock response_time_ms per (provider, model, class)
    -- powers the real 48-cohort (class x model) Efficiency Frontier
    scatter Kimi review 27 recommended over the thin 4-5-point
    model-level aggregate (build_avg_response_time above, kept as-is
    for the model card table's single summary number -- this is an
    addition, not a replacement). Keyed the same way as
    confusion_matrix (model -> actual_class -> ...) so the frontend can
    pair a real per-class accuracy (already derivable from
    confusion_matrix's own per-row diagonal) with a real per-class
    latency without a second new field for accuracy."""
    times: dict[str, dict[str, list[float]]] = {}
    for r in _model_rows(conn):
        if r["response_time_ms"] is None:
            continue
        key = f"{r['provider']}:{r['model']}"
        times.setdefault(key, {}).setdefault(r["actual_class"], []).append(r["response_time_ms"])

    return {
        key: {
            cls: {"n": len(values), "avg_response_time_ms": round(sum(values) / len(values), 1)}
            for cls, values in by_class.items()
        }
        for key, by_class in times.items()
    }


def build_durability_rate(conn: sqlite3.Connection, auto_fix_classes: set) -> dict:
    """Real % of dispatched actions that HELD (not flapped/failed) per
    (provider, model) -- auto-fix classes only, since report-only
    classes never dispatch a real action to have a durability verdict
    in the first place. Kimi review 27 item E ("Action durability
    rate") -- paired with build_overall_accuracy's diagnostic accuracy
    at render time (two real columns) rather than collapsed into a
    single fabricated "Action Bias" scalar, per Kimi's own explicit
    recommendation against building that as one number."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT snap.provider, snap.model, e.fault_class, snap.durability_verdict
        FROM episode_snapshots snap
        JOIN episodes e ON e.episode_id = snap.episode_id
        WHERE snap.provider IS NOT NULL AND snap.model IS NOT NULL
          AND snap.durability_verdict IS NOT NULL
        """
    ).fetchall()
    conn.row_factory = None

    tally: dict[str, dict] = {}
    for row in rows:
        if row["fault_class"] not in auto_fix_classes:
            continue
        key = f"{row['provider']}:{row['model']}"
        t = tally.setdefault(key, {"total": 0, "held": 0})
        t["total"] += 1
        if row["durability_verdict"] not in ("flapped",):
            t["held"] += 1

    return {
        key: {
            "total_actions": v["total"],
            "held": v["held"],
            "durability_rate": round(v["held"] / v["total"], 4) if v["total"] else None,
        }
        for key, v in tally.items()
    }


def build_cost_per_correct(overall_accuracy: dict, efficiency: dict) -> dict:
    """Real Kimi review 27 item B: cost-per-correct-diagnosis, derived
    from fields already built above (no new query) -- avg_tokens (or
    avg_neurons) divided by accuracy, i.e. "how much a real correct
    diagnosis actually costs once wrong/wasted attempts are accounted
    for," not just the flat avg-per-call cost efficiency already
    published. Approximate, not an exact per-episode join (llm_token_
    usage.json is pre-aggregated per class-provider, not per episode)
    -- documented as such, not presented as exact."""
    out = {}
    for key, acc_entry in overall_accuracy.items():
        acc = acc_entry.get("accuracy")
        eff = efficiency.get(key)
        if acc is None or acc <= 0 or eff is None:
            out[key] = {"tokens_per_correct": None, "neurons_per_correct": None}
            continue
        avg_tokens = eff.get("avg_tokens")
        avg_neurons = eff.get("avg_neurons")
        out[key] = {
            "tokens_per_correct": round(avg_tokens / acc, 1) if avg_tokens else None,
            "neurons_per_correct": round(avg_neurons / acc, 2) if avg_neurons else None,
        }
    return out


def build_comparison_episodes(conn: sqlite3.Connection) -> list[dict]:
    """Real per-sample data for comparison-only models (Groq/OpenRouter --
    never actually dispatched, see episode_snapshots/llm_diagnosis_log,
    but real diagnoses via the always-on background comparison system,
    comparison_sampling_log). Published separately from episodes.json
    (real dispatched episodes only) rather than merged into it -- a
    comparison sample has no action_taken/action_result/tool_output/
    durability_verdict at all (it never acted), and episodes.json's
    other real consumers (Replay Viewer's dot-sphere/CaseFile) assume
    every entry IS a real dispatch outcome. Mixing the two shapes into
    one file would silently misrepresent comparison-only samples there.

    Real gap this closes: CalibrationSection.jsx's per-model deviation
    bars already include Groq/OpenRouter (sourced from the wider
    _model_rows() union), but clicking to expand into the scatter
    drill-down found zero matches in episodes.json for those two
    providers -- an empty graph, since they have no episode_snapshots
    rows. This publishes the real comparison-sample data the drill-down
    needs to actually render something for them."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            c.episode_id, c.provider, c.model, c.diagnosis AS predicted_class,
            c.confidence AS score_confidence, c.confidence_source, c.reasoning,
            c.response_time_ms, c.completed_at,
            e.fault_class, e.target, e.t0
        FROM comparison_sampling_log c
        JOIN episodes e ON e.episode_id = c.episode_id
        WHERE c.diagnosis IS NOT NULL
        """
    ).fetchall()
    conn.row_factory = None

    out = []
    for row in rows:
        d = dict(row)
        d["correct"] = _is_correct(d["predicted_class"], d["fault_class"])
        out.append(d)
    return out


def build_model_scorecard(conn: sqlite3.Connection) -> dict:
    """Real Kimi review 27 (Calibration/Model Scorecard redesign) data,
    published as its own file since it's per-model, not per-class like
    trust_ladder.json. Nothing here is fabricated to fit a mockup --
    every field traces to a real table/column, verified before writing.
    Deliberately NOT built (per Kimi's own explicit advice, see review
    27): a radar/spider chart (normalization-crime trap across
    incompatible axes), a single composite "Action Bias" scalar
    (replaced by the raw diagnostic-accuracy/durability-rate pair), and
    any live "calibration drift" banner (that thread stays deferred
    indefinitely per wardence_context.md's temporal-drift section --
    real reason: calendar span too short to distinguish drift from
    noise, not a data-availability gap this file can close)."""
    report_only_classes = set(REAL_FAULT_CLASSES) - AUTO_FIX_CLASSES

    confusion_matrix = build_confusion_matrix(conn)
    overall_accuracy = build_overall_accuracy(confusion_matrix)
    efficiency = build_model_efficiency(conn)

    return {
        "confusion_matrix": confusion_matrix,
        "overall_accuracy": overall_accuracy,
        "tier_accuracy": build_tier_accuracy(conn),
        "overreach": build_overreach_flags(conn, report_only_classes),
        "confidence_variance": build_confidence_variance(conn),
        "calibration_error": build_calibration_error(conn, overall_accuracy),
        "efficiency": efficiency,
        "avg_response_time": build_avg_response_time(conn),
        "avg_response_time_by_class": build_avg_response_time_by_class(conn),
        "durability_rate": build_durability_rate(conn, AUTO_FIX_CLASSES),
        "cost_per_correct": build_cost_per_correct(overall_accuracy, efficiency),
    }


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


def build_action_accuracy_by_class(conn: sqlite3.Connection, auto_fix_classes: set) -> dict:
    """Real % of Dimension-C outcome recordings that were "correct" (per
    record_action_outcome's own `correct` column -- diagnosis AND action
    both correct, exact-match for the 3 magnitude-sensitive actions,
    tool-only-match for the other 3, per the split already applied by
    p3_agent.py before calling it) per fault class. Auto-fix classes
    only -- report-only classes never call record_action_outcome at all,
    so action_trust_history has zero rows for them; None (never 0), same
    "None means never measured" convention used everywhere else in this
    file (e.g. dimension_a fields for report-only classes in
    build_trust_ladder)."""
    rows = conn.execute(
        "SELECT fault_class, COUNT(*), SUM(correct) FROM action_trust_history GROUP BY fault_class"
    ).fetchall()
    tally = {fc: (n, c or 0) for fc, n, c in rows}
    out = {}
    for fc in REAL_FAULT_CLASSES:
        if fc not in auto_fix_classes:
            out[fc] = None
            continue
        n, c = tally.get(fc, (0, 0))
        out[fc] = round(c / n, 4) if n else None
    return out


def build_response_time_by_class(conn: sqlite3.Connection) -> dict:
    """Real avg response_time_ms per fault class, collapsed across every
    provider -- same _model_rows source as the rest of this file's
    per-model metrics, just regrouped by actual_class instead of
    provider:model. Universal: every diagnosis call has a response time
    regardless of whether the class is auto-fix or report-only."""
    times: dict[str, list[float]] = {}
    for r in _model_rows(conn):
        if r["response_time_ms"] is None:
            continue
        times.setdefault(r["actual_class"], []).append(r["response_time_ms"])
    return {
        fc: (round(sum(times[fc]) / len(times[fc]), 1) if times.get(fc) else None)
        for fc in REAL_FAULT_CLASSES
    }


def build_confidence_variance_by_class(conn: sqlite3.Connection) -> dict:
    """Real logprob-source confidence stdev per fault class -- Operator
    Trust Dossier radar axis, locked 2026-08-1x after a direct real-data
    check (check_confidence_variance_by_class.py, one-off, not committed)
    found genuine per-class spread on the logprob side (stdev ranged
    0.035-0.062 across classes) versus a near-flat self-reported side
    (~0.98 everywhere, two outliers) that alone would've been as
    uninformative as the durability-rate axis this replaced. Locked:
    logprob source only, never blended with self-reported -- same rule
    already used for build_calibration_error/build_confidence_variance
    elsewhere in this file. None for a class with <2 real logprob
    observations (a variance over 0-1 points isn't a real signal, same
    guard as the existing per-model version)."""
    import statistics

    groups: dict[str, list[float]] = {}
    for r in _model_rows(conn):
        if r["confidence"] is None or r["confidence_source"] != "logprob":
            continue
        groups.setdefault(r["actual_class"], []).append(r["confidence"])
    return {
        fc: (round(statistics.pstdev(groups[fc]), 4) if len(groups.get(fc, [])) >= 2 else None)
        for fc in REAL_FAULT_CLASSES
    }


def build_calibration_deviation_by_class(conn: sqlite3.Connection, diagnosis_accuracy: dict) -> dict:
    """Real |mean(logprob confidence) - real diagnostic accuracy| per
    fault class -- logprob-source only, same reasoning and same rule as
    build_confidence_variance_by_class above (self-reported is flat
    ~0.98 everywhere, would just measure "distance from 0.98" per class
    rather than real calibration behavior). Compared against the
    canonical per-class scores-table accuracy passed in (the same value
    the radar's own diagnosis_accuracy axis uses), not
    build_calibration_error's confusion-matrix-derived accuracy -- kept
    internally consistent within this one chart rather than reusing that
    function's slightly different source."""
    import statistics

    groups: dict[str, list[float]] = {}
    for r in _model_rows(conn):
        if r["confidence"] is None or r["confidence_source"] != "logprob":
            continue
        groups.setdefault(r["actual_class"], []).append(r["confidence"])

    out = {}
    for fc in REAL_FAULT_CLASSES:
        acc = diagnosis_accuracy.get(fc)
        values = groups.get(fc)
        if not values or acc is None:
            out[fc] = None
            continue
        out[fc] = round(abs(statistics.mean(values) - acc), 4)
    return out


def build_radar_dossier(conn: sqlite3.Connection) -> dict:
    """Real per-class 6-axis data for the Operator tab's Trust Dossier
    radar chart (EpisodePanel.jsx's currently-placeholder collapsible
    section). Locked axis set, 2026-08-1x, after ruling out: durability
    rate (near-always 100% -- flapping is rare, would read as a flat
    spike on every class); time-to-verified (structurally computed only
    when an action is dispatched -- verify_durability never runs for
    report-only classes, whose faults are reverted by the injector
    script, not verified as a trustworthy agent fix -- would have made 2
    of 6 axes auto-fix-only); tier reliance (rejected as redundant --
    fallback tiers are rarely exercised now that overnight batches have
    stopped, Cloudflare primary handles nearly everything); evidence-
    gathering tool-call depth (real but coarse, MAX_TURNS=5 caps it at
    1-5 discrete values, expected clustered at 1-2 for most classes).
    Final 6 axes, 5 universal + exactly 1 auto-fix-only:
      - diagnosis_accuracy     universal, canonical scores-table accuracy
      - action_accuracy        auto-fix only, None for report-only classes
      - calibration_deviation  universal, logprob-source only
      - dimension_b_streak     universal (Dimension B tracks every class)
      - avg_response_time_ms   universal
      - confidence_stdev       universal, logprob-source only
    """
    diagnosis_accuracy: dict[str, "float | None"] = {}
    for fc in REAL_FAULT_CLASSES:
        row = conn.execute(
            "SELECT COUNT(*), SUM(correct) FROM scores "
            "WHERE actual_class = ? AND (phase_e_status IS NULL OR phase_e_status != 'excluded')",
            (fc,),
        ).fetchone()
        total, correct = row[0], row[1] or 0
        diagnosis_accuracy[fc] = round(correct / total, 4) if total else None

    action_accuracy = build_action_accuracy_by_class(conn, AUTO_FIX_CLASSES)
    calibration_deviation = build_calibration_deviation_by_class(conn, diagnosis_accuracy)
    response_time = build_response_time_by_class(conn)
    confidence_stdev = build_confidence_variance_by_class(conn)
    b_max, b_cur = _max_and_current(conn, "diagnoser_mode_history", "diagnoser_mode", "streak")

    dossier = {}
    for fc in REAL_FAULT_CLASSES:
        dossier[fc] = {
            "tier": "auto-fix" if fc in AUTO_FIX_CLASSES else "report-only",
            "diagnosis_accuracy": diagnosis_accuracy.get(fc),
            "action_accuracy": action_accuracy.get(fc),
            "calibration_deviation": calibration_deviation.get(fc),
            "dimension_b_streak": b_cur.get(fc, 0),
            "dimension_b_max_streak": b_max.get(fc, 0),
            "avg_response_time_ms": response_time.get(fc),
            "confidence_stdev": confidence_stdev.get(fc),
        }
    return dossier


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
    calibration_curves = _load_calibration_curves()
    model_scorecard = build_model_scorecard(conn)
    comparison_episodes = build_comparison_episodes(conn)
    radar_dossier = build_radar_dossier(conn)

    conn.close()

    upload_json(client, bucket, "trust_ladder.json", trust_ladder)
    upload_json(client, bucket, "trust_history.json", trust_history)
    upload_json(client, bucket, "episodes.json", episodes)
    upload_json(client, bucket, "system_status.json", system_status)
    upload_json(client, bucket, "model_scorecard.json", model_scorecard)
    upload_json(client, bucket, "comparison_episodes.json", comparison_episodes)
    upload_json(client, bucket, "radar_dossier.json", radar_dossier)
    if calibration_curves:
        upload_json(client, bucket, "calibration_curves.json", calibration_curves)
    else:
        print("calibration_curves.json not found locally -- skipping upload "
              "(run p2_readonly_loop/calibration_refit.py first if this is unexpected).")


if __name__ == "__main__":
    main()
