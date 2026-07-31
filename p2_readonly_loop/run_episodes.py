"""
Shared library for episode-running scripts (injector -> wait -> scorer),
NOT a standalone runner -- real trim, 2026-07-29. This used to also have
its own single-class CLI loop (`main()`, `python3 run_episodes.py --class
crash-loop 20`), but that became fully redundant once run_batch_plan.py
existed: it does the exact same real sequence (wait-for-infra ->
wait-for-recency -> run injector -> settle -> run scorer -> update
timings), using these SAME helpers, plus strictly more (resumable/
pausable, runs check_all_baselines.py first, verifies a real episode
actually got recorded rather than trusting subprocess exit code, and has
a richer end-of-run summary). Any single-class use case is just
`python3 run_batch_plan.py --plan crash-loop:20` now. Removed alongside
`main()`: `wait_for_target_recency()` (only ever called by `main()` --
run_batch_plan.py has its own `_wait_for_target_recency`, shaped for
multiple concurrent targets, which `main()`'s single-target version
never needed to be).

What actually still lives here, and is the real reason this file isn't
deleted outright: SETTLE_SECONDS, TARGET_RECENCY_WINDOW_S/
DEFAULT_TARGET_RECENCY_WINDOW_S (the real per-class recency margins),
_Tee, _update_timings, run(), wait_for_infra_ready()/_infra_ready() --
all imported directly by run_batch_plan.py, the actual single source of
truth for these values/helpers.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import requests

SETTLE_SECONDS = 35  # kube-state-metrics scrapes every 30s; wait a full cycle + margin
                      # so the last fault action in the injection window is reflected

TIMINGS_PATH = Path(__file__).parent / "episode_timings.json"


class _Tee:
    """Same convention already used by phase_e_audit.py and other pipeline
    modules -- stdout was previously terminal-only here, a real gap for a
    multi-hour unattended run: anything printed (retry reasons, infra
    waits, per-episode verdicts) would be permanently lost once the
    terminal's scrollback is exceeded or the window closes. Timestamped
    filename (not a fixed name) so re-running the same class later
    doesn't silently overwrite a prior run's real log."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()

# Guardrails ported from phase_d_run.py (2026-07-28), for unsupervised
# overnight batches -- this script previously had neither, which was
# fine for short manual runs but a real gap for a multi-hour unattended
# batch of report-only classes.
PROMETHEUS_HEALTH_URL = "http://localhost:9090/-/healthy"
# P2's agent (unlike p3_agent) has no dedicated lightweight health
# endpoint -- /diagnose does real work, so hitting it as a health check
# would be wasteful and could itself pollute the DB. /openapi.json is
# FastAPI's own auto-generated schema endpoint: confirms the uvicorn
# process is alive and responsive with zero real logic touched,
# confirmed working (200) against the running P2 agent.
AGENT_HEALTH_URL = "http://localhost:8000/openapi.json"
INFRA_WAIT_MAX_S = 1200  # 20 min -- absorbs a real outage (port-forward
                          # reconnect, WSL2 stall) without waiting forever
INFRA_WAIT_POLL_S = 20

# Same reasoning as phase_d_run.py's wait_for_target_recency: this
# script re-injects the SAME class (and therefore the same real target)
# repeatedly, back-to-back, for potentially dozens of episodes in one
# run -- without a minimum gap, a still-settling prior fault could
# contaminate the next episode's baseline/injection, the same
# contamination shape behind several of this project's already-found
# bugs. 300s matches the value already proven correct elsewhere.
# Real per-class recency margin (recalibrated 2026-07-29, replacing the
# old flat 300s guess). Sized from each class's own REAL diagnosis
# lookback window (agent.py's actual PromQL queries, confirmed directly
# -- not assumed) plus a 30s buffer, matching the precedent already set
# by run_systematic_validation.py's own "215 = 3min lookback + 30s
# eviction/scrape-jitter buffer" derivation. The old flat 300s was
# correctly derived for the AUTO-FIX classes (a real margin over their
# ~280s fix+durability cycle) but was never actually valid for the
# report-only classes (no fix/durability cycle at all -- their real
# risk is a stale PRIOR fault still sitting inside the NEXT episode's
# own diagnosis lookback window) -- this is what stalled the original
# overnight thin-class batch (see wardence_buildlog.md, 2026-07-28
# session): session-cart-failure/init-failure's real per-episode cost
# was ~5x every other class specifically because of this over-wait.
TARGET_RECENCY_WINDOW_S = {
    # Report-only classes: real diagnosis lookback window + 30s buffer.
    "network-latency": 150,             # agent.py p95_latency query: max_over_time(...[2m])
    "network-partition": 150,           # agent.py combined_throughput_bps: [2m] subquery
    "init-failure": 150,                # agent.py payment_stuck_not_ready: max_over_time(...[2m])
    "session-cart-failure": 150,        # agent.py session_db_replicas_hit_zero: min_over_time(...[2m])
    "memory-leak": 210,                 # agent.py peak_memory_mib: max_over_time(...[3m])
    "connection-pool-exhaustion": 210,  # agent.py peak_threads_connected: max_over_time(...[3m])
    # Auto-fix classes: real fix+durability cycle -- unchanged from the
    # original flat 300s where it was already correctly derived.
    "crash-loop": 300,
    "oom": 300,
    "disk-full": 300,
    "bad-rollout": 300,
    # under-provisioned-replicas' own real observed worst-case
    # (durability_elapsed_s=278s, per wardence_buildlog.md) left only
    # ~22s real margin against the old flat 300s -- widened for genuine
    # safety, not because 300 was ever shown to actually fail.
    "under-provisioned-replicas": 350,
    # REAL CORRECTION, found during this recalibration: cpu-throttling's
    # own diagnosis query uses a [6m]=360s lookback (agent.py, widened
    # from [2m] on 2026-07-26 to fix a real reset-rollout timing bug) --
    # genuinely LONGER than the old flat 300s guard, meaning repeated
    # cpu-throttling runs were never actually safe against this exact
    # contamination risk. Fixed to 360 + 30s buffer = 390.
    "cpu-throttling": 390,
}
DEFAULT_TARGET_RECENCY_WINDOW_S = 300  # any class not yet in the dict above


