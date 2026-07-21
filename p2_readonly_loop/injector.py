"""
P2 injector: triggers a REAL fault against Sock Shop via Chaos Mesh and
records the ground-truth label (fault class, target, t0) into SQLite.

crash-loop: NOT Chaos Mesh either (as of this rewrite) -- originally
used a recurring Chaos Mesh Schedule running PodChaos/container-kill,
but chaos-daemon has a reproducible bug: it resolves and caches a
container's containerd task ID once, then never invalidates that cache
when the container's task actually changes (from a restart, whether
caused by chaos-daemon's own kill or anything else). The first kill
against a freshly-daemon-restarted target tends to succeed; as the
target's restart count accumulates since chaos-daemon was last
restarted, the odds of hitting the stale cache increase, producing
"no running task found: task ... not found" errors that get worse the
more the SAME target is used -- exactly what crash-loop's target
(carts) is, constantly, across a validation run. Restarting chaos-daemon
"fixes" it only temporarily. Confirmed with Kimi (reviews/03) that this
is a real architectural mismatch (Chaos Mesh treats container identity
as static for an experiment's lifetime; Kubernetes doesn't), not a
transient flake -- so this is driven directly instead: `kubectl exec`
SIGKILL (pkill -9 -f, NOT `kill -9 1` -- see _kill_main_process for why
targeting PID 1 empirically did NOT work here: carts' PID 1 is a shell
wrapper script that launches the real JVM as a child process, not via
`exec`, and killing PID 1 didn't tear the container down the way the
kernel's PID-namespace semantics would suggest it should; killing the
actual JVM process by name pattern is what was confirmed, manually, to
actually trigger a restart) the container's real application process,
repeated on an interval, letting kubelet restart it in place -- real,
accumulating restarts, genuine CrashLoopBackOff, same end effect as
container-kill, without chaos-daemon's broken cache in the loop at all.
Single pod-kill was tried first and rejected far
earlier in this project: it deletes the whole Pod object, so
Kubernetes' own controller silently recreates a fresh pod (0 restarts)
before the agent ever sees anything to diagnose or fix -- exec-kill
avoids that the same way container-kill did, by killing the process
IN PLACE rather than the pod.

oom: StressChaos memory stressor against catalogue (200Mi limit,
stressor requests 250Mi so the kubelet OOM-killer fires for real --
same failure signature as a genuine production OOM, not simulated).

The stressor sets oomScoreAdj: -1000. Without it, this was a coin-flip
that LOOKED reliable for a while: the kernel OOM-killer picks a victim
by oom_score within the cgroup, not "whoever caused the breach" -- the
stress-ng process itself (allocating 250M) usually has a HIGHER score
than catalogue's actual app, so the kernel was mostly killing the
stressor, not the app. Confirmed empirically (reviews/04):
container_oom_events_total=445 vs restarts=23 -- ~95% of OOM kills
were landing on the stressor, not catalogue. Protecting the stressor
with oomScoreAdj forces the kernel to kill the app instead, every time.

disk-full: NOT Chaos Mesh -- this installation's StressChaos only
supports cpu/memory stressors (confirmed via `kubectl explain`), and
the CRDs that exist for I/O (iochaos, blockchaos) simulate faults
(delay/errors) rather than consuming real disk space. So this is
driven directly: `kubectl exec` into the target container, write a
real file past its ephemeral-storage limit, repeat.

Target is queue-master, not payment -- payment's root filesystem is
readOnlyRootFilesystem: true with only tmpfs (Memory-backed) mounts,
so nothing written there would ever count as real disk usage (it'd
silently stress memory instead, misrepresenting this as an oom fault).
queue-master has a writable, disk-backed /tmp (confirmed via exec) and
had no ephemeral-storage limit until patched
(patch_queue_master_ephemeral_limit.sh, 100Mi).

Like crash-loop, this repeats on an interval rather than firing once:
an ephemeral-storage breach doesn't restart the container in place --
kubelet EVICTS the whole pod and the ReplicaSet creates a brand-new
pod object, the same trap pod-kill fell into for crash-loop. Each
cycle resolves the CURRENT pod (which may have changed since the last
cycle) and writes into it.

This is the ONLY place the true fault label is written. The agent must
never read this DB or see chaos-mesh.org resources (see blinding test,
P1 -- rerun the same style of check against any new fault mechanism
before trusting it for real episodes).

GROUND TRUTH IS ONLY RECORDED AFTER THE FAULT'S EFFECT IS VERIFIED, not
just after the k8s API accepted the request. Found the hard way: a
crash-loop episode's `kubectl apply` for its Schedule succeeded fine,
but chaos-daemon's own logs showed repeated "no running task found:
task ... not found" -- a stale containerd task reference inside Chaos
Mesh itself -- so the kill silently never executed underneath. The
restart count never moved, but without this fix the episode would have
been recorded as real ground truth anyway, corrupting the scorer, the
trust engine, and the (global) circuit breaker for a fault that never
actually happened -- none of that would be the agent's fault, but it
would have looked like one. Each class now: captures a baseline,
injects, polls for its own effect signal, and retries (re-injecting)
up to MAX_INJECT_ATTEMPTS before giving up. On total failure, NO
episode is recorded at all -- a loud warning prints instead, so a
consistently failing injector shows up as "no unscored episodes found"
rather than silently poisoning the data.

Usage:
    python injector.py --class crash-loop
    python injector.py --class oom
    python injector.py --class disk-full
"""

