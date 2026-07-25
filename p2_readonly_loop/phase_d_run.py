"""
Phase D: cross-class pairwise spot-check. Runs pre-generated batches of
ordered (A, B) class pairs sequentially through the REAL P3 pipeline
(injector -> settle -> p3_scorer.py, which calls the real p3_agent and
takes real actions for can_act classes) -- looking for cross-class
contamination that Phase 2's targeted per-class checks wouldn't catch.

Unlike run_systematic_validation.py, this does NOT stop or retry on
failure. Phase D is a spot-check, not a gate (see wardence_buildlog.md's
Phase D design notes) -- a failed pair is a FINDING to review afterward,
not a reason to halt an unattended overnight run. Every episode's outcome
(including exceptions) is caught, logged to phase_d_results.jsonl, and
printed; the run always proceeds to the next pair no matter what.

Prerequisites (must already be running):
    - Prometheus port-forward
    - uvicorn p3_agent:app --reload --app-dir p3_trust_action --port 8001
    - all 6 auto-fix classes should be can_act -- run
      p3_trust_action/phase_d_promote_all.py first if not.
    - phase_d_batch1.json / phase_d_batch2.json exist (run
      phase_d_generate_batches.py first if not).

Usage:
    python3 phase_d_run.py                                    # batch1 then batch2
    python3 phase_d_run.py --batch-file phase_d_batch1.json    # one file only
"""

import argparse
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
# already prints "CIRCUIT BREAKER TRIPPED: ..." when it happens. Capped,
# not infinitely auto-recovered -- a bug tripping the breaker repeatedly
# is itself a real finding worth NOT masking forever.
MAX_AUTO_RECOVERIES = 3
_auto_recovery_count = 0

# Per-class demotion auto-recovery -- a real gap in the first Phase D run
# (2026-07-24/25): the circuit-breaker recovery above only catches the
# GLOBAL breaker tripping. An individual class demoting on its own
# through the normal trust-engine path (a real flap/misdiagnosis on just
# that one class, no breaker involved) was NOT recovered -- that class
# just sat report_only (diagnosis-only) for the rest of the night,
# losing real-action coverage for however many of its remaining pairs
# were left. Found live, mid-run, by direct question -- fixed here for
# any future run. Same reasoning and cap pattern as the breaker recovery:
# re-promote immediately so coverage isn't lost, but cap it so a class
# that keeps demoting over and over in one run (a real finding -- see
# `bad-rollout`'s 3 demotions in the first run) still surfaces as one,
# not gets silently papered over forever. Demotions themselves are
# ALWAYS recorded in trust_history/failure_log regardless of how fast
# they're recovered from -- auto-recovery doesn't hide the finding, it
# just stops it from costing the rest of the night's real-action coverage.
MAX_PER_CLASS_AUTO_RECOVERIES = 3
_class_recovery_count: dict[str, int] = {fc: 0 for fc in REAL_CLASSES}
# Tracks whether each class was can_act as of the last check -- lets a
# fresh can_act->report_only transition be told apart from "already sitting
# report_only, still re-earning naturally" (which must NOT be force-promoted,
# or the whole earned-streak mechanism -- the actual thing being tested --
# would never get exercised at all). Seeded from real DB state at startup,
# not assumed, in case promote_all_to_can_act wasn't actually run first.
_known_can_act: dict[str, bool] = {}


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
    print(f"\n{'~' * 70}")
    print(f"{fault_class}: DEMOTED (can_act -> report_only) -- detected after this episode's scoring.")
    if _class_recovery_count[fault_class] < MAX_PER_CLASS_AUTO_RECOVERIES:
        _class_recovery_count[fault_class] += 1
        _promote_one_to_can_act(fault_class)
        _known_can_act[fault_class] = True
        print(f"Auto-recovery {_class_recovery_count[fault_class]}/{MAX_PER_CLASS_AUTO_RECOVERIES} for "
              f"{fault_class}: re-promoted so its remaining pairs still test real actions. "
              f"The demotion itself is still recorded in trust_history -- this doesn't hide the finding.")
        print(f"{'~' * 70}\n")
        return {"demoted": True, "recovered": True}
    print(f"Per-class auto-recovery cap ({MAX_PER_CLASS_AUTO_RECOVERIES}) already used for {fault_class} -- "
          f"NOT re-promoting this time. Repeated demotions for one class in one run is a real finding, "
          f"not something to keep masking. Its remaining pairs will run report_only until reviewed.")
    print(f"{'~' * 70}\n")
    return {"demoted": True, "recovered": False}


def _handle_circuit_breaker_trip(scorer_output: str) -> bool:
    """
    Called the moment p3_scorer.py's own output shows the breaker tripped.
    Returns True if auto-recovered (re-promoted all 6, run continues
    testing real actions), False if the recovery cap was already hit
    (left demoted -- a real, surfaced finding, not silently masked).
    """
    global _auto_recovery_count
    print(f"\n{'!' * 70}")
    print("CIRCUIT BREAKER TRIPPED (detected in p3_scorer.py output above) -- "
          "every can_act class was just force-demoted to report_only.")
    if _auto_recovery_count < MAX_AUTO_RECOVERIES:
        _auto_recovery_count += 1
        _promote_all_to_can_act()
        print(f"Auto-recovery {_auto_recovery_count}/{MAX_AUTO_RECOVERIES}: re-promoted all 6 classes to "
              f"can_act so the rest of the run keeps testing real actions.")
        print(f"{'!' * 70}\n")
        return True
    print(f"Auto-recovery cap ({MAX_AUTO_RECOVERIES}) already used -- NOT re-promoting this time. "
          f"3+ breaker trips in one run is itself a real finding, not something to keep masking. "
          f"Remaining episodes will run report_only (diagnosis-only) until this is manually reviewed.")
    print(f"{'!' * 70}\n")
    return False


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


