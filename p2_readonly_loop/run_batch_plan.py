"""
run_batch_plan.py -- resumable, pausable multi-class episode batch runner.

Real design, from a 2026-07-28 discussion after a real overnight batch had
to be manually stopped mid-way. Confirmed by reading run_episodes.py's own
loop structure: injector.py/scorer.py are both real, live cluster
mutations that must run to their own natural completion -- there is
exactly ONE safe pause point, between one episode finishing and the next
starting. Never mid-injection, never mid-diagnosis/scoring.

Three ways to request a pause, all checked ONLY at that one safe point:
1. A 5-second interactive prompt after every episode -- press Enter to
   pause, wait to auto-continue.
2. A PAUSE_REQUESTED flag file (touch it from another terminal/session).
3. Ctrl+C (SIGINT) -- a real handler sets a flag instead of killing
   immediately. The current injector.py/scorer.py subprocess call (run in
   its own process group by run_episodes.py's `run()`, so it does NOT
   receive the terminal's SIGINT directly) finishes naturally; the batch
   stops at the next safe point, same as the other two mechanisms.

Progress persists in PLAN_PATH (JSON), read-modify-write after every
completed episode -- an unclean kill (crash, power loss) still leaves an
accurate "last known good" record, not just a clean pause. On completion,
the file is archived with a timestamp rather than silently overwritten by
the next batch.

check_all_baselines.py runs automatically before starting/resuming --
refuses to proceed if real drift is found, rather than silently building
more episodes on top of a possibly-broken cluster state (e.g. a past
unclean interruption that didn't actually land at the safe point).

Usage:
    # Start a fresh plan. IGNORED if an incomplete plan already exists in
    # PLAN_PATH -- that resumes instead. Archive/delete the file manually
    # to force a fresh start over an incomplete one.
    python3 run_batch_plan.py --plan memory-leak:51,network-partition:51,connection-pool-exhaustion:51

    # Resume whatever's already in progress, no new plan needed.
    python3 run_batch_plan.py
"""

import argparse
import json
import select
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_episodes import (  # noqa: E402
    SETTLE_SECONDS,
    TARGET_RECENCY_WINDOW_S,
    _Tee,
    _update_timings,
    run as run_script,
    wait_for_infra_ready,
)
from injector import FAULT_CONFIG  # noqa: E402 -- real per-class targets, not hardcoded

HERE = Path(__file__).parent
PLAN_PATH = HERE / "batch_plan_progress.json"
PAUSE_FLAG_PATH = HERE / "PAUSE_REQUESTED"
BASELINE_CHECK_SCRIPT = HERE / "check_all_baselines.py"
OUTPUT_DIR = HERE / "output"
PROMPT_WINDOW_S = 5

_stop_requested = False


def _sigint_handler(signum, frame):
    global _stop_requested
    if _stop_requested:
        return  # second Ctrl+C while already stopping -- just wait, don't nag again
    print("\n  Ctrl+C caught -- will stop at the next safe point (after the "
          "current episode finishes), not immediately. Press Ctrl+C again "
          "only if you understand that may interrupt a live cluster change.")
    _stop_requested = True


def _prompt_for_pause() -> bool:
    """5-second window, only ever called between episodes (the one safe
    point). Any input at all requests a pause; no input auto-continues.
    select() means this never blocks past the window.

    Real bug fixed 2026-07-28: Ctrl+C during this wait interrupts
    select() and correctly runs _sigint_handler (setting _stop_requested),
    but Python auto-retries the interrupted select() call afterward (PEP
    475) rather than returning immediately -- so the ORIGINAL version of
    this function only ever checked whether Enter was pressed, silently
    ignoring that _stop_requested had already become True mid-wait. Fixed
    by checking it explicitly once select() actually returns, so Ctrl+C
    during the prompt pauses at THIS episode boundary, not one episode
    later."""
    print(f"  Safe to pause here. Press Enter within {PROMPT_WINDOW_S}s to "
          f"pause, or wait to continue...", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], PROMPT_WINDOW_S)
    if _stop_requested:
        print(" -- Ctrl+C during the wait, pausing.")
        return True
    if ready:
        sys.stdin.readline()
        print(" -- pause requested.")
        return True
    print(" -- continuing.")
    return False


def _pause_requested() -> bool:
    if _stop_requested:
        return True
    if PAUSE_FLAG_PATH.exists():
        print(f"  {PAUSE_FLAG_PATH.name} found -- pause requested.")
        return True
    return _prompt_for_pause()


def _load_plan() -> dict | None:
    if PLAN_PATH.exists():
        return json.loads(PLAN_PATH.read_text())
    return None


def _save_plan(plan: dict) -> None:
    PLAN_PATH.write_text(json.dumps(plan, indent=2) + "\n")


def _archive_completed_plan() -> None:
    if not PLAN_PATH.exists():
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = HERE / f"batch_plan_progress_{timestamp}.json"
    shutil.move(str(PLAN_PATH), str(archived))
    print(f"  Completed plan archived to {archived.name}")


def _parse_plan_arg(arg: str) -> list[dict]:
    items = []
    for chunk in arg.split(","):
        cls, _, count = chunk.partition(":")
        cls = cls.strip()
        if cls not in FAULT_CONFIG:
            raise ValueError(f"unknown fault class {cls!r} in --plan")
        items.append({"class": cls, "target": int(count)})
    return items