import argparse
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

PROMETHEUS_URL = "http://localhost:9090"
MAX_INJECT_ATTEMPTS = 3
EFFECT_VERIFY_TIMEOUT_S = 35  # covers kube-state-metrics' ~30s scrape cycle
EFFECT_VERIFY_POLL_S = 5

FAULT_CONFIG = {
    "crash-loop": {
        "namespace": "sock-shop",
        "target": "carts",
        "container": "carts",
        "duration_s": 40,
        "kill_pattern": "app.jar",
    },
    "oom": {
        "namespace": "sock-shop",
        "target": "catalogue",
        "container": "catalogue",
        "duration_s": 60,
        "chaos_name_prefix": "wardence-oom",
    },
    "disk-full": {
        "namespace": "sock-shop",
        "target": "queue-master",
        "container": "queue-master",
        "duration_s": 60,
    },
}

CRASH_LOOP_KILL_INTERVAL_S = 10  # matches the original chaos-mesh cron cadence
OOM_STRESS_SIZE = "250M"  # catalogue's memory limit is 200Mi; stress-ng format, not Ki/Mi

DISK_FULL_FIRE_INTERVAL_S = 15  # wait between write attempts, giving kubelet time to detect+evict
DISK_STRESS_BYTES = 150_000_000  # queue-master's ephemeral-storage limit is 100Mi

# DB lives on WSL2's native filesystem, not the Windows-mounted C:/ path --
# DrvFs (WSL2's NTFS translation layer) has known SQLite file-locking bugs
# that caused hangs/"unable to open database file" when scripts hammered
# a DB on /mnt/c.
OUTPUT_DIR = Path.home() / "wardence_p2_data"
DB_PATH = OUTPUT_DIR / "wardence.db"


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


def build_oom_manifest(chaos_name: str, cfg: dict, size: str = OOM_STRESS_SIZE) -> str:
    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: {chaos_name}
  namespace: chaos-mesh
spec:
  mode: one
  containerNames:
    - {cfg['container']}
  selector:
    namespaces:
      - {cfg['namespace']}
    labelSelectors:
      name: {cfg['target']}
  stressors:
    memory:
      workers: 1
      size: "{size}"
      oomScoreAdj: -1000