def wait_for_target_recency(target: str, last_fault_time: dict):
    if target not in last_fault_time:
        return
    elapsed = time.time() - last_fault_time[target]
    remaining = TARGET_RECENCY_WINDOW_S - elapsed
    if remaining > 0:
        print(f"  waiting {remaining:.0f}s so {target}'s last fault clears the recency window")
        time.sleep(remaining)


def run_episode(fault_class: str, last_fault_time: dict) -> dict:
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
            target = random.choice(CONTROL_TARGETS)
            wait_for_target_recency(target, last_fault_time)
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
    pairs = json.loads(batch_file.read_text())
    n = len(pairs)
    batch_start = time.time()
    print(f"\n{'=' * 70}\nStarting batch {batch_file.name}: {n} pairs\n{'=' * 70}")

    summary = {"total": 0, "both_ok": 0, "issues": 0, "breaker_trips": 0, "class_demotions": 0}

    for i, (a, b) in enumerate(pairs, start=1):
        print(f"\n--- {batch_file.name} pair {i}/{n}: {a} -> {b} ---")
        pair_start = time.time()

        result_a = run_episode(a, last_fault_time)
        result_b = run_episode(b, last_fault_time)

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
            print(f"Pair ({a} -> {b}): OK  [{pair_elapsed}s]")
        else:
            summary["issues"] += 1
            print(f"Pair ({a} -> {b}): ISSUE  [{pair_elapsed}s]  "
                  f"a={result_a.get('status')}/{result_a.get('diagnosis_correct')}  "
                  f"b={result_b.get('status')}/{result_b.get('diagnosis_correct')}")

        log_result({
            "batch_file": batch_file.name, "pair_index": i, "pair": [a, b],
            "pair_elapsed_s": pair_elapsed, "a": result_a, "b": result_b,
        })

    batch_elapsed = time.time() - batch_start
    print(f"\n{'=' * 70}\nBatch {batch_file.name} done: {summary['both_ok']}/{summary['total']} clean, "
          f"{summary['issues']} issue(s), {summary['breaker_trips']} circuit-breaker trip(s), "
          f"{summary['class_demotions']} per-class demotion(s) -- "
          f"elapsed {batch_elapsed / 60:.1f} min ({batch_elapsed:.0f}s)\n{'=' * 70}")
    return {"file": batch_file.name, "elapsed_s": batch_elapsed, **summary}


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-file", type=str, default=None,
                         help="run only this one batch file instead of batch1 then batch2")
    args = parser.parse_args()

    if not pre_run_sanity_gate():
        sys.exit(1)

    _seed_known_can_act()

    if args.batch_file:
        batch_files = [Path(args.batch_file)]
    else:
        batch_files = [HERE / "phase_d_batch1.json", HERE / "phase_d_batch2.json"]

    for f in batch_files:
        if not f.exists():
            print(f"Missing {f} -- run phase_d_generate_batches.py first.")
            sys.exit(1)

    last_fault_time: dict = {}
    run_summaries = []
    overall_start = time.time()

    for f in batch_files:
        run_summaries.append(run_batch(f, last_fault_time))

    overall_elapsed = time.time() - overall_start

    print(f"\n\n{'#' * 70}\nPHASE D RUN COMPLETE\n{'#' * 70}")
    total_trips = 0
    total_class_demotions = 0
    for s in run_summaries:
        print(f"  {s['file']}: {s['both_ok']}/{s['total']} clean, {s['issues']} issue(s), "
              f"{s['breaker_trips']} circuit-breaker trip(s), {s['class_demotions']} per-class demotion(s) "
              f"-- {s['elapsed_s'] / 60:.1f} min ({s['elapsed_s']:.0f}s)")
        total_trips += s["breaker_trips"]
        total_class_demotions += s["class_demotions"]
    print(f"\nTOTAL elapsed across both scripts: {overall_elapsed / 60:.1f} min ({overall_elapsed:.0f}s)")
    if total_trips > 0:
        print(f"\n{total_trips} circuit-breaker trip(s) occurred during this run "
              f"({_auto_recovery_count}/{MAX_AUTO_RECOVERIES} auto-recoveries used). "
              f"CHECK CURRENT TRUST STATE before trusting any 'issue' counts above at face value -- "
              f"if the recovery cap was hit, some later pairs ran report-only (diagnosis-only, no real "
              f"action/durability check) even though they're logged as regular auto-fix classes.")
    if total_class_demotions > 0:
        cap_hit = [fc for fc, n in _class_recovery_count.items() if n >= MAX_PER_CLASS_AUTO_RECOVERIES]
        print(f"\n{total_class_demotions} per-class demotion(s) occurred during this run. "
              f"Per-class recovery counts: {dict(_class_recovery_count)}.")
        if cap_hit:
            print(f"Recovery cap was hit for: {cap_hit} -- these classes' remaining pairs after the cap "
                  f"ran report_only (diagnosis-only, no real action/durability check). Repeated demotion "
                  f"of the same class in one run is a real finding worth investigating, not noise.")
    print(f"Full per-pair detail logged to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
