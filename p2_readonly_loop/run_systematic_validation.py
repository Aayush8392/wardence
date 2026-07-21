"""
Systematic validation batch -- built after two rounds of Kimi review on the
fault-class buildout and on this runner's own logic.

Design (round 1, on WHY this runner exists at all): run_mixed_episodes.py's
random shuffle (5 episodes/class) doesn't guarantee every ordered class-pair
transition gets tested, and the diagnosis priority ordering (OOM > Evicted >
crash-loop) has never been tested under a genuine two-signal conflict, only
each signal in isolation.

Design (round 2, on retry methodology): 1 rep/pair (32 episodes for a clean
pass) + retest-on-failure, not 2 reps/pair upfront -- chaos-daemon failures
are STATEFUL/correlated, so both reps of a pair would fail together, doubling
runtime without adding real independence. A pair that scores wrong gets
retested; passing on retest = infra flake, failing again = stop for real
investigation.

Design (round 3, a harsh line-by-line pass on THIS file): several real bugs
found and fixed here --
  - episode-to-score attribution was done via `ORDER BY scored_at DESC LIMIT
    1`, which is undefined under a tie (SQLite's scored_at has 1-second
    resolution, and two fast episodes can land in the same second) --
    replaced with explicit episode_id set-diffing (before/after) so the
    exact episode just created is always known, never inferred from
    ordering or count deltas.
  - retesting the WHOLE pair on any failure conflates two independent
    flakes (e.g. A genuinely wrong, B silently flakes on retest) into a
    false "real bug, stop" conclusion -- now retests only the specific
    failed episode(s) for cross-target pairs, and the whole pair only for
    same-target pairs (the only case where A could genuinely contaminate
    B's signal).
  - no cleanup of chaos resources left over from a previous crashed/
    interrupted run -- added at startup, before the sanity gate.
  - no SIGINT/SIGTERM handler -- a 2-hour run WILL get interrupted; added
    best-effort cleanup on interrupt.
  - failure paths returned exit code 0 -- fixed via sys.exit.
  - the compound-signal check resized the memory stressor via a raw
    string .replace() on the manifest YAML -- latent corruption risk if
    OOM_STRESS_SIZE and some other field ever collided as substrings;
    replaced with build_oom_manifest's new explicit `size` parameter.

Design (round 4, per reviews/04 -- Kimi and an independent Fable pass
converged on the same conclusions): after finding FOUR distinct, real,
reproducible bugs across two rounds of running this exact matrix
(chaos-daemon's stale task-ID cache, the wrong PID target for
crash-loop, a missing CrashLoopBackOff verification signal, and oom's
stressor-vs-app OOM-victim bug), it became clear every single one was a
SINGLE-MECHANISM reliability failure, not a cross-class interaction bug
-- exactly what a cheap up-front check catches, at a cost of minutes
instead of the hours each of these cost to discover mid-matrix.
  - added class_health_checks(): each real class run standalone,
    CLASS_HEALTH_CHECK_REPS times each, BEFORE the 16-pair matrix. Must
    pass EVERY rep, not just one -- oom's old mechanism looked "20/20
    reliable" while actually succeeding by a ~5% kernel coin-flip (see
    reviews/04); a single passing check can't tell a fixed mechanism
    from a lucky one. This also subsumes what the old pre-run sanity
    gate's own crash-loop check did, so that redundant separate check
    was removed -- the gate now only confirms the agent is reachable
    (cheap, fast-fails before spending time on the health checks at all
    if the whole pipeline is down), and class_health_checks does the
    real mechanism verification for all three classes uniformly. This
    also fixes a real bug Kimi caught: last_fault_time["carts"] used to
    be stamped with time.time() AFTER the gate returned, up to 30-60s
    later than the gate's own crash-loop fault actually fired, shorting
    the first carts-involving pair's recency buffer. Removing the
    gate's own injection entirely and letting run_episode's existing
    per-call tracking populate last_fault_time naturally fixes this by
    construction rather than patching the timestamp.
  - added checkpoint/resume, as a ROTATION not a permanent skip-list:
    every restart during this validation effort went back to pair 1,
    re-running everything that already passed. But permanently trusting
    "already passed, skip forever" is also wrong given today's actual
    failure pattern -- every bug was a mechanism that worked fine early
    in the session and degraded from cumulative use (kubelet backoff
    growing, chaos-daemon's cache going stale, stale pod objects
    accumulating), so a pair that passed hours ago isn't good evidence
    it's still reliable now. Instead the runner persists a single
    resume_index (same --seed only, since a different seed means
    different random `none` targets) and on resume runs all 16 pairs as
    a rotation STARTING at the one that failed -- fast feedback on
    whatever was just fixed, since it runs first, while still eventually
    re-confirming every earlier pass by wrapping back around to it,
    rather than trusting it indefinitely. Class health checks always
    re-run fresh on every invocation regardless of checkpoint state --
    they're specifically a "did something regress" guard, so resuming
    past them defeats their purpose. Health checks also gained a
    `--health-check-classes` filter, to re-verify only a specific class
    after a targeted fix without re-running the other two -- safe only
    when you've confirmed their actual code didn't change since their
    last clean pass.
  - the compound-signal check now verifies the memory stressor actually
    reached a running state before starting the concurrent disk-fill,
    instead of assuming apply_manifest succeeding means the stressor is
    live.

Usage:
    python3 run_systematic_validation.py [--seed N]
"""

