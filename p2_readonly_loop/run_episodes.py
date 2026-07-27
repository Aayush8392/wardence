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
from pathlib import Path

SETTLE_SECONDS = 35  # kube-state-metrics scrapes every 30s; wait a full cycle + margin
                      # so the last fault action in the injection window is reflected

TIMINGS_PATH = Path(__file__).parent / "episode_timings.json"


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

    episode_durations_s: list[float] = []

    for i in range(1, args.num_episodes + 1):
        episode_start = time.monotonic()
        print(f"\n--- Episode {i}/{args.num_episodes} ({args.fault_class}) ---")
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