"""


def _current_pod_name(target: str, namespace: str) -> str | None:
    """
    Found the hard way (reviews/04 follow-up): with no phase filter,
    `items[0]` is whatever the API happens to return first -- NOT
    guaranteed to be the healthy pod. Old evicted/errored pod objects
    from earlier episodes don't disappear immediately, so after enough
    testing on a target (queue-master had 2 dead `Error` pods sitting
    alongside the 1 live one), this was silently resolving to a DEAD
    pod. `kubectl exec` against it fails every time with no visible
    error in this function (subprocess.run below swallows it, matching
    every other caller's best-effort pattern) -- which is exactly what
    made disk-full's writes silently never land while looking, from the
    caller's side, like nothing was wrong. verifier.py's own
    _current_pod_name (a separate implementation) already filtered for
    phase=Running via a Prometheus query; this one never got the same
    protection until now.
    """
    result = subprocess.run(
        [
            "kubectl", "get", "pods", "-n", namespace,
            "-l", f"name={target}",
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    name = result.stdout.strip()
    return name or None


def _write_large_file(pod_name: str, namespace: str, container: str):
    # best-effort: the pod may get evicted mid-write, or the write may
    # fail once the limit is already breached -- both are fine, the
    # eviction is the actual fault signal we want, not this exec call
    # succeeding cleanly.
    subprocess.run(
        [
            "kubectl", "exec", "-n", namespace, pod_name, "-c", container,
            "--", "sh", "-c",
            f"head -c {DISK_STRESS_BYTES} /dev/zero > /tmp/wardence_fill_$$",
        ],
        capture_output=True,
        text=True,
    )


def _kill_main_process(pod_name: str, namespace: str, container: str, kill_pattern: str) -> bool:
    """
    Returns True if pkill actually matched and signaled a process, False
    if it found nothing to kill. NOT `kill -9 1` -- empirically confirmed
    wrong for carts specifically: `ps aux` showed PID 1 is a shell
    wrapper (java.sh) that launches the real JVM as a CHILD process, not
    via `exec`. In theory the kernel should still tear down the whole
    PID namespace when PID 1 dies, but that didn't happen here (verified
    manually: `kill -9 1` returned exit 0, restart count never moved,
    pod never blipped). Killing the actual process by name pattern
    instead -- confirmed this DOES trigger a real, immediate restart.
    -9 (SIGKILL) is deliberate: a graceful SIGTERM would let the app
    shut down cleanly (reason "Completed"), a real, different signal
    from what a genuine crash produces (reason "Error").

    The return value matters (found the hard way, per reviews/04): a
    silently-ignored return code means a broken kill_pattern (e.g. after
    some future image change) looks identical to "container was
    transiently down between kills" -- both produce silent no-ops and
    eventually just "no restart detected" with no clue why. pkill's exit
    code 1 specifically means "no process matched," which is worth a
    loud print if it happens (though NOT worth raising/aborting the
    loop on a single miss -- the container legitimately isn't always
    running between our own kills, that's expected and fine).
    """
    result = subprocess.run(
        ["kubectl", "exec", "-n", namespace, pod_name, "-c", container, "--", "pkill", "-9", "-f", kill_pattern],
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        print(f"    (pkill found no process matching '{kill_pattern}' in {pod_name} -- may be mid-restart, or the pattern may be stale)")
        return False
    return result.returncode == 0


def run_crash_loop_injection(cfg: dict, duration_s: int):
    """
    Repeatedly SIGKILLs the container's real application process (by
    name pattern, NOT PID 1 -- see _kill_main_process) via kubectl exec
    on the SAME pod (in-place restart, same as the old container-kill
    action), so restarts genuinely accumulate and kubelet can back off
    into CrashLoopBackOff. Not a Chaos Mesh resource -- see module
    docstring for why.
    """
    end_time = time.time() + duration_s
    while time.time() < end_time:
        pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
        if pod_name is None:
            time.sleep(2)
            continue
        _kill_main_process(pod_name, cfg["namespace"], cfg["container"], cfg["kill_pattern"])
        time.sleep(CRASH_LOOP_KILL_INTERVAL_S)


def _restart_count(target: str, namespace: str) -> int:
    """Prefix-match sum is fine here -- called only as a pre-injection
    baseline, before anything has happened to create ambiguity between
    an old and new pod."""
    query = f'kube_pod_container_status_restarts_total{{namespace="{namespace}", pod=~"{target}.*"}}'
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    return sum(int(float(entry["value"][1])) for entry in result)


def _crash_loop_backoff_now(target: str, namespace: str) -> bool:
    """
    Found the hard way: after enough repeated crash-loop testing across
    a long session, kubelet's exponential backoff delay between restarts
    can grow well past our detection window (confirmed empirically: an
    ~8 minute gap between one restart and the next, vs. a ~75s window).
    Restart-count-increase alone can't see a fault that's real but whose
    NEXT restart kubelet is deliberately delaying. Being in
    CrashLoopBackOff right now is itself valid, standing evidence the
    fault is active -- it doesn't require catching the next tick. This
    mirrors agent.py's own crash_query, which already checks this same
    signal for diagnosis; the injector's self-verification was missing
    the other half of that same OR.
    """
    query = (
        f'kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="CrashLoopBackOff"}} == 1'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _verify_crash_loop_effect(target: str, namespace: str, baseline_restarts: int) -> bool:
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _restart_count(target, namespace) > baseline_restarts:
            return True
        if _crash_loop_backoff_now(target, namespace):
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _pod_evicted_since(target: str, namespace: str, since_ts: float) -> bool:
    """
    Bounded to THIS episode's own start time, not a fixed window --
    more precise than agent.py's 3-minute heuristic since the injector
    knows exactly when it began. Without this bound, an old evicted pod
    lingering from a PREVIOUS episode would make the injector think
    THIS episode's injection succeeded when it didn't.
    """
    query = (
        f'(kube_pod_status_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="Evicted"}} == 1) '
        f'and on(namespace, pod) (kube_pod_deletion_timestamp'
        f'{{namespace="{namespace}", pod=~"{target}.*"}} > {since_ts})'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _cleanup_disk_full_files(target: str, namespace: str, container: str):
    """
    Best-effort cleanup after every disk-full attempt, success or
    failure. If eviction never fired, the written files would otherwise
    sit there and poison the NEXT episode on this target -- including a
    genuine no-fault control. Idempotent: if the pod was evicted, the
    new pod has no files to delete and this is just a no-op 404.
    """
    pod_name = _current_pod_name(target, namespace)
    if pod_name is None:
        return
    subprocess.run(
        [
            "kubectl", "exec", "-n", namespace, pod_name, "-c", container,
            "--", "sh", "-c", "rm -f /tmp/wardence_fill_*",
        ],
        capture_output=True,
        text=True,
    )


def _verify_restart_effect(target: str, namespace: str, baseline_restarts: int) -> bool:
    """Used by both crash-loop and oom -- both classes' real signal is
    simply "did this pod restart since baseline," regardless of exact
    cause. Polls briefly to give kube-state-metrics' scrape cycle a
    chance to catch up rather than judging on a single instant."""
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _restart_count(target, namespace) > baseline_restarts:
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def _verify_disk_full_effect(
    target: str, namespace: str, since_ts: float, baseline_pod_name: str | None
) -> bool:
    elapsed = 0
    while elapsed <= EFFECT_VERIFY_TIMEOUT_S:
        if _pod_evicted_since(target, namespace, since_ts):
            return True
        current_pod_name = _current_pod_name(target, namespace)
        if current_pod_name is not None and current_pod_name != baseline_pod_name:
            return True
        time.sleep(EFFECT_VERIFY_POLL_S)
        elapsed += EFFECT_VERIFY_POLL_S
    return False


def run_disk_full_injection(cfg: dict, duration_s: int):
    """
    Repeatedly resolves the CURRENT pod (which changes identity each
    time kubelet evicts and the ReplicaSet recreates it) and writes a
    file past the ephemeral-storage limit into it. Not a Chaos Mesh
    resource -- see module docstring for why.
    """
    end_time = time.time() + duration_s
    while time.time() < end_time:
        pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
        if pod_name is None:
            time.sleep(2)
            continue
        _write_large_file(pod_name, cfg["namespace"], cfg["container"])
        time.sleep(DISK_FULL_FIRE_INTERVAL_S)


def apply_manifest(manifest: str):
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl apply failed:\n{result.stderr}")
    print(result.stdout.strip())


def delete_chaos_resource(kind: str, name: str):
    result = subprocess.run(
        ["kubectl", "delete", kind, name, "-n", "chaos-mesh"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: failed to delete {kind} {name}:\n{result.stderr}")
    else:
        print(result.stdout.strip())


def record_episode(
    conn: sqlite3.Connection, episode_id: str, fault_class: str, cfg: dict, chaos_name: str, t0: str
):
    conn.execute(
        "INSERT INTO episodes (episode_id, fault_class, target, namespace, t0, chaos_resource_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (episode_id, fault_class, cfg["target"], cfg["namespace"], t0, chaos_name),
    )
    conn.commit()


def _inject_and_verify_disk_full(cfg: dict) -> bool:
    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_pod_name = _current_pod_name(cfg["target"], cfg["namespace"])
        since_ts = time.time()
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: running exec-based disk fill for {cfg['duration_s']}s...")
        run_disk_full_injection(cfg, cfg["duration_s"])
        try:
            verified = _verify_disk_full_effect(cfg["target"], cfg["namespace"], since_ts, baseline_pod_name)
        finally:
            # Cleanup must run even if verification itself throws (e.g. a
            # Prometheus hiccup) -- otherwise leftover files silently
            # poison the next episode on this target, exactly the bug
            # this whole verify-before-record fix exists to prevent.
            _cleanup_disk_full_files(cfg["target"], cfg["namespace"], cfg["container"])
        if verified:
            return True
        print(f"  attempt {attempt}: no eviction/pod-churn detected, retrying" if attempt < MAX_INJECT_ATTEMPTS else f"  attempt {attempt}: no eviction/pod-churn detected")
    return False


def _inject_and_verify_crash_loop(cfg: dict) -> bool:
    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        baseline_restarts = _restart_count(cfg["target"], cfg["namespace"])
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: running exec-based kill loop for {cfg['duration_s']}s...")
        run_crash_loop_injection(cfg, cfg["duration_s"])
        verified = _verify_crash_loop_effect(cfg["target"], cfg["namespace"], baseline_restarts)
        if verified:
            return True
        print(f"  attempt {attempt}: no restart detected, retrying" if attempt < MAX_INJECT_ATTEMPTS else f"  attempt {attempt}: no restart detected")
    return False


def _inject_and_verify_chaos_mesh(fault_class: str, cfg: dict, manifest_builder) -> str | None:
    """Returns the chaos_name of the attempt that got verified, or None
    if all attempts failed. Only oom uses this now -- crash-loop moved
    to the exec-based mechanism above, disk-full never used Chaos Mesh
    at all."""
    chaos_kind = "stresschaos"
    baseline_restarts = _restart_count(cfg["target"], cfg["namespace"])

    for attempt in range(1, MAX_INJECT_ATTEMPTS + 1):
        chaos_name = f"{cfg.get('chaos_name_prefix', fault_class)}-{uuid.uuid4().hex[:8]}"
        manifest = manifest_builder(chaos_name, cfg)
        apply_manifest(manifest)
        print(f"  attempt {attempt}/{MAX_INJECT_ATTEMPTS}: letting it run for {cfg['duration_s']}s...")
        time.sleep(cfg["duration_s"])
        try:
            verified = _verify_restart_effect(cfg["target"], cfg["namespace"], baseline_restarts)
        finally:
            # Cleanup must run even if verification throws -- an active
            # Schedule left behind on a connection hiccup would keep
            # killing the target pod indefinitely, corrupting every
            # future episode on it until someone notices manually.
            delete_chaos_resource(chaos_kind, chaos_name)
        if verified:
            return chaos_name
        print(f"  attempt {attempt}: no restart detected, retrying" if attempt < MAX_INJECT_ATTEMPTS else f"  attempt {attempt}: no restart detected")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--class", dest="fault_class", required=True, choices=FAULT_CONFIG.keys())
    args = parser.parse_args()

    fault_class = args.fault_class
    cfg = FAULT_CONFIG[fault_class]

    episode_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc).isoformat()
    print(f"Episode {episode_id}: attempting {fault_class} on {cfg['target']} ({cfg['namespace']}) at {t0}")

    if fault_class == "disk-full":
        verified = _inject_and_verify_disk_full(cfg)
        chaos_name = "manual-exec" if verified else None
    elif fault_class == "crash-loop":
        verified = _inject_and_verify_crash_loop(cfg)
        chaos_name = "manual-exec" if verified else None
    else:
        chaos_name = _inject_and_verify_chaos_mesh(fault_class, cfg, build_oom_manifest)

    if not chaos_name:
        print(
            f"INJECTION FAILED after {MAX_INJECT_ATTEMPTS} attempts for {fault_class} on "
            f"{cfg['target']} -- NO episode recorded. If this keeps happening, the cluster "
            f"(or Chaos Mesh's own daemon) is unhealthy, not the diagnosis/verifier code."
        )
        return

    conn = ensure_db()
    record_episode(conn, episode_id, fault_class, cfg, chaos_name, t0)
    conn.close()
    print(f"Episode {episode_id}: injection verified ({chaos_name}) and ground truth recorded.")


if __name__ == "__main__":
    main()
