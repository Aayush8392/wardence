"""
Phase D: cross-class pairwise spot-check. Runs pre-generated batches (or
sampled rounds) of ordered (A, B) class pairs sequentially through the
REAL P3 pipeline (injector -> settle -> p3_scorer.py, which calls the
real p3_agent and takes real actions for can_act classes) -- looking for
cross-class contamination that Phase 2's targeted per-class checks
wouldn't catch.

Unlike run_systematic_validation.py, this does NOT stop or retry on
failure. Phase D is a spot-check, not a gate (see wardence_buildlog.md's
Phase D design notes) -- a failed pair is a FINDING to review afterward,
not a reason to halt an unattended overnight run. Every episode's outcome
(including exceptions) is caught, logged to phase_d_results.jsonl, and
printed; the run always proceeds to the next pair no matter what.

MULTI-ROUND MODE (added 2026-07-26, for running phase_d_generate_sample_
rounds.py's output -- several ~50%-sized rounds in ONE command instead of
one full 7x7 pass): pass --rounds-glob (default "phase_d_round*.json") or
explicit --batch-file entries (repeatable). Every round/batch runs
sequentially in the SAME process, so a single invocation produces one
combined summary across all of them, not N separate summaries to
reconcile by hand.

UNCAPPED AUTO-RECOVERY (changed 2026-07-26): earlier versions capped
auto-recovery at 3 (globally and per-class) so a repeatedly-tripping
breaker/demoting class would surface as a finding instead of being
silently re-promoted forever. Per user's explicit call, the cap is
REMOVED -- every demotion (per-class or global-breaker) is now ALWAYS
auto-recovered so real-action coverage is never lost to a demotion for
the rest of a long multi-round run. The recovery COUNT is still fully
tracked and printed/summarized (per class and globally) so a class or
the breaker tripping repeatedly is still visible as a real finding --
just never gated from being re-promoted.

Prerequisites (must already be running):
    - Prometheus port-forward
    - uvicorn p3_agent:app --reload --app-dir p3_trust_action --port 8001
    - all 6 auto-fix classes should be can_act -- run
      p3_trust_action/phase_d_promote_all.py first if not.
    - batch/round files exist (run phase_d_generate_batches.py or
      phase_d_generate_sample_rounds.py first if not).

Usage:
    python3 phase_d_run.py                                       # discovers phase_d_round*.json, runs all found (sorted)
    python3 phase_d_run.py --rounds-glob "phase_d_round*.json"    # same, explicit
    python3 phase_d_run.py --batch-file phase_d_batch1.json --batch-file phase_d_batch2.json
                                                                   # explicit file list, original two-batch mode still works
"""

import argparse
import glob
import json
import random
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from injector import FAULT_CONFIG, ensure_db

HERE = Path(__file__).parent
SCORER_SCRIPT = HERE.parent / "p3_trust_action" / "p3_scorer.py"


def _now_iso() -> str:
    """Single helper for every timestamp this script writes, so they're
    all the same real-UTC ISO format and trivially sortable/diffable
    across a multi-round run's results file."""
    return datetime.now(timezone.utc).isoformat()

SETTLE_SECONDS = 35
NONE_SETTLE_SECONDS = 10
# Generous margin over the longest real fix+durability cycle observed so
# far (disk-full/under-provisioned-replicas run up to ~280s) -- just
# spacing out repeated faults on the same target, not a correctness gate.
TARGET_RECENCY_WINDOW_S = 300

REAL_CLASSES = [
    "crash-loop", "oom", "disk-full",
    "cpu-throttling", "under-provisioned-replicas", "bad-rollout",
]
CONTROL_TARGETS = [FAULT_CONFIG[c]["target"] for c in REAL_CLASSES]

RESULTS_PATH = HERE / "phase_d_results.jsonl"
DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

# circuit_breaker.py's global breaker (3 failures/5min -> every can_act
# class force-demoted to report_only, independent of per-class trust) is
# a real risk for an unattended overnight run: if it trips mid-run, every
# remaining episode of every class silently degrades to diagnosis-only,
# no error, nothing that stops the script -- exactly the kind of quiet
# "inaccurate run in the morning" that defeats the point of Phase D
# exercising real actions. Detected via p3_scorer.py's own stdout, which
# already prints "CIRCUIT BREAKER TRIPPED: ..." when it happens.
#
# UNCAPPED (changed 2026-07-26, per explicit user call): earlier versions
# capped this at 3 recoveries so a repeatedly-tripping breaker would
# surface as a finding instead of being silently re-promoted forever.
# Across a long multi-round overnight session that cap risked losing
# real-action coverage for entire later rounds once hit. Recovery is now
# ALWAYS attempted, with NO limit -- but every recovery event is still
# fully counted AND timestamped (see _recovery_log below), so "the
# breaker tripped 11 times tonight" is still fully visible as a real
# finding in the final summary, just never gates re-promotion.
_auto_recovery_count = 0

