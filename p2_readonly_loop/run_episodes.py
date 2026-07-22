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
import subprocess
import sys
import time

SETTLE_SECONDS = 35  # kube-state-metrics scrapes every 30s; wait a full cycle + margin
                      # so the last fault action in the injection window is reflected


def run(script: str, extra_args: list[str] | None = None):
    cmd = [sys.executable, script] + (extra_args or [])
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--class", dest="fault_class", required=True,
        choices=["crash-loop", "oom", "disk-full", "network-latency", "memory-leak", "connection-pool-exhaustion"]
    )
    parser.add_argument("num_episodes", nargs="?", type=int, default=20)
    args = parser.parse_args()

    for i in range(1, args.num_episodes + 1):
        print(f"\n--- Episode {i}/{args.num_episodes} ({args.fault_class}) ---")
        if not run("injector.py", ["--class", args.fault_class]):
            print("Injector failed, stopping.")
            break

        time.sleep(SETTLE_SECONDS)

        if not run("scorer.py"):
            print("Scorer failed, stopping.")
            break


if __name__ == "__main__":
    main()