import argparse
import json
import random
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from injector import (
    FAULT_CONFIG,
    _cleanup_disk_full_files,
    apply_manifest,
    build_oom_manifest,
    delete_chaos_resource,
    ensure_db,
    run_disk_full_injection,
)

REAL_CLASSES = ["crash-loop", "oom", "disk-full"]
ALL_CLASSES = REAL_CLASSES + ["none"]
CONTROL_TARGETS = [FAULT_CONFIG[c]["target"] for c in REAL_CLASSES]

SETTLE_SECONDS = 35
NONE_SETTLE_SECONDS = 10
TARGET_RECENCY_WINDOW_S = 215  # 3min diagnosis lookback + 30s eviction/scrape-jitter buffer

AGENT_URL = "http://localhost:8000/diagnose"

COMPOUND_MEMORY_STRESS_SIZE = "600M"  # queue-master's memory limit is 500Mi

FLAKE_LOG_PATH = Path.home() / "wardence_p2_data" / "validation_flakes.jsonl"
CHECKPOINT_PATH = Path.home() / "wardence_p2_data" / "validation_checkpoint.json"

CLASS_HEALTH_CHECK_REPS = 2  # not 1 -- see module docstring, round 4

# Chaos Mesh resource kinds this project's injector ever creates.
CHAOS_RESOURCE_KINDS = ["schedule", "stresschaos"]

_interrupted = False


def _signal_cleanup(signum, frame):
    global _interrupted
    _interrupted = True
    print("\nInterrupted -- cleaning up lingering chaos resources before exit...")
    cleanup_stale_chaos_resources()
    sys.exit(1)


