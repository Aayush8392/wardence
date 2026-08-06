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
    python3 p3_scorer.py                       # scores the most recent unscored episode
    python3 p3_scorer.py --episode-id <id>      # scores that SPECIFIC episode (used by operator_api.py)
"""

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "p2_readonly_loop"))

from circuit_breaker import ensure_circuit_breaker_table, record_failure  # noqa: E402
from trust_engine import DB_PATH, PROMOTION_STREAK, ensure_trust_tables, record_outcome  # noqa: E402
from verifier import verify_durability  # noqa: E402
from llm_trust_state import (  # noqa: E402
    ensure_llm_trust_tables, record_action_outcome, record_diagnoser_outcome,
)
from action_proposer import DETERMINISTIC_ACTION_MAP, log_proposal  # noqa: E402
from llm_replay_test import _same_diagnosis, ensure_llm_diagnosis_log_table  # noqa: E402
from trust_gate import action_is_correct, eligible_action_for_streak, eligible_for_trust_ladder  # noqa: E402
from misdispatch_guard import ensure_misdispatch_tables, record_misdispatch  # noqa: E402

DIAGNOSE_URL = "http://localhost:8001/diagnose"
ACT_URL = "http://localhost:8001/act"

# Split into two independently-timed calls, 2026-07-31 (Kimi review 13's
# locked decision, see p3_agent.py's module docstring for the full
# reasoning): a slow/stuck action-proposal call can no longer also blow
# out the diagnosis call's budget, or vice versa. Diagnosis keeps the
# old combined-call's 180s -- still the heavier of the two (a full
# 5-turn ReAct loop, with episode-scoped retry across the whole
# provider chain on failure).
#
# ACT_TIMEOUT_S must cover BOTH real components of /act, not just the
# LLM side: action_proposer.py's own bounded design caps the LLM part
# at 3 real calls (~90s worst case), but /act then also DISPATCHES the
# real action afterward -- restore_from_disk_full alone genuinely polls
# for up to POD_TERMINATE_TIMEOUT_S + POD_START_TIMEOUT_S = 120s (see
# actions.py). 90s alone would have been a real, live timeout risk for
# disk-full specifically (one of the 6 currently can_act classes) the
# moment its diagnoser_mode ever promotes to "llm".
#
# RAISED 2026-08-01, real live incident: model_backend.py's new DNS
# flush-and-retry (real fix for a real recurring stale-DNS failure that
# night) adds up to one extra `timeout`-length retry to the FIRST
# provider that fails within any 60s window (cooldown-gated -- every
# OTHER provider failing in that same window skips the retry and fails
# fast, same as before). Real corrected worst case for a genuinely bad
# night (multiple providers failing together): ~240s for diagnosis
# (60s first-failure-with-retry + 6*30s the rest, 7-entry chain) and
# ~240s for the LLM part of /act alone (60s + 2*30s, 3-call bound) before
# even reaching dispatch -- both already at or past the OLD flat
# ceilings, confirmed live (a real oom episode hit exactly this wall).
# 300s/450s below are real-margin-over-corrected-worst-case, not
# arbitrary round numbers.
DIAGNOSE_TIMEOUT_S = 300
ACT_TIMEOUT_S = 450

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


def ensure_episode_snapshots_table(conn: sqlite3.Connection):
    """
    Tier A capture (2026-07-22): persists what the agent's /handle response
    ALREADY contains per episode but was previously discarded once
    p3_scorer.py pulled out only the few fields needed for scores/trust.
    This is the real data source the frontend's Replay Viewer will read
    from -- without it there was nothing to show beyond a bare verdict.

    Deliberately NOT a time series (Tier B, deferred): tool_output is
    the single point-in-time snapshot query_prometheus already returns,
    and durability_elapsed_s is verify_durability's own final elapsed
    time, not a per-poll history -- neither of those exist as real data
    anywhere in the system yet. Storing exactly what's real, nothing
    fabricated to look richer than it is.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_snapshots (
            episode_id TEXT PRIMARY KEY,
            tool_output TEXT,
            reasoning TEXT,
            confidence REAL,
            action_taken TEXT,
            action_result TEXT,
            durability_verdict TEXT,
            durability_elapsed_s INTEGER,
            captured_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
        )
        """
    )
    # gate_substitution added 2026-07-31 (review 16) -- same
    # table-may-already-exist pattern as ensure_scores_table above.
    # Populated only on episodes where the dispatch gate actually
    # redirected the real action; NULL otherwise. This is the real,
    # honest record the Replay Viewer reads to show "agent diagnosed Y,
    # actual fault was X, gate applied the correct fix instead" rather
    # than silently showing the substituted action as if the agent had
    # proposed it.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(episode_snapshots)")}
    if "gate_substitution" not in existing_cols:
        conn.execute("ALTER TABLE episode_snapshots ADD COLUMN gate_substitution TEXT")
    # provider/model/tier/transcript/observations/failed_attempts added
    # 2026-08-05 for the Replay Viewer's real turn-by-turn story -- all
    # six already computed by run_react_diagnosis() on every episode
    # (the background LLM comparison runs regardless of diagnoser_mode),
    # just never persisted before now. Only ever populated going
    # forward; episodes scored before this column existed keep these
    # NULL -- the Replay Viewer must treat that as "no enriched trace
    # available", never backfill or fabricate one.
    new_cols = {
        "provider": "TEXT", "model": "TEXT", "tier": "TEXT",
        "transcript_json": "TEXT", "observations_json": "TEXT", "failed_attempts_json": "TEXT",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE episode_snapshots ADD COLUMN {col} {col_type}")
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


def get_episode_by_id(conn: sqlite3.Connection, episode_id: str):
    """
    Score a SPECIFIC episode, not "whatever's most recent" (2026-07-24,
    Phase B). operator_api.py's /trigger/resolve always knows exactly
    which episode it wants scored -- it's the one it just injected -- so
    it should never rely on the "most recent unscored" heuristic
    get_unscored_episode() uses for manual/CLI runs. That heuristic is
    only a reasonable guess when the caller doesn't know a specific ID;
    once a caller DOES know, guessing is a real correctness risk (e.g. the
    future parked multi-inject case: inject crash-loop, then inject oom,
    then click "fix" on crash-loop -- "most recent unscored" would
    silently score oom's episode instead, applying the wrong real action).
    No staleness check here -- the caller (operator_api.py) already
    enforces its own, tighter, real-time-window-based expiry check BEFORE
    ever invoking this, so by the time this runs the caller has already
    decided the episode is fresh enough to score.
    """
    row = conn.execute(
        """
        SELECT e.episode_id, e.fault_class, e.target, e.namespace
        FROM episodes e
        LEFT JOIN scores s ON e.episode_id = s.episode_id
        WHERE e.episode_id = ? AND s.episode_id IS NULL
        """,
        (episode_id,),
    ).fetchone()
    if row is None:
        print(f"Episode '{episode_id}' not found, or already scored.")
        return None
    return row


def diagnosis_matches(predicted: str, actual: str) -> bool:
    # "none" control episodes never went through p3_scorer.py before Phase
    # D (only p2_readonly_loop/scorer.py's systematic validation used
    # them) -- this special case already exists there and is mirrored here
    # for the same reason: the STUB reports "no anomaly detected", never
    # the literal string "none".
    #
    # CORRECTED 2026-07-31: `predicted` here is now p3_agent's
    # production_result["diagnosis"], which can ALSO come from the real
    # LLM (once a class is promoted to Dimension B's "llm" mode) -- and
    # the LLM speaks FAULT_CLASSES' own vocabulary, "none" literally, not
    # "no anomaly detected". Without this, every correct "none" diagnosis
    # from an LLM-mode class would score as WRONG, corrupting Dimension A
    # itself. Accept either spelling.
    actual = actual.strip().lower()
    predicted = predicted.strip().lower()
    if actual == "none":
        return predicted in ("no anomaly detected", "none")
    return predicted == actual


def record_llm_trust(conn: sqlite3.Connection, episode_id: str, actual_class: str,
                      target: str, namespace: str, result: dict):
    """
    Real Dimension B/C wiring (step 4). Runs for EVERY episode -- p3_agent's
    /handle always runs the real LLM in the background, regardless of
    current diagnoser_mode, so this always has something to log/compare.
    This is the ONLY place that ever calls llm_trust_state.record_*
    (the scorer is the only piece allowed to see ground truth, same
    discipline as trust_engine's Dimension A above). Never raises on a
    normal missing-data case (llm_unavailable, non-auto-fix class,
    fallback-tier) -- those just mean nothing gets recorded this episode,
    not an error.
    """
    stub_result = result.get("stub_result") or {}
    llm_result = result.get("llm_result") or {}
    stub_predicted = stub_result.get("diagnosis")
    llm_diagnosis = llm_result.get("llm_diagnosis")

    ensure_llm_diagnosis_log_table(conn)
    conn.execute(
        """
        INSERT INTO llm_diagnosis_log (
            episode_id, actual_class, stub_predicted_class, stub_correct,
            llm_diagnosis, llm_confidence, llm_confidence_source, llm_reasoning,
            provider, model, tier, matches_ground_truth, matches_stub, failed_attempts_json,
            response_time_ms, llm_version_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode_id, actual_class, stub_predicted,
            int(diagnosis_matches(stub_predicted, actual_class)) if stub_predicted else None,
            llm_diagnosis, llm_result.get("llm_confidence"), llm_result.get("llm_confidence_source"),
            llm_result.get("llm_reasoning"), llm_result.get("provider"), llm_result.get("model"),
            llm_result.get("tier"),
            int(llm_diagnosis == actual_class) if llm_diagnosis else None,
            int(_same_diagnosis(llm_diagnosis, stub_predicted)) if llm_diagnosis and stub_predicted else None,
            json.dumps(llm_result.get("failed_attempts", []), default=lambda o: getattr(o, "__dict__", str(o))),
            llm_result.get("response_time_ms"),
            llm_result.get("llm_version_fingerprint"),
        ),
    )
    conn.commit()

    # Dimension B: only when the LLM actually produced a diagnosis this
    # episode -- llm_unavailable/max_turns_exceeded/invalid_tool_name have
    # no diagnosis to score, so no streak movement either way (a dead
    # provider shouldn't be able to revert a class's mode by producing
    # nothing, same "don't punish silence" reasoning circuit_breaker.py
    # already uses elsewhere).
    if llm_result.get("status") == "diagnosed":
        diag_eligible, diag_reason = eligible_for_trust_ladder(llm_result.get("tier"))
        if diag_eligible:
            diag_correct = llm_diagnosis == actual_class
            b_result = record_diagnoser_outcome(conn, actual_class, diag_correct, episode_id=episode_id)
            print("Dimension B update:", b_result)
        else:
            print(f"Dimension B: not recorded -- {diag_reason}")

    # Dimension C: only when p3_agent actually computed a real action
    # proposal this episode (diagnoser_mode_used=='llm' AND the LLM's own
    # diagnosis landed on an auto-fix class) AND the TRUE class is itself
    # an auto-fix class (nothing real to compare a proposal against
    # otherwise -- same "ground truth must be an auto-fix class" gate
    # trust_engine's own record_outcome enforces for Dimension A).
    proposal = result.get("llm_action_proposal")
    if proposal is not None and proposal.get("tool_name") is not None and actual_class in DETERMINISTIC_ACTION_MAP:
        log_proposal(episode_id, target, namespace, proposal)  # opens its own connection, ensures its own table

        gt_tool, gt_params_fn = DETERMINISTIC_ACTION_MAP[actual_class]
        gt_params = gt_params_fn(target, namespace)
        action_ok, action_reason = action_is_correct(
            actual_class, proposal["tool_name"], proposal["params"], gt_tool, gt_params,
            tool_output=result.get("tool_output"),
        )
        action_eligible, action_gate_reason = eligible_action_for_streak(proposal.get("tier"))
        if action_eligible:
            c_result = record_action_outcome(conn, actual_class, action_ok, episode_id=episode_id)
            print("Dimension C update:", c_result, "--", action_reason)
        else:
            print(f"Dimension C: not recorded -- {action_gate_reason}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--episode-id", dest="episode_id", default=None,
        help="Score this specific episode instead of guessing the most recent unscored one.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    # WAL mode, added 2026-08-05 (Kimi review 24) alongside the new
    # comparison-sampling background writes in p3_agent.py -- without
    # it, this connection's writes and the background executor's own
    # fresh connections can collide ("database is locked") under real
    # concurrent load. Readers/writers no longer block each other; the
    # remaining single-writer lock is held for microseconds per INSERT.
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_scores_table(conn)
    ensure_episode_snapshots_table(conn)
    ensure_trust_tables(conn)
    ensure_circuit_breaker_table(conn)
    ensure_llm_trust_tables(conn)

    if args.episode_id:
        episode = get_episode_by_id(conn, args.episode_id)
    else:
        episode = get_unscored_episode(conn)
    if episode is None:
        print("No unscored episodes found.")
        return

    episode_id, actual_class, target, namespace = episode

    # Phase 1: diagnosis only (stub + background LLM), independently timed.
    diag_resp = requests.post(
        DIAGNOSE_URL, json={"target": target, "namespace": namespace, "episode_id": episode_id},
        timeout=DIAGNOSE_TIMEOUT_S,
    )
    diag_resp.raise_for_status()
    result = diag_resp.json()

    # Phase 2: action proposal + real dispatch. Real fix 2026-08-05 (Kimi
    # review 23 follow-up): USED to call /act only when eligible_for_action
    # said the AUTHORITATIVE (stub, while B=stub) diagnosis was already an
    # auto-fix class at can_act -- meaning Dimension C's background
    # comparison could never see an episode where the stub said "none"/
    # report-only while the LLM privately believed it was a real auto-fix
    # fault. Now ALSO calls /act whenever the LLM produced ANY auto-fix-
    # class diagnosis, even if the stub's own answer wasn't eligible --
    # /act itself (not this file) is what actually decides whether that's
    # worth spending real quota on (only proceeds to propose_action if the
    # LLM's OWN believed class is already Dimension-A can_act; otherwise
    # it's a cheap local DB check and a no-op, not a real LLM call).
    llm_diag = (result.get("llm_result") or {}).get("llm_diagnosis")
    worth_calling_act = result.get("eligible_for_action") or (
        llm_diag is not None and llm_diag in DETERMINISTIC_ACTION_MAP
    )
    if worth_calling_act:
        act_req = {
            "target": target, "namespace": namespace,
            "predicted": result["diagnosis"], "diagnoser_mode_used": result["diagnoser_mode_used"],
            "confidence": result.get("confidence"), "reasoning": result.get("reasoning"),
            "llm_provider": (result.get("llm_result") or {}).get("provider"),
            "llm_model": (result.get("llm_result") or {}).get("model"),
            # The LLM's OWN raw diagnosis, distinct from "predicted"
            # above (which is whichever diagnosis is authoritative for
            # production -- the stub's, while diagnoser_mode_used ==
            # "stub"). Added 2026-08-05 so /act's Dimension C background
            # comparison can run off the LLM's own belief even when it
            # isn't currently driving production -- see p3_agent.py's
            # ActRequest docstring for the full reasoning.
            "llm_diagnosis": (result.get("llm_result") or {}).get("llm_diagnosis"),
            "llm_confidence": (result.get("llm_result") or {}).get("llm_confidence"),
            "llm_reasoning": (result.get("llm_result") or {}).get("llm_reasoning"),
            "tool_output": result.get("tool_output"),
            # Ground truth, review 16's dispatch gate -- p3_scorer.py is
            # the only piece of this system allowed to know this; /act
            # uses it exclusively for the gate's internal comparison,
            # never forwards it to propose_action/run_react_diagnosis.
            "episode_id": episode_id,
            "actual_class": actual_class,
        }
        act_resp = requests.post(ACT_URL, json=act_req, timeout=ACT_TIMEOUT_S)
        act_resp.raise_for_status()
        result.update(act_resp.json())
    else:
        result.setdefault("action_taken", None)
        result.setdefault("action_result", None)
        result.setdefault("action_source", None)
        result.setdefault("llm_action_proposal", None)
        result.setdefault("gate_substitution", None)

    predicted_class = result["diagnosis"]
    confidence = result.get("confidence")
    diagnosis_correct = diagnosis_matches(predicted_class, actual_class)

    action_taken = result.get("action_taken")
    action_result = result.get("action_result")
    action_applied = action_result["applied"] if action_result else None
    durability_verdict = None
    durability_elapsed_s = None
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
            # action_result passed through 2026-08-06 -- see
            # verifier.py's verify_durability docstring: under-
            # provisioned-replicas' durability check needs the REAL
            # dispatched replica count, not a hardcoded assumption.
            verdict = verify_durability(actual_class, target, namespace, action_result=action_result)
            durability_verdict = verdict["verdict"]
            durability_elapsed_s = verdict["elapsed_s"]
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
    # Real per-episode LLM trace, same dict record_llm_comparison already
    # reads from (see above) -- present whenever the background LLM
    # comparison actually produced a result this episode, None otherwise
    # (e.g. llm_unavailable). Stored raw here so the Replay Viewer can
    # show the real turn-by-turn evidence sequence, not just the final
    # collapsed reasoning sentence.
    llm_result = result.get("llm_result") or {}
    conn.execute(
        """
        INSERT INTO episode_snapshots
            (episode_id, tool_output, reasoning, confidence,
             action_taken, action_result, durability_verdict, durability_elapsed_s,
             gate_substitution, provider, model, tier,
             transcript_json, observations_json, failed_attempts_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode_id,
            json.dumps(result.get("tool_output")),
            result.get("reasoning"),
            confidence,
            action_taken,
            json.dumps(action_result) if action_result is not None else None,
            durability_verdict,
            durability_elapsed_s,
            json.dumps(result.get("gate_substitution")) if result.get("gate_substitution") else None,
            llm_result.get("provider"),
            llm_result.get("model"),
            llm_result.get("tier"),
            json.dumps(llm_result.get("transcript")) if llm_result.get("transcript") else None,
            json.dumps(llm_result.get("observations")) if llm_result.get("observations") else None,
            json.dumps(llm_result.get("failed_attempts"), default=lambda o: getattr(o, "__dict__", str(o)))
            if llm_result.get("failed_attempts") else None,
        ),
    )
    conn.commit()

    if trust_correct is not None:
        trust_result = record_outcome(conn, actual_class, trust_correct, episode_id=episode_id)
        print("trust update:", trust_result)
        if breaker_result and breaker_result["tripped"]:
            print("CIRCUIT BREAKER TRIPPED:", breaker_result)

    # Cross-class misdispatch guard -- REVISED 2026-07-31 after Kimi
    # review 15 rejected the original fix (which called record_outcome
    # on predicted_class, penalizing its OWN promotion streak). Real
    # reasoning for the reversal: predicted_class's action mechanism
    # executed exactly as designed (dry-run passed, patch applied) --
    # the failure was entirely in diagnosis, not in that class's own
    # action reliability. Penalizing its streak for that would corrupt
    # what "N consecutive correct" is supposed to measure (a class
    # frequently confused with others would get demoted despite a
    # perfectly reliable action mechanism). See misdispatch_guard.py's
    # own module docstring for the full reasoning and the accepted
    # alternative: track misdispatches separately, force a temporary
    # safety hold (NOT a demotion, streak untouched) if they recur
    # often enough, never touch trust_state's streak at all here.
    if action_taken is not None and predicted_class != actual_class and predicted_class in PROMOTION_STREAK:
        ensure_misdispatch_tables(conn)
        misdispatch_result = record_misdispatch(conn, episode_id, predicted_class, actual_class)
        print("misdispatch guard:", misdispatch_result)
        if misdispatch_result["hold_triggered"]:
            print(f"SAFETY HOLD TRIGGERED for {predicted_class} -- streak preserved, dispatch paused.")

    # Dimension B/C -- always runs, independent of Dimension A above,
    # since /handle always ran the real LLM in the background regardless
    # of this episode's action outcome.
    record_llm_trust(conn, episode_id, actual_class, target, namespace, result)

    conn.close()

    verdict = "CORRECT" if diagnosis_correct else "WRONG"
    print(f"Episode {episode_id}: predicted='{predicted_class}' actual='{actual_class}' -> {verdict}")


if __name__ == "__main__":
    main()