def _infra_ready() -> tuple[bool, str]:
    """Checks BOTH Prometheus directly (injector.py's own effect-
    verification queries hit it directly) and the P2 agent process
    (which scorer.py depends on). Either being down is a reason to
    pause, not push through and log a false failure."""
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
    """Called before every episode. If Prometheus/the agent is briefly
    down (port-forward reconnecting, a real outage), pauses and polls
    instead of immediately treating the episode as a real failure --
    which would otherwise pollute the results with a false failure that
    has nothing to do with the actual class being tested. Gives up
    after INFRA_WAIT_MAX_S."""
    waited = 0
    while True:
        ok, detail = _infra_ready()
        if ok:
            if waited > 0:
                print(f"  infra back up after {waited}s wait -- proceeding.")
            return True
        if waited >= INFRA_WAIT_MAX_S:
            print(f"  infra still not ready after {INFRA_WAIT_MAX_S}s ({detail}) -- giving up.")
            return False
        print(f"  infra not ready ({detail}) -- pausing {INFRA_WAIT_POLL_S}s before rechecking "
              f"(waited {waited}s/{INFRA_WAIT_MAX_S}s so far)")
        time.sleep(INFRA_WAIT_POLL_S)
        waited += INFRA_WAIT_POLL_S


def run(script: str, extra_args: list[str] | None = None):
    # Real bug found 2026-07-31: `script` used to be passed bare
    # ("injector.py"), which subprocess.run resolves relative to the
    # PARENT process's cwd, not this file's own directory -- worked fine
    # whenever run_batch_plan.py happened to be invoked from inside
    # p2_readonly_loop/ (the project's habitual pattern), but broke with
    # a real "can't open file" the moment someone ran it from the repo
    # root instead (e.g. `python3 p2_readonly_loop/run_batch_plan.py`).
    # Resolved to an absolute path off THIS file's own location, and
    # cwd explicitly pinned to the same directory -- covers both the
    # script-not-found case and any future relative-path assumption
    # inside injector.py/scorer.py itself, regardless of where the
    # caller's own shell happens to be sitting.
    script_dir = Path(__file__).parent
    cmd = [sys.executable, str(script_dir / script)] + (extra_args or [])
    # start_new_session=True (2026-07-28): launches the child in its OWN
    # process group, not the terminal's foreground one -- without this, a
    # Ctrl+C at the terminal delivers SIGINT to injector.py/scorer.py
    # directly and simultaneously with this parent process, regardless of
    # any signal handling done here. That risks killing a real, live
    # cluster mutation (a kubectl scale/patch mid-flight) at a genuinely
    # unsafe moment. With this, only the parent receives Ctrl+C, and the
    # child always runs to its own natural completion undisturbed --
    # required for run_batch_plan.py's "Ctrl+C is safe, stops at the next
    # safe point" guarantee to actually be true, not just aspirational.
    result = subprocess.run(
        cmd, capture_output=True, text=True, start_new_session=True, cwd=script_dir
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
    return result.returncode == 0


def _update_timings(fault_class: str, elapsed_s: float) -> None:
    """Updates episode_timings.json's running count/avg/min/max/total for
    this class. Read-modify-write on every completed episode (not batched
    at the end) so a long overnight run's stats are visible mid-run, not
    only after it finishes or if it's interrupted."""
    timings: dict[str, dict[str, float]] = {}
    if TIMINGS_PATH.exists():
        timings = json.loads(TIMINGS_PATH.read_text())

    stats = timings.get(fault_class, {"count": 0, "total_s": 0.0, "min_s": elapsed_s, "max_s": elapsed_s})
    stats["count"] += 1
    stats["total_s"] += elapsed_s
    stats["avg_s"] = stats["total_s"] / stats["count"]
    stats["min_s"] = min(stats["min_s"], elapsed_s)
    stats["max_s"] = max(stats["max_s"], elapsed_s)
    timings[fault_class] = stats

    TIMINGS_PATH.write_text(json.dumps(timings, indent=2) + "\n")
