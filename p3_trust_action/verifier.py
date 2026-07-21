"""
P3 verifier: a fix's verdict stays PROVISIONAL until a class-specific
durability window closes clean. If the fault reappears at any point
during the window, the fix counts as WRONG (flapped) even if it looked
fixed right after the action ran.

crash-loop, oom, and disk-full "still faulty" checks are all
implemented. disk-full's check is structurally different from the
other two -- see _make_disk_full_check.
"""

import time

import requests

PROMETHEUS_URL = "http://localhost:9090"

# Seconds, per the locked fault taxonomy (wardence_context.md).
DURABILITY_WINDOWS = {
    "crash-loop": 120,
    "oom": 180,
    "disk-full": 120,
}

POLL_INTERVAL_S = 15


def _current_pod_name(target: str, namespace: str) -> str:
    """
    A fix action (e.g. restart_deployment) creates a NEW pod with a new
    name suffix. Matching by name PREFIX (pod=~"target.*") can still
    pick up the just-terminated OLD pod's stale restart count / status
    for a short time before Prometheus's next scrape reflects its
    removal -- contaminating verification with pre-fix data. Resolving
    the exact current pod name once and matching on it exactly avoids
    that.
    """
    query = (
        f'kube_pod_status_phase{{namespace="{namespace}", '
        f'pod=~"{target}.*", phase="Running"}} == 1'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        raise RuntimeError(f"no Running pod found matching '{target}' in {namespace}")
    # If more than one is Running (rollout mid-transition), the newest
    # pod name sorts last for the standard <name>-<hash>-<hash> scheme.
    pod_names = sorted(entry["metric"]["pod"] for entry in result)
    return pod_names[-1]


BASELINE_CAPTURE_RETRY_S = 30
BASELINE_CAPTURE_POLL_S = 5


def _current_pod_name_with_retry(target: str, namespace: str) -> str:
    """
    scale_deployment (disk-full's fix) scales to 0 then back up -- there's
    a real window with zero Running pods. Calling _current_pod_name
    uncaught at baseline capture would crash the verifier (and the
    scorer, unhandled) the first time a disk-full auto-fix runs. Retries
    for up to BASELINE_CAPTURE_RETRY_S before giving up for real.
    """
    elapsed = 0
    while True:
        try:
            return _current_pod_name(target, namespace)
        except RuntimeError:
            if elapsed >= BASELINE_CAPTURE_RETRY_S:
                raise
            time.sleep(BASELINE_CAPTURE_POLL_S)
            elapsed += BASELINE_CAPTURE_POLL_S


def _restart_count(pod_name: str, namespace: str) -> int:
    query = f'kube_pod_container_status_restarts_total{{namespace="{namespace}", pod="{pod_name}"}}'
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    return sum(int(float(entry["value"][1])) for entry in result)


def _crash_loop_backoff_now(pod_name: str, namespace: str) -> bool:
    query = (
        f'kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod="{pod_name}", reason="CrashLoopBackOff"}} == 1'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _make_crash_loop_check(pod_name: str, baseline_restarts: int):
    """
    Scoped to the exact pod resolved when the durability window started
    -- see _current_pod_name for why exact-match beats prefix-match here.
    """

    def check(target: str, namespace: str) -> bool:
        if _crash_loop_backoff_now(pod_name, namespace):
            return True
        return _restart_count(pod_name, namespace) > baseline_restarts

    return check


def _make_oom_check(pod_name: str, baseline_restarts: int):
    """
    Originally also checked last_terminated_reason="OOMKilled" as a
    fallback signal, same shape as the crash-loop check. Dropped after
    mixed-class validation exposed why that's actually dangerous here:
    last_terminated_reason is a gauge with no expiry -- it stays
    "OOMKilled" until the SAME pod/container terminates again for a
    different reason. A resource-limit patch fix (patch_memory_limit)
    doesn't clear it, so every real OOM auto-fix would false-positive
    as "flapped" on the very first poll, even when the fix genuinely
    worked. Restart-count-vs-baseline alone is sufficient and doesn't
    have this staleness problem -- it only trips on an ACTUAL new
    restart since the durability window started.
    """

    def check(target: str, namespace: str) -> bool:
        return _restart_count(pod_name, namespace) > baseline_restarts

    return check


def _evicted_recently(target: str, namespace: str) -> bool:
    """
    Time-bounded to pods EVICTED (not just created) in the last 3
    minutes. Went through two wrong fixes before this one:
      1. Bounded on kube_pod_created (pod age) -- wrong: a pod can run
         healthy for a long time before being evicted, so its creation
         time can be old even though the eviction just happened,
         incorrectly excluding a genuine fresh eviction (proven wrong
         empirically: disk-full false-negatived 5/5 in a mixed
         validation run with that bound).
      2. Bounded on kube_pod_deletion_timestamp -- also wrong,
         empirically: queried this directly against genuinely-just-
         evicted pods and it returned nothing at all, even though
         status_reason clearly showed "Evicted". Eviction on this
         cluster doesn't reliably populate that field.
    Landed on: bound by whether the CURRENT Running pod for this
    target was created recently, instead of trusting the evicted pod's
    own metadata at all. A fresh healthy replacement pod existing is
    itself direct evidence a churn just happened. Same fix applied to
    agent.py's evicted_query -- see that file for the full derivation.
    """
    evicted_query = (
        f'kube_pod_status_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="Evicted"}} == 1'
    )
    evicted_resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": evicted_query}, timeout=10)
    evicted_resp.raise_for_status()
    if not evicted_resp.json()["data"]["result"]:
        return False

    recent_running_pod_query = (
        f'(kube_pod_status_phase{{namespace="{namespace}", pod=~"{target}.*", phase="Running"}} == 1) '
        f'and on(namespace, pod) ((time() - kube_pod_created'
        f'{{namespace="{namespace}", pod=~"{target}.*"}}) < 180)'
    )
    recent_resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": recent_running_pod_query}, timeout=10)
    recent_resp.raise_for_status()
    return len(recent_resp.json()["data"]["result"]) > 0


def _make_disk_full_check(baseline_pod_name: str):
    """
    disk-full doesn't restart a container in place like crash-loop/oom --
    an ephemeral-storage breach makes kubelet EVICT the whole pod, and
    the ReplicaSet creates a brand-new pod object to replace it. There's
    no single pod identity to accumulate a restart count on, so this
    checks for the Evicted status reason directly, falling back to pod
    IDENTITY CHURN (a different pod name than the one resolved when the
    durability window started) in case Prometheus's scrape missed the
    Evicted status before the pod object was already replaced.
    """

    def check(target: str, namespace: str) -> bool:
        if _evicted_recently(target, namespace):
            return True
        try:
            current_pod_name = _current_pod_name(target, namespace)
        except RuntimeError:
            # no Running pod right now -- mid eviction/recreation, treat as still faulty
            return True
        return current_pod_name != baseline_pod_name

    return check


def verify_durability(fault_class: str, target: str, namespace: str = "sock-shop") -> dict:
    """
    Poll until the fault class's durability window closes.

    Returns {"verdict": "confirmed" | "flapped", "elapsed_s": int,
    "fault_class": ..., "target": ...}. "flapped" is returned the
    moment the fault reappears -- no need to wait out the rest of
    the window once it's already wrong.
    """
    if fault_class not in DURABILITY_WINDOWS:
        raise ValueError(f"no durability window defined for fault class '{fault_class}'")

    if fault_class == "crash-loop":
        pod_name = _current_pod_name_with_retry(target, namespace)
        baseline = _restart_count(pod_name, namespace)
        check_fn = _make_crash_loop_check(pod_name, baseline)
    elif fault_class == "oom":
        pod_name = _current_pod_name_with_retry(target, namespace)
        baseline = _restart_count(pod_name, namespace)
        check_fn = _make_oom_check(pod_name, baseline)
    elif fault_class == "disk-full":
        baseline_pod_name = _current_pod_name_with_retry(target, namespace)
        check_fn = _make_disk_full_check(baseline_pod_name)

    window_s = DURABILITY_WINDOWS[fault_class]
    elapsed = 0

    while elapsed < window_s:
        time.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S

        if check_fn(target, namespace):
            return {
                "verdict": "flapped",
                "elapsed_s": elapsed,
                "fault_class": fault_class,
                "target": target,
            }

    return {
        "verdict": "confirmed",
        "elapsed_s": elapsed,
        "fault_class": fault_class,
        "target": target,
    }
