"""
P2 episode runner: repeats injector -> wait -> scorer N times.

Prometheus port-forward and the agent (uvicorn agent:app) must already
be running in separate terminals before starting this.

Usage:
    python3 run_episodes.py [num_episodes]   # default 20
"""

import subprocess
import sys
import time

SETTLE_SECONDS = 35  # kube-state-metrics scrapes every 30s; wait a full cycle + margin
                      # so the last container-kill in the injection window is reflected


def run(script: str):
    result = subprocess.run([sys.executable, script], capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
    return result.returncode == 0


def main():
    num_episodes = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    for i in range(1, num_episodes + 1):
        print(f"\n--- Episode {i}/{num_episodes} ---")
        if not run("injector.py"):
            print("Injector failed, stopping.")
            break

        time.sleep(SETTLE_SECONDS)

        if not run("scorer.py"):
            print("Scorer failed, stopping.")
            break


if __name__ == "__main__":
    main()
