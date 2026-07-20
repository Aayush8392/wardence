"""
P3 verifier: a fix's verdict stays PROVISIONAL until a class-specific
durability window closes clean. If the fault reappears at any point
during the window, the fix counts as WRONG (flapped) even if it looked
fixed right after the action ran.

Only crash-loop's "still faulty" check is implemented so far, reusing
the same PromQL proven in p2_readonly_loop/agent.py. OOM and disk-full
checks are stubbed until those fix actions are actually built and their
detection logic is sanity-checked the same way crash-loop's was in P2
(see wardence_buildlog.md -- container-kill vs pod-kill correction).
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


def _oom_still_faulty(target: str, namespace: str) -> bool:
    raise NotImplementedError("OOM fix action + detection not built yet")


def _disk_full_still_faulty(target: str, namespace: str) -> bool:
    raise NotImplementedError("disk-full fix action + detection not built yet")


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
        pod_name = _current_pod_name(target, namespace)
        baseline = _restart_count(pod_name, namespace)
        check_fn = _make_crash_loop_check(pod_name, baseline)
    elif fault_class == "oom":
        check_fn = _oom_still_faulty
    elif fault_class == "disk-full":
        check_fn = _disk_full_still_faulty

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
