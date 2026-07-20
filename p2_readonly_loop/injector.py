"""
P2 injector: triggers a REAL crash-loop against Sock Shop via Chaos Mesh
(recurring container-kill on the same pod, so restarts genuinely
accumulate and kubelet can back off into CrashLoopBackOff) and records
the ground-truth label (fault class, target, t0) into SQLite.

Single pod-kill was tried first and rejected: it deletes the whole Pod
object, so Kubernetes' own controller silently recreates a fresh pod
(0 restarts) before the agent ever sees anything to diagnose or fix.
container-kill kills the container process IN PLACE, so the same pod
takes real, accumulating restarts -- an actual crash-loop, not a blip
Kubernetes fixes for free.

This is the ONLY place the true fault label is written. The agent must
never read this DB or see chaos-mesh.org resources (see blinding test,
P1 -- rerun the same style of check against container-kill before
trusting it for real episodes).

Usage:
    python injector.py
"""

import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

TARGET_NAMESPACE = "sock-shop"
TARGET_LABEL_APP = "carts"
TARGET_CONTAINER = "carts"
FAULT_CLASS = "crash-loop"

KILL_INTERVAL_SECONDS = "*/10 * * * * *"  # container-kill every 10s
EPISODE_DURATION_SECONDS = 40  # let it fire ~4 times before stopping

# DB lives on WSL2's native filesystem, not the Windows-mounted C:/ path --
# DrvFs (WSL2's NTFS translation layer) has known SQLite file-locking bugs
# that caused hangs/"unable to open database file" when scripts hammered
# a DB on /mnt/c.
OUTPUT_DIR = Path.home() / "wardence_p2_data"
DB_PATH = OUTPUT_DIR / "wardence.db"

CHAOS_NAME_PREFIX = "wardence-crashloop"


def ensure_db():
    OUTPUT_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            episode_id TEXT PRIMARY KEY,
            fault_class TEXT NOT NULL,
            target TEXT NOT NULL,
            namespace TEXT NOT NULL,
            t0 TEXT NOT NULL,
            chaos_resource_name TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def build_schedule_manifest(schedule_name: str) -> str:
    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: Schedule
metadata:
  name: {schedule_name}
  namespace: chaos-mesh
spec:
  schedule: "{KILL_INTERVAL_SECONDS}"
  type: PodChaos
  historyLimit: 5
  concurrencyPolicy: Forbid
  podChaos:
    action: container-kill
    mode: one
    containerNames:
      - {TARGET_CONTAINER}
    selector:
      namespaces:
        - {TARGET_NAMESPACE}
      labelSelectors:
        name: {TARGET_LABEL_APP}
"""


def apply_schedule(schedule_name: str):
    manifest = build_schedule_manifest(schedule_name)
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl apply failed:\n{result.stderr}")
    print(result.stdout.strip())


def delete_schedule(schedule_name: str):
    result = subprocess.run(
        ["kubectl", "delete", "schedule", schedule_name, "-n", "chaos-mesh"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: failed to delete schedule {schedule_name}:\n{result.stderr}")
    else:
        print(result.stdout.strip())


def record_episode(conn: sqlite3.Connection, episode_id: str, chaos_name: str, t0: str):
    conn.execute(
        "INSERT INTO episodes (episode_id, fault_class, target, namespace, t0, chaos_resource_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (episode_id, FAULT_CLASS, TARGET_LABEL_APP, TARGET_NAMESPACE, t0, chaos_name),
    )
    conn.commit()


def main():
    episode_id = str(uuid.uuid4())
    schedule_name = f"{CHAOS_NAME_PREFIX}-{episode_id[:8]}"
    t0 = datetime.now(timezone.utc).isoformat()

    conn = ensure_db()
    apply_schedule(schedule_name)
    record_episode(conn, episode_id, schedule_name, t0)
    conn.close()

    print(f"Episode {episode_id} started: {FAULT_CLASS} on {TARGET_LABEL_APP} ({TARGET_NAMESPACE}) at {t0}")
    print(f"Letting it run for {EPISODE_DURATION_SECONDS}s (recurring container-kill)...")
    time.sleep(EPISODE_DURATION_SECONDS)

    delete_schedule(schedule_name)
    print(f"Episode {episode_id} injection window closed. Schedule {schedule_name} removed.")


if __name__ == "__main__":
    main()