# Per-class demotion auto-recovery -- a real gap in the first Phase D run
# (2026-07-24/25): the circuit-breaker recovery above only catches the
# GLOBAL breaker tripping. An individual class demoting on its own
# through the normal trust-engine path (a real flap/misdiagnosis on just
# that one class, no breaker involved) was NOT recovered -- that class
# just sat report_only (diagnosis-only) for the rest of the night,
# losing real-action coverage for however many of its remaining pairs
# were left. Found live, mid-run, by direct question -- fixed here for
# any future run.
#
# UNCAPPED (changed 2026-07-26, same reasoning as the breaker cap above):
# re-promotion is now ALWAYS attempted on every fresh can_act->report_only
# transition, with no per-class limit -- so real-action coverage is never
# lost to a demotion for the rest of a long multi-round run. Demotions
# themselves are ALWAYS recorded in trust_history/failure_log regardless
# of how fast they're recovered from -- auto-recovery doesn't hide the
# finding, it just stops it from costing coverage. The per-class count is
# still fully tracked and timestamped so a class demoting repeatedly (see
# `bad-rollout`'s 3 demotions in the first run) is still clearly visible
# as a real finding in the final combined summary.
_class_recovery_count: dict[str, int] = {fc: 0 for fc in REAL_CLASSES}

# Full timestamped log of every recovery event (breaker trips AND
# per-class demotions), across the ENTIRE multi-round run -- not just a
# running count. This is what makes "what happened and when, across all
# 4 rounds tonight" answerable without grepping stdout. Each entry:
# {"timestamp": iso, "type": "breaker"|"class_demotion", "fault_class":
#  str|None, "round_id": str, "recovery_number": int (this type's running
#  total as of this event)}.
_recovery_log: list[dict] = []

# Tracks whether each class was can_act as of the last check -- lets a
# fresh can_act->report_only transition be told apart from "already sitting
# report_only, still re-earning naturally" (which must NOT be force-promoted,
# or the whole earned-streak mechanism -- the actual thing being tested --
# would never get exercised at all). Seeded from real DB state at startup,
# not assumed, in case promote_all_to_can_act wasn't actually run first.
_known_can_act: dict[str, bool] = {}

# Set by run_batch() before processing each pair -- lets run_episode() /
# the recovery handlers stamp which round/batch an event belongs to
# without threading an extra parameter through every call site.
_current_round_id: str = "unknown"