def cleanup_stale_chaos_resources():
    """
    Deletes any wardence-* Schedule/StressChaos left behind by a previous
    crashed or interrupted run. Without this, a leftover resource can
    pre-contaminate the FIRST real episode of a fresh run before the
    injector even does anything -- the agent would see a genuine signal,
    score correctly "by accident," and the run would look validated when
    the signal source was actually stale garbage from before.
    """
    for kind in CHAOS_RESOURCE_KINDS:
        result = subprocess.run(
            ["kubectl", "get", kind, "-n", "chaos-mesh", "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True,
            text=True,
        )
        names = [n for n in result.stdout.split() if n.startswith("wardence-")]
        for name in names:
            print(f"Cleaning up stale {kind}/{name} from a previous run...")
            delete_chaos_resource(kind, name)


def run(script: str, extra_args: list[str] | None = None) -> bool:
    """
    Deliberately NOT capturing output -- capture_output=True buffers the
    child's stdout/stderr until it fully exits before printing anything,
    which made a single crash-loop attempt (40-95+s, longer with retries)
    look completely frozen with zero feedback. Letting the child inherit
    the parent's stdout/stderr streams its output live instead, which
    matters for a run meant to be watched in real time.
    """
    cmd = [sys.executable, script] + (extra_args or [])
    result = subprocess.run(cmd)
    return result.returncode == 0


def wait_for_target_recency(target: str, last_fault_time: dict):
    if target not in last_fault_time:
        return
    elapsed = time.time() - last_fault_time[target]
    remaining = TARGET_RECENCY_WINDOW_S - elapsed
    if remaining > 0:
        print(f"waiting {remaining:.0f}s extra so {target}'s last fault clears the diagnosis recency window")
        time.sleep(remaining)


def _episode_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT episode_id FROM episodes")}


def record_none_episode(conn: sqlite3.Connection, target: str) -> str:
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


def run_episode(fault_class: str, last_fault_time: dict) -> tuple[bool, bool | None, str | None]:
    """
    Returns (ran_ok, correct, target). ran_ok False = hard failure, stop
    the whole batch. correct is None if no episode was actually scored
    for the RIGHT episode_id (injection failed, or -- the bug this
    version fixes -- a stale pending episode from an earlier crashed run
    got scored instead of the one just created).

    Attribution is done by explicit episode_id set-diffing (before vs
    after), not by scored_at ordering (undefined under a same-second tie)
    or by row-count deltas (can't distinguish "my new episode was scored"
    from "some OLD unrelated pending episode was scored instead").
    """
    conn = ensure_db()
    ids_before = _episode_ids(conn)
    conn.close()

    if fault_class == "none":
        target = random.choice(CONTROL_TARGETS)
        wait_for_target_recency(target, last_fault_time)
        conn = ensure_db()
        record_none_episode(conn, target)
        conn.close()
        time.sleep(NONE_SETTLE_SECONDS)
    else:
        target = FAULT_CONFIG[fault_class]["target"]
        wait_for_target_recency(target, last_fault_time)
        if not run("injector.py", ["--class", fault_class]):
            print("Injector script crashed, stopping.")
            return False, None, target
        last_fault_time[target] = time.time()
        time.sleep(SETTLE_SECONDS)

    conn = ensure_db()
    ids_after = _episode_ids(conn)
    new_ids = ids_after - ids_before
    conn.close()

    if not new_ids:
        print(f"No episode was created for {fault_class} on {target} -- injection failed all internal retries.")
        return True, None, target
    if len(new_ids) > 1:
        # Shouldn't happen given the strictly sequential design, but
        # don't silently guess which one is "ours" if it ever does.
        print(f"WARNING: {len(new_ids)} new episodes appeared in one step (expected 1) -- {new_ids}")
    episode_id = new_ids.pop()

    if not run("scorer.py"):
        print("Scorer failed, stopping.")
        return False, None, target

    conn = ensure_db()
    row = conn.execute("SELECT correct FROM scores WHERE episode_id = ?", (episode_id,)).fetchone()
    conn.close()
    if row is None:
        print(f"WARNING: episode {episode_id} was created but never scored -- scorer picked a different episode.")
        return True, None, target

    return True, bool(row[0]), target


def run_pair(a: str, b: str, last_fault_time: dict) -> tuple[bool, bool | None, bool | None, str | None, str | None]:
    """Returns (ran_ok, a_correct, b_correct, a_target, b_target)."""
    ok, a_correct, a_target = run_episode(a, last_fault_time)
    if not ok:
        return False, None, None, a_target, None
    ok, b_correct, b_target = run_episode(b, last_fault_time)
    if not ok:
        return False, a_correct, None, a_target, b_target
    return True, a_correct, b_correct, a_target, b_target


def pre_run_sanity_gate() -> bool:
    """
    Confirms the agent/scorer pipeline is reachable at all, BEFORE
    spending time on the (more expensive) class health checks below.
    Deliberately does NOT do its own injection anymore -- that used to
    duplicate what class_health_checks now does more thoroughly (2 reps,
    not 1), and a real bug it introduced (the crash-loop fault's actual
    timestamp being up to 30-60s earlier than when last_fault_time got
    stamped, per reviews/04) is now moot since there's no separate gate
    injection to track at all.
    """
    print("--- Pre-run sanity gate: agent reachability ---")
    try:
        resp = requests.post(AGENT_URL, json={"target": "carts", "namespace": "sock-shop"}, timeout=5)
        if resp.status_code != 200:
            print(f"Sanity gate FAILED: agent returned {resp.status_code}. Is it running (port 8000)?")
            return False
    except requests.RequestException as e:
        print(f"Sanity gate FAILED: agent unreachable ({e}). Is uvicorn running on port 8000?")
        return False
    print("Agent reachable.")
    return True


def class_health_checks(last_fault_time: dict, classes: list[str] | None = None) -> bool:
    """
    Confirms each real fault class's injection mechanism actually works,
    standalone, BEFORE the 16-pair matrix. See module docstring (round 4)
    for the full reasoning -- every bug found across this validation
    effort was a single-mechanism reliability failure, exactly what this
    catches cheaply instead of discovering it deep in the matrix.

    `classes` lets a caller re-check only specific classes after a
    targeted fix, instead of re-running all three -- safe ONLY when the
    other classes' actual injection/diagnosis code genuinely didn't
    change since their last clean pass (verify that by hand before
    using this, same as any other "skip the expensive check" shortcut).
    """
    classes_to_check = classes if classes is not None else REAL_CLASSES
    print(f"\n--- Class health checks ({CLASS_HEALTH_CHECK_REPS} reps each, before the pair matrix) ---")
    for cls in classes_to_check:
        for rep in range(1, CLASS_HEALTH_CHECK_REPS + 1):
            print(f"  {cls} health check {rep}/{CLASS_HEALTH_CHECK_REPS}...")
            ok, correct, _ = run_episode(cls, last_fault_time)
            if not ok:
                print(f"HEALTH CHECK FAILED: {cls} crashed the injector/scorer script.")
                return False
            if not correct:
                print(
                    f"HEALTH CHECK FAILED: {cls} rep {rep}/{CLASS_HEALTH_CHECK_REPS} did not verify "
                    f"correctly -- fix this mechanism before running the matrix."
                )
                return False
    print("All class health checks passed.")
    return True


def build_pairs() -> list[tuple[str, str]]:
    """Every ordered (A, B) pair, including self-pairs (A==B) -- 16 total
    for 4 classes. One pass, not repeated -- see module docstring."""
    return [(a, b) for a in ALL_CLASSES for b in ALL_CLASSES]


def load_checkpoint(seed: int) -> dict:
    """
    Only reused if the seed matches -- a different seed means different
    random `none` targets, so a resume point recorded under a different
    seed isn't valid under this one.

    Deliberately NOT a permanent "pairs passed, skip forever" list.
    Today's actual bugs all had the same shape: a mechanism that worked
    fine early in a long session degraded from cumulative use (kubelet
    backoff growing, chaos-daemon's cache going stale, stale pod objects
    accumulating) -- a pair that passed hours ago isn't good evidence
    it's still reliable now. Instead this stores a single resume_index:
    on a fresh start it's 0 (pair 1 first, normal order); after a
    stop-for-investigation, it's the index of the pair that failed. The
    matrix is then run as a ROTATION starting at that index and wrapping
    around through all 16 pairs -- fast feedback on whatever was just
    fixed (it runs first), while still eventually re-confirming every
    earlier "passed" pair too, instead of trusting them indefinitely.
    """
    if CHECKPOINT_PATH.exists():
        data = json.loads(CHECKPOINT_PATH.read_text())
        if data.get("seed") == seed:
            return data
    return {"seed": seed, "resume_index": 0}


def save_checkpoint(checkpoint: dict):
    CHECKPOINT_PATH.parent.mkdir(exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(checkpoint))


def clear_checkpoint():
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()


def _log_flake(pair: tuple[str, str], detail: str):
    FLAKE_LOG_PATH.parent.mkdir(exist_ok=True)
    with open(FLAKE_LOG_PATH, "a") as f:
        f.write(
            json.dumps(
                {"pair": list(pair), "detail": detail, "at": datetime.now(timezone.utc).isoformat()}
            )
            + "\n"
        )


def handle_pair(a: str, b: str, last_fault_time: dict) -> bool | None:
    """
    Returns True (pass, possibly after retest), False (real failure --
    caller should stop), or None (a script crashed -- caller should stop).

    Same-target pairs (only self-pairs, given FAULT_CONFIG's 1:1
    class->target mapping) retest the WHOLE pair on failure, since A's
    signal could genuinely still be live when B runs. Cross-target pairs
    retest ONLY the episode(s) that actually failed -- retesting the
    other one too would just add unrelated failure surface and risk
    conflating two independent flakes into a false "reproduced bug."
    """
    ok, a_correct, b_correct, a_target, b_target = run_pair(a, b, last_fault_time)
    if not ok:
        return None

    if bool(a_correct) and bool(b_correct):
        print(f"Pair ({a} -> {b}): PASS")
        return True

    same_target = a_target is not None and a_target == b_target
    print(f"Pair ({a} -> {b}): WRONG on first attempt (a_correct={a_correct}, b_correct={b_correct}) -- retesting...")

    if same_target:
        ok, a_correct2, b_correct2, _, _ = run_pair(a, b, last_fault_time)
        if not ok:
            return None
        passed = bool(a_correct2) and bool(b_correct2)
    else:
        a_correct2, b_correct2 = a_correct, b_correct
        if not a_correct:
            ok, a_correct2, _ = run_episode(a, last_fault_time)
            if not ok:
                return None
        if not b_correct:
            ok, b_correct2, _ = run_episode(b, last_fault_time)
            if not ok:
                return None
        passed = bool(a_correct2) and bool(b_correct2)

    if passed:
        print(f"Pair ({a} -> {b}): passed on retest -- logged as infra flake, excluded from pair result.")
        _log_flake((a, b), f"first attempt a_correct={a_correct} b_correct={b_correct}, passed on retest")
        return True

    print(f"Pair ({a} -> {b}): FAILED AGAIN on retest -- likely a real bug, not infra flake.")
    return False


def run_compound_signal_check():
    """
    Fires OOM's memory stressor and disk-full's exec-based fill CONCURRENTLY
    against queue-master, then asks the agent to diagnose it. Not scored --
    there's no single correct ground-truth label for a pod that's genuinely
    both OOM-pressured and disk-pressured at once.
    """
    print("\n--- Compound-signal check (not scored, exploratory) ---")
    cfg = FAULT_CONFIG["disk-full"]  # queue-master's namespace/target/container

    chaos_name = f"wardence-compound-{uuid.uuid4().hex[:8]}"
    manifest = build_oom_manifest(chaos_name, cfg, size=COMPOUND_MEMORY_STRESS_SIZE)
    apply_manifest(manifest)

    # Confirm the stressor actually reached the cluster before assuming
    # it's live -- apply_manifest succeeding only means the k8s API
    # accepted the request, same class of assumption that caused the
    # crash-loop/oom bugs found earlier this session.
    time.sleep(5)
    check = subprocess.run(
        ["kubectl", "get", "stresschaos", chaos_name, "-n", "chaos-mesh"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        print(f"WARNING: compound-signal stressor {chaos_name} not found after apply -- "
              f"this check may just be disk-full alone, not a genuine compound signal.")

    print(f"Memory stressor ({COMPOUND_MEMORY_STRESS_SIZE}) applied to {cfg['target']}, "
          f"now concurrently running the disk-fill loop for {cfg['duration_s']}s...")

    try:
        run_disk_full_injection(cfg, cfg["duration_s"])
    finally:
        delete_chaos_resource("stresschaos", chaos_name)
        _cleanup_disk_full_files(cfg["target"], cfg["namespace"], cfg["container"])

    time.sleep(SETTLE_SECONDS)

    resp = requests.post(AGENT_URL, json={"target": cfg["target"], "namespace": cfg["namespace"]}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    print(f"Compound-signal diagnosis: {result.get('diagnosis')} (confidence={result.get('confidence')})")
    print(f"Full tool output: {result.get('tool_output')}")
    print(
        "Expected per the locked priority ordering: 'oom' should win (checked before "
        "Evicted) if both signals are genuinely present."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42, help="seed for none-episode target selection (reproducibility)")
    parser.add_argument(
        "--health-check-only", action="store_true",
        help="run the agent reachability check + class health checks, then exit without starting the pair matrix",
    )
    parser.add_argument(
        "--health-check-classes", type=str, default=None,
        help="comma-separated subset of classes to health-check (default: all 3) -- "
             "only skip a class if you've verified its actual code genuinely didn't "
             "change since its last clean pass",
    )
    args = parser.parse_args()
    health_check_classes = (
        [c.strip() for c in args.health_check_classes.split(",")] if args.health_check_classes else None
    )
    if health_check_classes:
        unknown = [c for c in health_check_classes if c not in REAL_CLASSES]
        if unknown:
            print(f"Unknown class(es) in --health-check-classes: {unknown}. Valid: {REAL_CLASSES}")
            sys.exit(1)
    random.seed(args.seed)

    signal.signal(signal.SIGINT, _signal_cleanup)
    signal.signal(signal.SIGTERM, _signal_cleanup)

    cleanup_stale_chaos_resources()

    if not pre_run_sanity_gate():
        sys.exit(1)

    last_fault_time: dict[str, float] = {}

    # Always fresh, never skipped by checkpoint -- these specifically
    # guard against regressions since the last run, so resuming past
    # them would defeat their purpose. See module docstring, round 4.
    if not class_health_checks(last_fault_time, classes=health_check_classes):
        sys.exit(1)

    if args.health_check_only:
        print("\n--health-check-only set -- stopping here, not starting the pair matrix.")
        sys.exit(0)

    checkpoint = load_checkpoint(args.seed)
    pairs = build_pairs()
    n = len(pairs)
    resume_index = checkpoint.get("resume_index", 0) % n
    rotated_pairs = pairs[resume_index:] + pairs[:resume_index]

    if resume_index != 0:
        print(f"\nResuming as a rotation starting at pair {resume_index + 1}/{n} (the one that needed investigation) "
              f"-- wraps around through all {n} pairs, re-confirming earlier passes too, not just skipping them.")
    print(f"\nSystematic validation: {n} ordered pairs, one full rotation")

    for i, (a, b) in enumerate(rotated_pairs, start=1):
        original_index = (resume_index + i - 1) % n
        print(f"\n=== Pair {i}/{n} (position {original_index + 1}/{n} in the base order): {a} -> {b} ===")
        result = handle_pair(a, b, last_fault_time)
        if result is None:
            checkpoint["resume_index"] = original_index
            save_checkpoint(checkpoint)
            print("Stopping -- a script crashed (not a wrong-verdict case). "
                  "Resume by re-running -- starts right back at this pair.")
            sys.exit(1)
        if result is False:
            checkpoint["resume_index"] = original_index
            save_checkpoint(checkpoint)
            print("Stopping for investigation -- a pair failed on both the original attempt and retest. "
                  "Resume by re-running once fixed -- starts right back at this pair.")
            sys.exit(1)

        # Advance past this pair even on success, so a LATER crash in
        # this same rotation resumes after it, not re-running it again.
        checkpoint["resume_index"] = (original_index + 1) % n
        save_checkpoint(checkpoint)

    print(f"\nCompleted a full rotation -- all {n} pairs passed (directly or after retest).")
    if FLAKE_LOG_PATH.exists():
        print(f"Infra-flake exclusions logged to {FLAKE_LOG_PATH} -- review if any pair recurs across future runs.")

    run_compound_signal_check()
    clear_checkpoint()  # full rotation succeeded -- don't let a future fresh run look like a resume
    sys.exit(0)


if __name__ == "__main__":
    main()