def _run_baseline_check() -> bool:
    print("\nRunning check_all_baselines.py before starting/resuming...")
    result = subprocess.run(
        [sys.executable, str(BASELINE_CHECK_SCRIPT)],
        capture_output=True, text=True, start_new_session=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
        print(
            "\nBaseline drift found -- refusing to start/resume against a "
            "possibly-broken cluster state. Run:\n"
            "  python3 check_all_baselines.py --fix\n"
            "then rerun this script."
        )
        return False
    print("Baselines clean.\n")
    return True


def _wait_for_target_recency(target: str, last_fault_time: dict) -> None:
    """Per-TARGET, not a single global scalar -- a multi-class batch can
    move between genuinely unrelated targets (shipping, orders,
    catalogue-db, ...), and a global last-injection timestamp would
    incorrectly force a wait even when switching to a completely
    different service. Same real correctness pattern already proven in
    phase_d_run.py's own wait_for_target_recency, just not something
    run_episodes.py itself needed (it only ever handles one class, so one
    target, per invocation)."""
    if target not in last_fault_time:
        return
    elapsed = time.time() - last_fault_time[target]
    if elapsed < TARGET_RECENCY_WINDOW_S:
        remaining = TARGET_RECENCY_WINDOW_S - elapsed
        print(f"  waiting {remaining:.0f}s so {target}'s last fault clears the recency window")
        time.sleep(remaining)


def run_one_episode(fault_class: str, last_fault_time: dict) -> bool:
    target = FAULT_CONFIG[fault_class]["target"]

    if not wait_for_infra_ready():
        print("Infra unreachable for too long, stopping.")
        return False

    _wait_for_target_recency(target, last_fault_time)
    last_fault_time[target] = time.time()

    if not run_script("injector.py", ["--class", fault_class]):
        print("Injector failed, stopping.")
        return False

    time.sleep(SETTLE_SECONDS)

    if not run_script("scorer.py"):
        print("Scorer failed, stopping.")
        return False

    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plan", default=None,
        help="e.g. memory-leak:51,network-partition:51 -- ignored if an "
             "incomplete plan already exists (resumes that instead)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _sigint_handler)

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = OUTPUT_DIR / f"run_batch_plan_{timestamp}.log"
    log_f = open(log_path, "w", encoding="utf-8")
    real_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout, log_f)
    print(f"(full log also being written to {log_path})")

    plan_state = _load_plan()
    if plan_state is not None and plan_state.get("status") != "complete":
        print(f"Resuming existing plan from {PLAN_PATH.name} "
              f"(status={plan_state['status']!r}).")
        if args.plan:
            print("  --plan argument given but IGNORED -- an incomplete plan "
                  "already exists. Archive/delete the file manually to "
                  "start fresh instead.")
    else:
        if not args.plan:
            print("No existing plan and no --plan given. Nothing to do.")
            return
        plan_items = _parse_plan_arg(args.plan)
        plan_state = {
            "plan": plan_items,
            "completed": {item["class"]: 0 for item in plan_items},
            "status": "running",
        }
        _save_plan(plan_state)
        print(f"New plan started: {plan_items}")

    if PAUSE_FLAG_PATH.exists():
        PAUSE_FLAG_PATH.unlink()  # clear a stale flag from a prior paused run

    if not _run_baseline_check():
        plan_state["status"] = "paused"
        _save_plan(plan_state)
        return

    # Real bug fixed 2026-07-28: this used to be a fresh in-memory dict,
    # so it never survived a pause/resume -- a fast resume right after
    # pausing would silently forget when the last episode's target was
    # injected, defeating TARGET_RECENCY_WINDOW_S's whole purpose for
    # exactly that target. Persisted inside plan_state instead, so
    # _save_plan (already called after every episode) carries it forward
    # for free. setdefault so a plan saved before this fix still loads.
    plan_state.setdefault("last_fault_time", {})
    last_fault_time = plan_state["last_fault_time"]
    stopped_early = False

    for item in plan_state["plan"]:
        fault_class = item["class"]
        target_count = item["target"]
        already_done = plan_state["completed"].get(fault_class, 0)

        if already_done >= target_count:
            continue

        print(f"\n=== {fault_class}: {already_done}/{target_count} done, "
              f"continuing ===")

        for i in range(already_done + 1, target_count + 1):
            episode_start = time.monotonic()
            print(f"\n--- {fault_class} episode {i}/{target_count} ---")

            if not run_one_episode(fault_class, last_fault_time):
                plan_state["status"] = "paused"
                _save_plan(plan_state)
                stopped_early = True
                break

            episode_elapsed_s = time.monotonic() - episode_start
            _update_timings(fault_class, episode_elapsed_s)
            plan_state["completed"][fault_class] = i
            _save_plan(plan_state)
            print(f"--- episode {i} took {episode_elapsed_s:.1f}s "
                  f"({fault_class}: {i}/{target_count}) ---")

            if _pause_requested():
                plan_state["status"] = "paused"
                _save_plan(plan_state)
                if PAUSE_FLAG_PATH.exists():
                    PAUSE_FLAG_PATH.unlink()
                print("\nPaused cleanly. Rerun this script (no --plan needed) to resume.")
                stopped_early = True
                break

        if stopped_early:
            break

    if not stopped_early:
        plan_state["status"] = "complete"
        _save_plan(plan_state)
        print("\n=== Plan fully complete. ===")
        _archive_completed_plan()


if __name__ == "__main__":
    main()
