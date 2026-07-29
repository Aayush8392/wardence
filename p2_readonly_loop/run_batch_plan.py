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

This is the ONLY script that ever runs episodes against the real cluster
now (2026-07-29) -- phase_d_run.py, which used to be a SEPARATE executor
for cross-class pair validation, is retired. It duplicated every real
guardrail here (its own wait_for_target_recency, its own stale flat
TARGET_RECENCY_WINDOW_S=300 that never got the per-class recalibration,
no real-episode verification, no per-class give-up-and-continue) --
closed for good by making this the single executor for every mode:
plain per-class runs, shuffled cross-class pairs, and Phase D's own
pair-coverage files. See --shuffle/--pairs-file below.

Usage:
    # Start a fresh plan -- one continuous block per class, in order.
    # IGNORED if an incomplete plan already exists in PLAN_PATH -- that
    # resumes instead. Archive/delete the file manually to force a fresh
    # start over an incomplete one.
    python3 run_batch_plan.py --plan memory-leak:51,network-partition:51,connection-pool-exhaustion:51

    # Shuffled cross-class pairs -- real counts don't need to match.
    # Builds as many random (class_a, class_b) pairs as possible, then
    # drains whichever class has leftover budget as one continuous block
    # at the end (_build_shuffled_plan). Good for topping up unequal
    # per-class counts while also getting cross-class-adjacency exposure,
    # instead of one big block per class run back to back.
    python3 run_batch_plan.py --shuffle oom:5,network-partition:12,memory-leak:8

    # Consume an existing Phase-D-style pairs file (a JSON list of
    # [class_a, class_b] pairs -- phase_d_generate_batches.py's/
    # phase_d_generate_sample_rounds.py's own real output format,
    # unchanged) for real, systematic pair-coverage validation, executed
    # through this file's own guardrails instead of the retired
    # phase_d_run.py.
    python3 run_batch_plan.py --pairs-file phase_d_round1.json

    # Resume whatever's already in progress, no new plan needed.
    python3 run_batch_plan.py