def _promote_one_to_can_act(fault_class: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO trust_state (fault_class, state, streak, updated_at)
        VALUES (?, 'can_act', 5, datetime('now'))
        ON CONFLICT(fault_class) DO UPDATE SET
            state = excluded.state, streak = excluded.streak, updated_at = excluded.updated_at
        """,
        (fault_class,),
    )
    conn.commit()
    conn.close()


def _promote_all_to_can_act():
    """Same mechanism as phase_d_promote_all.py, inlined so a mid-run
    circuit-breaker trip can be recovered from without stopping."""
    conn = sqlite3.connect(DB_PATH)
    for fc in REAL_CLASSES:
        conn.execute(
            """
            INSERT INTO trust_state (fault_class, state, streak, updated_at)
            VALUES (?, 'can_act', 5, datetime('now'))
            ON CONFLICT(fault_class) DO UPDATE SET
                state = excluded.state, streak = excluded.streak, updated_at = excluded.updated_at
            """,
            (fc,),
        )
    conn.commit()
    conn.close()


def _seed_known_can_act():
    """Called once at startup -- reads REAL current trust_state, not an
    assumption, in case the pre-run promote step was skipped or partial."""
    conn = sqlite3.connect(DB_PATH)
    for fc in REAL_CLASSES:
        row = conn.execute("SELECT state FROM trust_state WHERE fault_class = ?", (fc,)).fetchone()
        _known_can_act[fc] = (row is not None and row[0] == "can_act")
    conn.close()


def _check_and_recover_class_demotion(fault_class: str) -> dict:
    """
    Called after EVERY scored episode of a real class (not just ones where
    an action was taken -- a misdiagnosis-caused demotion, like
    cpu-throttling's false negative in the first run, never takes an
    action at all but still demotes). Compares current trust_state against
    the last known state to detect a FRESH can_act->report_only transition
    specifically -- a class already sitting report_only and naturally
    climbing back toward can_act via real consecutive-correct episodes
    must be left alone, or the whole earned-trust mechanism this project
    exists to test would never actually run.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT state, streak FROM trust_state WHERE fault_class = ?", (fault_class,)).fetchone()
    conn.close()
    if row is None:
        return {"demoted": False, "recovered": False}
    state, streak = row

    was_can_act = _known_can_act.get(fault_class, False)
    if state == "can_act":
        _known_can_act[fault_class] = True
        return {"demoted": False, "recovered": False}

    if not was_can_act:
        # Already report_only before this episode -- normal re-earning in
        # progress, not a fresh demotion. Leave it alone.
        return {"demoted": False, "recovered": False}

    # Fresh can_act -> report_only transition just happened.
    _known_can_act[fault_class] = False
    demotion_ts = _now_iso()
    print(f"\n{'~' * 70}")
    print(f"[{demotion_ts}] {fault_class}: DEMOTED (can_act -> report_only) -- "
          f"detected after this episode's scoring, round={_current_round_id}.")

    # UNCAPPED: always re-promote (see module docstring + the comment
    # above _class_recovery_count for the 2026-07-26 reasoning). The
    # count is still incremented and logged with a timestamp so repeated
    # demotions of the same class remain fully visible as a finding.
    _class_recovery_count[fault_class] += 1
    _promote_one_to_can_act(fault_class)
    _known_can_act[fault_class] = True
    _recovery_log.append({
        "timestamp": demotion_ts,
        "type": "class_demotion",
        "fault_class": fault_class,
        "round_id": _current_round_id,
        "recovery_number": _class_recovery_count[fault_class],
    })
    print(f"Auto-recovery #{_class_recovery_count[fault_class]} for {fault_class} (uncapped): "
          f"re-promoted so its remaining pairs still test real actions. "
          f"The demotion itself is still recorded in trust_history -- this doesn't hide the finding.")
    print(f"{'~' * 70}\n")
    return {"demoted": True, "recovered": True}


def _handle_circuit_breaker_trip(scorer_output: str) -> bool:
    """
    Called the moment p3_scorer.py's own output shows the breaker tripped.

    UNCAPPED (2026-07-26): always re-promotes all 6 classes and returns
    True -- see module docstring for reasoning. The trip is still fully
    counted and timestamped in _recovery_log, so a breaker tripping many
    times in one run remains a clearly visible finding in the final
    combined summary, it just never gates re-promotion.
    """
    global _auto_recovery_count
    trip_ts = _now_iso()
    print(f"\n{'!' * 70}")
    print(f"[{trip_ts}] CIRCUIT BREAKER TRIPPED (detected in p3_scorer.py output above, "
          f"round={_current_round_id}) -- every can_act class was just force-demoted to report_only.")
    _auto_recovery_count += 1
    _promote_all_to_can_act()
    _recovery_log.append({
        "timestamp": trip_ts,
        "type": "breaker",
        "fault_class": None,
        "round_id": _current_round_id,
        "recovery_number": _auto_recovery_count,
    })
    print(f"Auto-recovery #{_auto_recovery_count} (uncapped): re-promoted all 6 classes to "
          f"can_act so the rest of the run keeps testing real actions.")
    print(f"{'!' * 70}\n")
    return True


def log_result(record: dict):
    record["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(RESULTS_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def run_subprocess(script: Path, extra_args: list[str] | None = None) -> tuple[bool, str]:
    cmd = [sys.executable, str(script)] + (extra_args or [])
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    print(output.strip())
    return result.returncode == 0, output


PROMETHEUS_HEALTH_URL = "http://localhost:9090/-/healthy"
# Deliberately NOT /handle -- /handle is p3_agent's ACTION-TAKING
# endpoint (see p3_agent.py): if a diagnosis against the ping target maps
# to a can_act class, it unconditionally takes the real action. A health
# check firing ~100 times overnight against a live-fire endpoint would be
# a real, untracked risk (e.g. a false-positive crash-loop read on
# "carts" mid-ping silently triggering a real, unscored restart) --
# /progress is a plain read (in-memory lookup, no diagnosis, no action)
# and only exists to confirm the uvicorn process itself is alive and
# responsive, same purpose as a true health endpoint.
AGENT_HEALTH_URL = "http://localhost:8001/progress/sock-shop/__phase_d_health_check__"
INFRA_WAIT_MAX_S = 1200  # 20 min -- absorbs a real outage (port-forward
                          # reconnect, WSL2 stall) without waiting forever
INFRA_WAIT_POLL_S = 20


def _infra_ready() -> tuple[bool, str]:
    """
    Checks BOTH Prometheus directly (injector.py's own effect-verification
    queries hit it directly, not through the agent) and the agent process
    (which p3_scorer.py depends on). Either being down is a reason to
    pause, not push through and log a false failure.
    """
    import requests
    try:
        r = requests.get(PROMETHEUS_HEALTH_URL, timeout=5)
        if r.status_code != 200:
            return False, f"Prometheus returned {r.status_code}"
    except requests.RequestException as e:
        return False, f"Prometheus unreachable ({e})"
    try:
        r = requests.get(AGENT_HEALTH_URL, timeout=10)
        if r.status_code != 200:
            return False, f"agent returned {r.status_code}"
    except requests.RequestException as e:
        return False, f"agent unreachable ({e})"
    return True, "ok"


def wait_for_infra_ready() -> bool:
    """
    Called at the START of every episode. If Prometheus/the agent is
    briefly down (port-forward reconnecting, a real outage), PAUSES here
    and polls instead of immediately treating the episode as a real
    failure -- a false failure caused by infra noise would otherwise
    pollute the results log AND feed the circuit breaker on nothing real.
    Gives up after INFRA_WAIT_MAX_S and lets the caller log it distinctly
    (infra_unreachable), not as a diagnosis/action failure.
    """
    waited = 0
    while True:
        ok, detail = _infra_ready()
        if ok:
            if waited > 0:
                print(f"  infra back up after {waited}s wait -- proceeding.")
            return True
        if waited >= INFRA_WAIT_MAX_S:
            print(f"  infra still not ready after {INFRA_WAIT_MAX_S}s ({detail}) -- giving up on this episode.")
            return False
        print(f"  infra not ready ({detail}) -- pausing {INFRA_WAIT_POLL_S}s before rechecking "
              f"(waited {waited}s/{INFRA_WAIT_MAX_S}s so far)")
        time.sleep(INFRA_WAIT_POLL_S)
        waited += INFRA_WAIT_POLL_S


def _episode_ids(conn) -> set:
    return {row[0] for row in conn.execute("SELECT episode_id FROM episodes")}


def record_none_episode(conn, target: str) -> str:
    episode_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO episodes (episode_id, fault_class, target, namespace, t0, chaos_resource_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (episode_id, "none", target, "sock-shop", t0, "none"),
    )
    conn.commit()
    print(f"Episode {episode_id} (control): no fault injected, target={target} at {t0}")
    return episode_id


# Found 2026-07-25, investigating 2 Phase D "none" -> "bad-rollout" false
# positives (episode_ids a901327d-3207-44d6-a349-f76ccf989718 and
# 1f7db472-c567-4849-b74d-5551e1bc1b39). Root cause was NOT staleness or a
# diagnosis bug -- confirmed by reading injector.py directly:
# _inject_and_verify_bad_rollout is deliberately designed as an AUTO-FIX
# class with no self-revert on success ("ground truth is left broken for
# the agent's own real fix to resolve later"). While bad-rollout was
# report_only (as it was for the ~3.3 hour window spanning both false
# positives, confirmed via trust_history), nothing in the pipeline ever
# ran rollback_deployment on it either -- report-only classes never take
# real actions. So front-end's Deployment was genuinely, correctly still
# broken (real ImagePullBackOff pod) for the whole 70-87 minute gap
# between the last bad-rollout injection and each false positive.
#
# record_none_episode() never checked whether its randomly-picked target
# was already broken by an earlier episode before labeling the episode
# "none" (nothing injected, target assumed healthy) -- an assumption
# that's false whenever an earlier REPORT-ONLY auto-fix-class injection
# succeeded and was never cleaned up. The agent's diagnosis was CORRECT
# both times; the control episode's own ground-truth label was wrong.
#
# Only front-end/bad-rollout has this specific risk today: it's the only
# CONTROL_TARGETS class whose injection mechanism has no revert-on-success
# path (crash-loop/oom/disk-full self-verify and, when can_act, get fixed
# by the agent; even when report-only, crash-loop/oom/disk-full's OWN
# injectors correct themselves -- see injector.py's per-class revert/reset
# behavior for each). If a future class is added to CONTROL_TARGETS with
# the same "no revert on successful report-only injection" shape, this
# same check should be extended to cover it too -- not assumed safe by
# default.
FRONT_END_BAD_ROLLOUT_LABEL_MATCH = {"ImagePullBackOff", "ErrImagePull"}


def _front_end_genuinely_broken() -> bool:
    """
    Live kubectl check (not Prometheus -- same reasoning as verifier.py's
    _front_end_image_pull_failing_live, avoids any scrape-lag question
    entirely by going straight to the source of truth) for whether
    front-end currently has a pod stuck in ImagePullBackOff/ErrImagePull.
    Only relevant when target == "front-end" was picked for a "none"
    control -- see the comment above record_none_episode for why this
    check exists.
    """
    result = subprocess.run(
        [
            "kubectl", "get", "pods", "-n", "sock-shop",
            "-l", "name=front-end",
            "-o", "json",
        ],
        capture_output=True,
        text=True,
    )
    try:
        pods = json.loads(result.stdout)["items"]
    except (json.JSONDecodeError, KeyError):
        # Can't confirm health -- fail safe by treating as "can't verify,
        # skip this target" rather than silently trusting an unknown state.
        return True
    for pod in pods:
        for cs in pod.get("status", {}).get("containerStatuses", []):
            reason = cs.get("state", {}).get("waiting", {}).get("reason")
            if reason in FRONT_END_BAD_ROLLOUT_LABEL_MATCH:
                return True
    return False


def pick_healthy_none_target(max_attempts: int = 6) -> str | None:
    """
    Picks a random target for a "none" control episode, the same as
    before, but now verifies front-end's real live health first if it's
    the one picked -- since front-end/bad-rollout is the one class whose
    report-only injections can leave a genuine, real standing fault with
    no self-revert (see the comment above record_none_episode). Other
    targets are trusted as before -- this is a targeted fix for the one
    confirmed real risk, not a blanket "verify every target" change.

    Returns None if it can't find a healthy target within max_attempts
    (extremely unlikely -- would mean front-end keeps getting picked AND
    keeps being genuinely broken every time) -- caller should skip
    recording a "none" episode this round rather than force a bad one
    through, same "refuse rather than record bad data" principle already
    used elsewhere in this project (injector.py's own total-failure
    handling, p3_scorer.py's staleness guard).
    """
    for _ in range(max_attempts):
        target = random.choice(CONTROL_TARGETS)
        if target != "front-end":
            return target
        if not _front_end_genuinely_broken():
            return target
        print("  front-end picked for a \"none\" control, but it's currently "
              "genuinely broken (real ImagePullBackOff pod) -- rerolling to "
              "avoid mislabeling a real standing fault as a clean control.")
    return None


def wait_for_target_recency(target: str, last_fault_time: dict):
    if target not in last_fault_time:
        return
    elapsed = time.time() - last_fault_time[target]
    remaining = TARGET_RECENCY_WINDOW_S - elapsed
    if remaining > 0:
        print(f"  waiting {remaining:.0f}s so {target}'s last fault clears the recency window")
        time.sleep(remaining)


# Found 2026-07-27, investigating a "none" -> under-provisioned-replicas
# false positive on catalogue (episode ccad4a97-d230-4815-8336-2b166c063b9c).
# wait_for_target_recency() above only checks the CONTROL's own picked
# target -- catalogue's own last real fault was 36 minutes earlier, well
# past TARGET_RECENCY_WINDOW_S, so it correctly didn't wait. But 3 OTHER
# real fixes (disk-full/queue-master, cpu-throttling/user,
# bad-rollout/front-end) all completed within the ~90s immediately before
# this control, and catalogue's active capacity probe (k6-driven, hits the
# whole app through front-end) read 203.83ms -- just over the 200ms
# threshold -- almost certainly real ambient load from that concurrent
# activity, not noise on an idle system. Different mechanism from the
# front-end/bad-rollout no-self-revert bug (Investigation 2) -- this is
# system-wide load bleeding into one target's probe, not one target's own
# leftover state. A per-target recency check can never catch this; only a
# system-wide one can.
def wait_for_system_quiet(last_fault_time: dict):
    if not last_fault_time:
        return
    elapsed = time.time() - max(last_fault_time.values())
    remaining = TARGET_RECENCY_WINDOW_S - elapsed
    if remaining > 0:
        print(f"  waiting {remaining:.0f}s for system-wide quiet before recording a 'none' control "
              f"(most recent real fault completion anywhere was {elapsed:.0f}s ago)")
        time.sleep(remaining)


def run_episode(fault_class: str, last_fault_time: dict) -> dict:
    """
    Timestamp + round_id wrapper around _run_episode_inner -- keeps the
    inner function's many existing early-return paths untouched (each
    already computes its own elapsed_s) while guaranteeing every result
    dict this function ever returns also carries started_at, completed_at,
    and round_id, regardless of which return path fired inside.
    """
    started_at = _now_iso()
    round_id = _current_round_id
    result = _run_episode_inner(fault_class, last_fault_time)
    result["started_at"] = started_at
    result["completed_at"] = _now_iso()
    result["round_id"] = round_id
    return result


def _run_episode_inner(fault_class: str, last_fault_time: dict) -> dict:
    """
    Never raises -- any failure at any step is caught and returned as a
    result dict, so one crashed episode can never take down the batch.
    """
    start = time.time()
    target = None
    try:
        if not wait_for_infra_ready():
            return {"fault_class": fault_class, "status": "infra_unreachable",
                     "elapsed_s": round(time.time() - start, 1)}

        conn = ensure_db()
        ids_before = _episode_ids(conn)
        conn.close()

        if fault_class == "none":
            target = pick_healthy_none_target()
            if target is None:
                return {"fault_class": fault_class, "status": "no_healthy_control_target",
                         "elapsed_s": round(time.time() - start, 1),
                         "detail": "front-end kept being genuinely broken across all reroll "
                                    "attempts -- skipped rather than record a mislabeled control"}
            # System-wide quiet check, not just this target's own recency --
            # see wait_for_system_quiet's comment above for why (a control
            # can be contaminated by ANY recent real fault's ambient load,
            # not only a prior fault on the same target).
            wait_for_system_quiet(last_fault_time)
            conn = ensure_db()
            episode_id = record_none_episode(conn, target)
            conn.close()
            time.sleep(NONE_SETTLE_SECONDS)
        else:
            target = FAULT_CONFIG[fault_class]["target"]
            wait_for_target_recency(target, last_fault_time)
            ok, out = run_subprocess(HERE / "injector.py", ["--class", fault_class])
            if not ok:
                return {"fault_class": fault_class, "target": target, "status": "injector_failed",
                         "elapsed_s": round(time.time() - start, 1), "detail": out[-2000:]}
            last_fault_time[target] = time.time()
            time.sleep(SETTLE_SECONDS)

            conn = ensure_db()
            ids_after = _episode_ids(conn)
            conn.close()
            new_ids = ids_after - ids_before
            if not new_ids:
                return {"fault_class": fault_class, "target": target, "status": "no_episode_recorded",
                         "elapsed_s": round(time.time() - start, 1)}
            if len(new_ids) > 1:
                print(f"  WARNING: {len(new_ids)} new episodes appeared in one step (expected 1): {new_ids}")
            episode_id = sorted(new_ids)[-1]

        ok, out = run_subprocess(SCORER_SCRIPT, ["--episode-id", episode_id])
        if not ok:
            return {"fault_class": fault_class, "target": target, "episode_id": episode_id,
                     "status": "scorer_failed", "elapsed_s": round(time.time() - start, 1), "detail": out[-2000:]}

        breaker_tripped = "CIRCUIT BREAKER TRIPPED" in out
        recovered = False
        if breaker_tripped:
            recovered = _handle_circuit_breaker_trip(out)

        conn = ensure_db()
        row = conn.execute(
            "SELECT predicted_class, correct, action_taken, action_applied, durability_verdict, trust_correct "
            "FROM scores WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        conn.close()
        if row is None:
            return {"fault_class": fault_class, "target": target, "episode_id": episode_id,
                     "status": "not_scored", "elapsed_s": round(time.time() - start, 1),
                     "circuit_breaker_tripped": breaker_tripped, "circuit_breaker_recovered": recovered}

        class_demotion = {"demoted": False, "recovered": False}
        if fault_class in REAL_CLASSES:
            class_demotion = _check_and_recover_class_demotion(fault_class)

        predicted, correct, action_taken, action_applied, durability_verdict, trust_correct = row
        return {
            "fault_class": fault_class, "target": target, "episode_id": episode_id,
            "status": "scored", "predicted": predicted, "diagnosis_correct": bool(correct),
            "action_taken": action_taken,
            "action_applied": bool(action_applied) if action_applied is not None else None,
            "durability_verdict": durability_verdict,
            "trust_correct": bool(trust_correct) if trust_correct is not None else None,
            "elapsed_s": round(time.time() - start, 1),
            "circuit_breaker_tripped": breaker_tripped,
            "circuit_breaker_recovered": recovered,
            "class_demoted": class_demotion["demoted"],
            "class_demotion_recovered": class_demotion["recovered"],
        }
    except Exception as e:
        return {"fault_class": fault_class, "target": target, "status": "exception", "detail": str(e),
                 "elapsed_s": round(time.time() - start, 1)}


def run_batch(batch_file: Path, last_fault_time: dict) -> dict:
    """
    Runs one round/batch file. round_id defaults to the file's own stem
    (e.g. "phase_d_round1" or "phase_d_batch1") -- this is what gets
    stamped onto every episode result and every recovery-log entry
    during this round, so a later query can answer "which round/run did
    episode X happen in" directly from phase_d_results.jsonl or
    _recovery_log without any separate bookkeeping.
    """
    global _current_round_id
    round_id = batch_file.stem
    _current_round_id = round_id

    pairs = json.loads(batch_file.read_text())
    n = len(pairs)
    round_started_at = _now_iso()
    batch_start = time.time()
    print(f"\n{'=' * 70}\nStarting round '{round_id}' ({batch_file.name}): {n} pairs "
          f"-- started_at={round_started_at}\n{'=' * 70}")

    # Per-round sanity gate: re-check infra is actually alive before THIS
    # round starts, not just once at the very beginning of the whole
    # multi-round session. A port-forward reconnect or WSL2 stall between
    # rounds (plausible over a 10-12hr unattended window) would otherwise
    # only surface as episode-level infra_unreachable results scattered
    # through the round, rather than a clear "round N couldn't even
    # start" signal.
    ok, detail = _infra_ready()
    if not ok:
        print(f"Round '{round_id}' sanity gate FAILED ({detail}) -- infra not reachable at round start. "
              f"Proceeding anyway; wait_for_infra_ready() inside each episode will retry/pause as usual, "
              f"but this is flagged here so a round-start failure is visible at a glance, not just buried "
              f"in individual episode results.")
    else:
        print(f"Round '{round_id}' sanity gate OK -- Prometheus and the agent are reachable.")

    summary = {
        "total": 0, "both_ok": 0, "issues": 0, "breaker_trips": 0, "class_demotions": 0,
        "started_at": round_started_at,
    }

    for i, (a, b) in enumerate(pairs, start=1):
        pair_started_at = _now_iso()
        print(f"\n--- round '{round_id}' pair {i}/{n}: {a} -> {b}  [started_at={pair_started_at}] ---")
        pair_start = time.time()

        result_a = run_episode(a, last_fault_time)
        result_b = run_episode(b, last_fault_time)

        pair_completed_at = _now_iso()
        pair_elapsed = round(time.time() - pair_start, 1)
        a_ok = result_a.get("status") == "scored" and result_a.get("diagnosis_correct") \
            and result_a.get("trust_correct") is not False
        b_ok = result_b.get("status") == "scored" and result_b.get("diagnosis_correct") \
            and result_b.get("trust_correct") is not False

        if result_a.get("circuit_breaker_tripped") or result_b.get("circuit_breaker_tripped"):
            summary["breaker_trips"] += 1
        if result_a.get("class_demoted"):
            summary["class_demotions"] += 1
        if result_b.get("class_demoted"):
            summary["class_demotions"] += 1

        summary["total"] += 1
        if a_ok and b_ok:
            summary["both_ok"] += 1
            print(f"Pair ({a} -> {b}): OK  [{pair_elapsed}s]  completed_at={pair_completed_at}")
        else:
            summary["issues"] += 1
            print(f"Pair ({a} -> {b}): ISSUE  [{pair_elapsed}s]  completed_at={pair_completed_at}  "
                  f"a={result_a.get('status')}/{result_a.get('diagnosis_correct')}  "
                  f"b={result_b.get('status')}/{result_b.get('diagnosis_correct')}")

        log_result({
            "round_id": round_id, "batch_file": batch_file.name, "pair_index": i, "pair": [a, b],
            "pair_started_at": pair_started_at, "pair_completed_at": pair_completed_at,
            "pair_elapsed_s": pair_elapsed, "a": result_a, "b": result_b,
        })

    round_completed_at = _now_iso()
    batch_elapsed = time.time() - batch_start
    print(f"\n{'=' * 70}\nRound '{round_id}' done: {summary['both_ok']}/{summary['total']} clean, "
          f"{summary['issues']} issue(s), {summary['breaker_trips']} circuit-breaker trip(s), "
          f"{summary['class_demotions']} per-class demotion(s) -- "
          f"elapsed {batch_elapsed / 60:.1f} min ({batch_elapsed:.0f}s) -- "
          f"started_at={round_started_at}  completed_at={round_completed_at}\n{'=' * 70}")
    summary["completed_at"] = round_completed_at
    return {"round_id": round_id, "file": batch_file.name, "elapsed_s": batch_elapsed, **summary}


def pre_run_sanity_gate() -> bool:
    """
    Cheap, fast-failing check that Prometheus and the agent process are
    both actually reachable, BEFORE committing to a multi-hour unattended
    run. Fails loud in ~15s instead of silently timing out 35s into
    episode 1. Reuses _infra_ready() -- same safe, side-effect-free
    checks used throughout the run, not a separate live /handle call.
    """
    print("--- Pre-run sanity gate: Prometheus + p3_agent reachability ---")
    ok, detail = _infra_ready()
    if not ok:
        print(f"Sanity gate FAILED: {detail}. Is the Prometheus port-forward alive? "
              f"Is uvicorn p3_agent:app running on port 8001?")
        return False
    print("Prometheus and the agent process are both reachable.")
    return True


SUMMARY_PATH = HERE / "phase_d_run_summary.json"


def _discover_round_files(rounds_glob: str) -> list[Path]:
    """
    Default multi-round discovery: finds every file matching
    phase_d_round*.json (from phase_d_generate_sample_rounds.py), sorted
    naturally (round1, round2, ... round10, not round1/round10/round2
    lexicographic order) so rounds always run in the intended sequence
    regardless of how many digits the round number has.

    REAL BUG CAUGHT BY TESTING before this was ever run for real: the
    default glob "phase_d_round*.json" also matches
    "phase_d_rounds_manifest.json" (phase_d_generate_sample_rounds.py's
    own coverage-report file) -- "rounds_manifest" starts with "round"
    too. That file has no digit in its stem, so it sorted to position 0
    (ahead of round1) and would have been silently handed to run_batch()
    as if it were a real batch file, crashing when json.loads() choked
    on trying to parse it as a list of [a, b] pairs. Fixed by explicitly
    excluding any file with "manifest" in its name AND requiring the
    stem to actually contain a digit (a real round file always does,
    e.g. "phase_d_round3"; anything without one is not a round file and
    is silently skipped rather than assumed to be one).
    """
    import re

    matches = [Path(p) for p in glob.glob(str(HERE / rounds_glob))]

    def _is_real_round_file(p: Path) -> bool:
        if "manifest" in p.stem:
            return False
        return re.search(r"\d", p.stem) is not None

    def _round_num(p: Path) -> int:
        m = re.search(r"(\d+)", p.stem)
        return int(m.group(1)) if m else 0

    filtered = [p for p in matches if _is_real_round_file(p)]
    skipped = [p for p in matches if p not in filtered]
    if skipped:
        print(f"Note: glob '{rounds_glob}' also matched non-round file(s), skipped: "
              f"{[p.name for p in skipped]}")

    return sorted(filtered, key=_round_num)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-file", type=str, action="append", default=None,
                         help="explicit batch/round file to run -- repeatable "
                              "(e.g. --batch-file phase_d_round1.json --batch-file phase_d_round2.json). "
                              "If given, overrides --rounds-glob discovery entirely.")
    parser.add_argument("--rounds-glob", type=str, default="phase_d_round*.json",
                         help="glob (relative to this script's directory) used to discover round files "
                              "when --batch-file isn't given. Default: phase_d_round*.json "
                              "(phase_d_generate_sample_rounds.py's output). Use "
                              "'phase_d_batch*.json' to run the original two-batch full-matrix files instead.")
    args = parser.parse_args()

    if not pre_run_sanity_gate():
        sys.exit(1)

    _seed_known_can_act()

    if args.batch_file:
        batch_files = [Path(f) for f in args.batch_file]
    else:
        batch_files = _discover_round_files(args.rounds_glob)
        if not batch_files:
            print(f"No files matched glob '{args.rounds_glob}' in {HERE} -- "
                  f"run phase_d_generate_sample_rounds.py (or phase_d_generate_batches.py, "
                  f"with --rounds-glob 'phase_d_batch*.json') first.")
            sys.exit(1)

    for f in batch_files:
        if not f.exists():
            print(f"Missing {f} -- run the generator script first.")
            sys.exit(1)

    print(f"\nWill run {len(batch_files)} round(s)/batch(es) in this single invocation, in order:")
    for f in batch_files:
        print(f"  - {f.name}")

    last_fault_time: dict = {}
    run_summaries = []
    overall_started_at = _now_iso()
    overall_start = time.time()

    for f in batch_files:
        run_summaries.append(run_batch(f, last_fault_time))

    overall_completed_at = _now_iso()
    overall_elapsed = time.time() - overall_start

    print(f"\n\n{'#' * 70}\nPHASE D RUN COMPLETE -- {len(batch_files)} round(s)\n{'#' * 70}")
    print(f"Overall started_at:   {overall_started_at}")
    print(f"Overall completed_at: {overall_completed_at}")

    total_trips = 0
    total_class_demotions = 0
    total_episodes = 0
    total_both_ok = 0
    total_issues = 0
    for s in run_summaries:
        print(f"\n  Round '{s['round_id']}' ({s['file']}):")
        print(f"    {s['both_ok']}/{s['total']} pairs clean, {s['issues']} issue(s), "
              f"{s['breaker_trips']} circuit-breaker trip(s), {s['class_demotions']} per-class demotion(s)")
        print(f"    started_at={s['started_at']}  completed_at={s['completed_at']}  "
              f"elapsed={s['elapsed_s'] / 60:.1f} min ({s['elapsed_s']:.0f}s)")
        total_trips += s["breaker_trips"]
        total_class_demotions += s["class_demotions"]
        total_episodes += s["total"] * 2  # each pair = 2 episodes (a and b)
        total_both_ok += s["both_ok"]
        total_issues += s["issues"]

    print(f"\n{'-' * 70}")
    print(f"COMBINED ACROSS ALL {len(batch_files)} ROUND(S):")
    print(f"  Total pairs run: {sum(s['total'] for s in run_summaries)}  "
          f"({total_episodes} total episodes)")
    print(f"  Clean pairs: {total_both_ok}   Issue pairs: {total_issues}")
    print(f"  Total circuit-breaker trips: {total_trips}  (all auto-recovered -- uncapped)")
    print(f"  Total per-class demotions: {total_class_demotions}  (all auto-recovered -- uncapped)")
    print(f"  Per-class recovery counts: {dict(_class_recovery_count)}")
    print(f"  TOTAL elapsed across all {len(batch_files)} round(s): "
          f"{overall_elapsed / 60:.1f} min ({overall_elapsed:.0f}s, "
          f"{overall_elapsed / 3600:.2f} hours)")

    if total_trips > 0:
        print(f"\n{total_trips} circuit-breaker trip(s) occurred, all auto-recovered (uncapped -- "
              f"see _recovery_log in {SUMMARY_PATH.name} for exact timestamps/rounds). "
              f"Repeated trips across a run are a real finding worth investigating, not something "
              f"the uncapped auto-recovery is meant to paper over.")
    if total_class_demotions > 0:
        repeated = {fc: n for fc, n in _class_recovery_count.items() if n > 1}
        print(f"\n{total_class_demotions} per-class demotion(s) occurred, all auto-recovered (uncapped).")
        if repeated:
            print(f"Classes demoted MORE THAN ONCE across this run: {repeated} -- "
                  f"repeated demotion of the same class is a real finding worth investigating "
                  f"(see the exact timestamps/rounds in _recovery_log within {SUMMARY_PATH.name}), "
                  f"not noise -- same lesson as `bad-rollout`'s 3 demotions in the original run.")

    print(f"\nFull per-pair detail logged to {RESULTS_PATH}")
    print(f"Combined run summary (with full recovery log + all timestamps) written to {SUMMARY_PATH}")

    # Written summary file -- everything printed above, plus the full
    # timestamped recovery log, in one machine-readable place. This is
    # what answers "which run/round did episode X happen in" and "give
    # me the total time for the whole multi-round session" without
    # having to grep stdout or reconcile N separate invocations by hand.
    SUMMARY_PATH.write_text(json.dumps({
        "overall_started_at": overall_started_at,
        "overall_completed_at": overall_completed_at,
        "overall_elapsed_s": round(overall_elapsed, 1),
        "overall_elapsed_hours": round(overall_elapsed / 3600, 2),
        "rounds_run": [f.name for f in batch_files],
        "per_round_summary": run_summaries,
        "combined": {
            "total_pairs": sum(s["total"] for s in run_summaries),
            "total_episodes": total_episodes,
            "total_pairs_clean": total_both_ok,
            "total_pairs_issue": total_issues,
            "total_circuit_breaker_trips": total_trips,
            "total_class_demotions": total_class_demotions,
            "per_class_recovery_counts": dict(_class_recovery_count),
        },
        "recovery_log": _recovery_log,
    }, indent=2))


if __name__ == "__main__":
    main()
