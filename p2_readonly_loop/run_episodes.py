"""
P2 episode runner: repeats injector -> wait -> scorer N times.

Prometheus port-forward and the agent (uvicorn agent:app) must already
be running in separate terminals before starting this.

Usage:
    python3 run_episodes.py --class crash-loop [num_episodes]   # default 20
    python3 run_episodes.py --class oom [num_episodes]
    python3 run_episodes.py --class network-latency [num_episodes]
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SETTLE_SECONDS = 35  # kube-state-metrics scrapes every 30s; wait a full cycle + margin
                      # so the last fault action in the injection window is reflected

TIMINGS_PATH = Path(__file__).parent / "episode_timings.json"
OUTPUT_DIR = Path(__file__).parent / "output"


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
TARGET_RECENCY_WINDOW_S = 300


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


def wait_for_target_recency(last_injection_time: float | None) -> None:
    if last_injection_time is None:
        return
    elapsed = time.time() - last_injection_time
    if elapsed < TARGET_RECENCY_WINDOW_S:
        remaining = TARGET_RECENCY_WINDOW_S - elapsed
        print(f"  waiting {remaining:.0f}s so the target's last fault clears the recency window")
        time.sleep(remaining)


def run(script: str, extra_args: list[str] | None = None):
    cmd = [sys.executable, script] + (extra_args or [])
    result = subprocess.run(cmd, capture_output=True, text=True)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--class", dest="fault_class", required=True,
        choices=[
            "crash-loop", "oom", "disk-full", "network-latency", "memory-leak",
            "connection-pool-exhaustion", "network-partition", "init-failure",
            "session-cart-failure", "cpu-throttling", "under-provisioned-replicas",
            "bad-rollout",
        ]
    )
    parser.add_argument("num_episodes", nargs="?", type=int, default=20)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = OUTPUT_DIR / f"run_episodes_{args.fault_class}_{timestamp}.log"
    log_f = open(log_path, "w", encoding="utf-8")
    real_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout, log_f)
    print(f"(full log also being written to {log_path})")

    episode_durations_s: list[float] = []
    last_injection_time: float | None = None

    for i in range(1, args.num_episodes + 1):
        episode_start = time.monotonic()
        print(f"\n--- Episode {i}/{args.num_episodes} ({args.fault_class}) ---")

        if not wait_for_infra_ready():
            print("Infra unreachable for too long, stopping.")
            break

        wait_for_target_recency(last_injection_time)
        last_injection_time = time.time()

        if not run("injector.py", ["--class", args.fault_class]):
            print("Injector failed, stopping.")
            break

        time.sleep(SETTLE_SECONDS)

        if not run("scorer.py"):
            print("Scorer failed, stopping.")
            break

        episode_elapsed_s = time.monotonic() - episode_start
        episode_durations_s.append(episode_elapsed_s)
        _update_timings(args.fault_class, episode_elapsed_s)
        print(f"--- Episode {i} took {episode_elapsed_s:.1f}s ---")

    if episode_durations_s:
        avg_s = sum(episode_durations_s) / len(episode_durations_s)
        print(
            f"\n=== {len(episode_durations_s)} episode(s) of '{args.fault_class}' "
            f"completed. avg={avg_s:.1f}s min={min(episode_durations_s):.1f}s "
            f"max={max(episode_durations_s):.1f}s total={sum(episode_durations_s):.1f}s ==="
        )


if __name__ == "__main__":
    main()