"""

import argparse
import json
import random
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_episodes import (  # noqa: E402
    DEFAULT_TARGET_RECENCY_WINDOW_S,
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

# Same DB path convention as trust_engine.py/injector.py.
DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"
# Real safety cap, same spirit as phase_d_run.py's own circuit-breaker-
# style caps -- a persistent real problem should stop the batch, not
# retry the same slot forever.
MAX_CONSECUTIVE_REAL_FAILURES = 3


def _real_episode_count(fault_class: str) -> int:
    """Real DB count, not the plan's own loop-iteration counter --
    distinguishes a genuinely recorded episode from a loop iteration
    that ran injector.py/scorer.py but produced nothing. Real gap found
    2026-07-29: injector.py can correctly refuse to fabricate an episode
    after real retries all fail ("INJECTION FAILED... NO episode
    recorded", its own intended "refuse rather than corrupt" behavior)
    and still exit 0 -- run_one_episode's success/failure return only
    reflects subprocess exit code, so the OLD fixed for-loop silently
    counted that slot as done anyway. Confirmed happening for real once
    in the first full overnight run of the recalibrated batch
    (connection-pool-exhaustion episode 1/29), undercounting the real
    result by one episode."""
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE fault_class = ?", (fault_class,)
    ).fetchone()[0]
    conn.close()
    return n

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


def _build_shuffled_plan(class_counts: dict) -> list[dict]:
    """Real algorithm, 2026-07-29 (replaces phase_d_run.py as a separate
    executor -- see wardence_buildlog.md for the full reasoning): classes
    rarely have equal remaining counts, so a naive shuffle-then-block
    split doesn't cleanly express "run as many random cross-class pairs
    as possible, then drain whichever class has leftover budget as one
    continuous block at the end." This does that directly:

    While >=2 classes still have budget: pick 2 DISTINCT classes at
    random from those with remaining budget, append one single-episode
    chunk for EACH (in random order between the two -- a genuine random
    pair, not a fixed A-then-B), decrement both. Once only one class has
    budget left, append it as ONE block (target=remaining), not
    fragmented single-episode chunks -- a guaranteed, clean "runs
    continuously at the end," not just statistically likely.

    Real, deliberately DIFFERENT algorithm from phase_d_generate_
    sample_rounds.py's rolling-overlap coverage sampler -- that one
    solves a different problem (guaranteed full N-by-N pair coverage,
    assuming roughly equal budgets across rounds) that doesn't fit an
    unequal, one-off top-up run like this. See _pairs_file_to_plan
    below for consuming that generator's own output when full coverage
    is actually what's needed instead."""
    remaining = {cls: n for cls, n in class_counts.items() if n > 0}
    plan_items: list[dict] = []

    while len(remaining) >= 2:
        pair = random.sample(list(remaining.keys()), 2)
        random.shuffle(pair)
        # Real bug caught before this ever ran: an earlier version had a
        # `break` here that skipped the SECOND class in the pair whenever
        # the FIRST one happened to hit 0 remaining after its own
        # decrement (common -- any class with exactly 1 left). Both
        # classes in a pair must always get their one episode appended;
        # removal from `remaining` only happens AFTER both are processed.
        for cls in pair:
            plan_items.append({"class": cls, "target": 1})
            remaining[cls] -= 1
        for cls in pair:
            if remaining.get(cls, 0) <= 0:
                remaining.pop(cls, None)

    for cls, n in remaining.items():
        plan_items.append({"class": cls, "target": n})

    return plan_items


def _parse_shuffle_arg(arg: str) -> dict:
    counts = {}
    for chunk in arg.split(","):
        cls, _, count = chunk.partition(":")
        cls = cls.strip()
        if cls not in FAULT_CONFIG:
            raise ValueError(f"unknown fault class {cls!r} in --shuffle")
        counts[cls] = int(count)
    return counts


def _pairs_file_to_plan(path: str) -> list[dict]:
    """Consumes an existing Phase-D-style pairs file (a JSON list of
    [class_a, class_b] pairs, e.g. phase_d_round1.json/phase_d_batch1.json
    -- phase_d_generate_batches.py's/phase_d_generate_sample_rounds.py's
    own real output format, unchanged) and expands each pair into two
    single-episode plan chunks in sequence. This is the ONLY change
    needed to consume Phase D's existing, real, already-tested pair-
    coverage generators -- their own generation logic doesn't need to
    change at all, only how the result gets executed (through this
    file's own guardrail-owning loop now, not the retired
    phase_d_run.py)."""
    pairs = json.loads(Path(path).read_text())
    plan_items: list[dict] = []
    for a, b in pairs:
        plan_items.append({"class": a, "target": 1})
        plan_items.append({"class": b, "target": 1})
    return plan_items


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


def _wait_for_target_recency(target: str, fault_class: str, last_fault_time: dict) -> None:
    """Per-TARGET, not a single global scalar -- a multi-class batch can
    move between genuinely unrelated targets (shipping, orders,
    catalogue-db, ...), and a global last-injection timestamp would
    incorrectly force a wait even when switching to a completely
    different service. Same real correctness pattern already proven in
    phase_d_run.py's own wait_for_target_recency, just not something
    run_episodes.py itself needed (it only ever handles one class, so one
    target, per invocation).

    Window looked up by the CURRENT (about-to-inject) fault_class, not
    whichever class last touched this target -- recalibrated 2026-07-29:
    the real risk being guarded against is a stale prior signal still
    sitting inside THIS episode's own diagnosis lookback query, so the
    margin must be sized against the window the upcoming diagnosis will
    actually use. In today's roster every class targets a distinct
    service, so this is equivalent to keying by target either way -- but
    keying by fault_class is the technically correct choice if a future
    batch ever mixes two classes sharing a target (e.g. network-latency/
    network-partition, both on orders)."""
    if target not in last_fault_time:
        return
    window = TARGET_RECENCY_WINDOW_S.get(fault_class, DEFAULT_TARGET_RECENCY_WINDOW_S)
    elapsed = time.time() - last_fault_time[target]
    if elapsed < window:
        remaining = window - elapsed
        print(f"  waiting {remaining:.0f}s so {target}'s last fault clears the recency window")
        time.sleep(remaining)


def run_one_episode(fault_class: str, last_fault_time: dict) -> bool:
    target = FAULT_CONFIG[fault_class]["target"]

    if not wait_for_infra_ready():
        print("Infra unreachable for too long, stopping.")
        return False

    _wait_for_target_recency(target, fault_class, last_fault_time)
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
        help="e.g. memory-leak:51,network-partition:51 -- one continuous "
             "block per class, in the order given. Ignored if an "
             "incomplete plan already exists (resumes that instead)",
    )
    parser.add_argument(
        "--shuffle", default=None,
        help="e.g. oom:5,network-partition:12 -- real counts don't need to "
             "match. Builds as many random cross-class pairs as possible, "
             "then drains whichever class has leftover budget as one "
             "continuous block at the end. Mutually exclusive with --plan.",
    )
    parser.add_argument(
        "--pairs-file", default=None,
        help="Path to an existing Phase-D-style pairs file (a JSON list of "
             "[class_a, class_b] pairs, e.g. phase_d_round1.json -- see "
             "phase_d_generate_sample_rounds.py). Mutually exclusive with "
             "--plan/--shuffle.",
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

    new_plan_arg_given = args.plan or args.shuffle or args.pairs_file
    plan_state = _load_plan()
    if plan_state is not None and plan_state.get("status") != "complete":
        print(f"Resuming existing plan from {PLAN_PATH.name} "
              f"(status={plan_state['status']!r}).")
        if new_plan_arg_given:
            print("  --plan/--shuffle/--pairs-file argument given but IGNORED -- "
                  "an incomplete plan already exists. Archive/delete the file "
                  "manually to start fresh instead.")
    else:
        if not new_plan_arg_given:
            print("No existing plan and no --plan/--shuffle/--pairs-file given. Nothing to do.")
            return
        if sum(bool(x) for x in (args.plan, args.shuffle, args.pairs_file)) > 1:
            print("Only one of --plan/--shuffle/--pairs-file may be given at once.")
            return
        if args.plan:
            plan_items = _parse_plan_arg(args.plan)
        elif args.shuffle:
            plan_items = _build_shuffled_plan(_parse_shuffle_arg(args.shuffle))
        else:
            plan_items = _pairs_file_to_plan(args.pairs_file)
        plan_state = {
            "plan": plan_items,
            "completed": [0] * len(plan_items),
            "status": "running",
        }
        _save_plan(plan_state)
        print(f"New plan started: {len(plan_items)} chunk(s).")

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

    # Real end-of-run summary tracking, added 2026-07-29 -- run_episodes.py
    # always printed one for its own single-class runs; run_batch_plan.py
    # never got the equivalent for the whole multi-class plan (found after
    # a real ~7h overnight run finished with no way to see its own total
    # time without hand-computing it from log timestamps).
    run_start = time.monotonic()
    per_class_durations_s: dict[str, list[float]] = {}
    real_failures_total = 0

    # Real fix, 2026-07-29: `completed` used to be keyed purely by
    # fault_class, assuming each class appears in at most ONE plan chunk
    # -- true for a plain `--plan class:N` run, but broken for a
    # shuffled plan (_build_shuffled_plan/_pairs_file_to_plan), which
    # deliberately scatters the SAME class across many small, non-
    # consecutive single-episode chunks. Under the old per-class
    # tracking, the second chunk of a repeated class would look
    # "already done" the moment the FIRST chunk completed its own
    # target, silently skipping every later occurrence. Fixed by
    # tracking progress PER CHUNK (by index in plan_state["plan"]),
    # never per-class. `gave_up` stays per-class (a real mechanism
    # problem found in one chunk is likely to recur in every later
    # chunk of the same class, so skip all of them, not just the one).
    if not isinstance(plan_state.get("completed"), list):
        plan_state["completed"] = [0] * len(plan_state["plan"])

    for idx, item in enumerate(plan_state["plan"]):
        fault_class = item["class"]
        target_count = item["target"]
        already_done = plan_state["completed"][idx]

        if already_done >= target_count:
            continue

        if fault_class in plan_state.get("gave_up", {}):
            print(f"\n=== {fault_class} chunk {idx} SKIPPED -- already gave up on "
                  f"this class earlier in this run ===")
            plan_state["completed"][idx] = target_count
            _save_plan(plan_state)
            continue

        print(f"\n=== {fault_class} chunk {idx}: {already_done}/{target_count} done, "
              f"continuing ===")

        consecutive_real_failures = 0
        attempt = already_done

        while plan_state["completed"][idx] < target_count:
            attempt += 1
            done_so_far = plan_state["completed"][idx]
            episode_start = time.monotonic()
            print(f"\n--- {fault_class} attempt {attempt} "
                  f"({done_so_far}/{target_count} real episodes so far, this chunk) ---")

            before_count = _real_episode_count(fault_class)

            if not run_one_episode(fault_class, last_fault_time):
                plan_state["status"] = "paused"
                _save_plan(plan_state)
                stopped_early = True
                break

            episode_elapsed_s = time.monotonic() - episode_start
            after_count = _real_episode_count(fault_class)

            if after_count <= before_count:
                # Real injection failure -- injector.py already refused to
                # fabricate an episode. Don't advance progress; retry the
                # same slot, capped so a persistent real problem doesn't
                # loop forever. Real fix, 2026-07-29: hitting the cap gives
                # up on THIS CLASS only (all its remaining chunks, not just
                # this one -- see the `gave_up` skip-check above) and moves
                # to the next chunk -- a per-class mechanism problem (e.g.
                # connection-pool-exhaustion's flood not reaching real
                # capacity) has nothing to do with whether memory-leak/
                # network-partition can still run fine unattended, so it
                # must not pause the WHOLE overnight batch. Real infra
                # failures (below, "not run_one_episode(...)") still pause
                # everything, since those genuinely block every class, not
                # just one.
                consecutive_real_failures += 1
                real_failures_total += 1
                print(f"  WARNING: no real episode was recorded this attempt "
                      f"({before_count} -> {after_count} in the DB) -- retrying "
                      f"({consecutive_real_failures}/{MAX_CONSECUTIVE_REAL_FAILURES} "
                      f"consecutive real failures for {fault_class}).")
                if consecutive_real_failures >= MAX_CONSECUTIVE_REAL_FAILURES:
                    print(f"  {MAX_CONSECUTIVE_REAL_FAILURES} consecutive real failures for "
                          f"{fault_class} -- giving up on this class for the REST of this run "
                          f"(real {done_so_far}/{target_count} episodes recorded in this chunk), "
                          f"moving on rather than pausing the whole batch.")
                    plan_state.setdefault("gave_up", {})[fault_class] = {
                        "real_episodes_this_chunk": done_so_far,
                        "target_this_chunk": target_count,
                        "consecutive_real_failures": consecutive_real_failures,
                    }
                    _save_plan(plan_state)
                    break  # out of this class's while loop only -- stopped_early stays False
                continue

            consecutive_real_failures = 0
            _update_timings(fault_class, episode_elapsed_s)
            per_class_durations_s.setdefault(fault_class, []).append(episode_elapsed_s)
            plan_state["completed"][idx] = done_so_far + 1
            _save_plan(plan_state)
            print(f"--- real episode recorded, took {episode_elapsed_s:.1f}s "
                  f"({fault_class}: {plan_state['completed'][idx]}/{target_count}, this chunk) ---")

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

    run_elapsed_s = time.monotonic() - run_start
    total_real_episodes = sum(len(v) for v in per_class_durations_s.values())
    print(f"\n=== Batch summary: real wall-clock time "
          f"{run_elapsed_s / 3600:.2f}h ({run_elapsed_s:.0f}s), "
          f"{total_real_episodes} real episode(s) recorded, "
          f"{real_failures_total} real injection failure(s) "
          f"(no episode, correctly not recorded) ===")
    for fault_class, durations in per_class_durations_s.items():
        avg_s = sum(durations) / len(durations)
        print(f"  {fault_class:<28} {len(durations)} episodes, "
              f"avg={avg_s:.1f}s min={min(durations):.1f}s max={max(durations):.1f}s")
    gave_up = plan_state.get("gave_up", {})
    if gave_up:
        print(f"\n  {len(gave_up)} class(es) gave up short of target after "
              f"{MAX_CONSECUTIVE_REAL_FAILURES} consecutive real injection failures "
              f"(real mechanism problem, not an environment/infra issue):")
        for fault_class, info in gave_up.items():
            print(f"    {fault_class}: {info['real_episodes_this_chunk']}/{info['target_this_chunk']} "
                  f"real episodes in the chunk that triggered the give-up "
                  f"(any later chunks of this class in the plan were skipped entirely)")


if __name__ == "__main__":
    main()
