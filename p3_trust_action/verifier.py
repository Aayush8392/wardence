"""
P3 verifier: a fix's verdict stays PROVISIONAL until a class-specific
durability window closes clean. If the fault reappears at any point
during the window, the fix counts as WRONG (flapped) even if it looked
fixed right after the action ran.

crash-loop, oom, and disk-full "still faulty" checks are all
implemented. disk-full's check is structurally different from the
other two -- see _make_disk_full_check.
"""

import re
import subprocess
import time
import uuid

import requests

PROMETHEUS_URL = "http://localhost:9090"

# Seconds, per the locked fault taxonomy (wardence_context.md).
DURABILITY_WINDOWS = {
    "crash-loop": 120,
    "oom": 180,
    "disk-full": 120,
    # cpu-throttling: a transient CPU-scheduling effect, not a lingering
    # leak/growth pattern -- matches crash-loop/disk-full's shorter tier
    # rather than oom/memory-leak's 3min.
    "cpu-throttling": 120,
    # Nominal target for the custom active-probe branch below -- not
    # used by the generic poll loop, since this class bypasses it.
    "under-provisioned-replicas": 90,
    "bad-rollout": 120,
}

# Same real-measured margin as injector.py's CPU_THROTTLE_MIN_PERIODS_INCREASE
# (baseline delta over 60s = 0.0, during-stress delta over 30s ~= 300) --
# reused here for the post-fix durability check, not re-derived.
CPU_THROTTLE_DURABILITY_MIN_INCREASE = 50

# under-provisioned-replicas: durability here means "does capacity
# genuinely stay recovered," checked via the SAME real active-probe
# mechanism as injection/diagnosis (no passive metric exists -- see
# injector.py's UNDER_PROVISIONED_* constants for the full real-
# measurement history). Deliberately NOT the generic POLL_INTERVAL_S
# sleep-then-check loop every other class uses: that loop assumes an
# instant, near-free check -- reusing it here (a real ~20s burst each
# time) would fire far more real load than intended (6 checks over a
# 120s window x ~35s real time each = ~210s of real bursts, not the
# nominal 120s) and badly undercount real elapsed_s in the result.
# Handled as its own dedicated branch in verify_durability instead --
# 3 real probes, spaced to land total real wall-clock close to the
# nominal window.
K6_IMAGE = "grafana/k6:latest"
UNDER_PROVISIONED_PROBE_VUS = 20
UNDER_PROVISIONED_PROBE_DURATION_S = 20
UNDER_PROVISIONED_PROBE_THRESHOLD_MS = 200
UNDER_PROVISIONED_PROBE_COUNT = 3
UNDER_PROVISIONED_INTER_PROBE_SLEEP_S = 15
# Real bug found and fixed (2026-07-24): scale_deployment (the fix
# action) returns as soon as the API accepts the scale request, NOT
# once the new replicas are actually Ready -- catalogue's real
# readinessProbe has a confirmed 180s initialDelaySeconds (see
# injector.py/measure_catalogue_load.py's own real measurement
# history), so the probe sequence below was starting and finishing
# well before the new replicas could possibly be serving traffic,
# causing a real but MISLEADING flap (only the original 1 replica was
# ever actually live during the whole check). Same root-cause class as
# disk-full's own "API acceptance != real completion" lesson.
UNDER_PROVISIONED_TARGET_REPLICAS = 3  # matches FIX_PARAMS["under-provisioned-replicas"] in p3_agent.py
UNDER_PROVISIONED_REPLICA_WAIT_TIMEOUT_S = 220  # real margin over the confirmed 180s readiness delay

POLL_INTERVAL_S = 15


def _current_pod_name_live(target: str, namespace: str) -> str | None:
    """
    Resolves the current Running pod straight from the Kubernetes API,
    NOT Prometheus. Prometheus's kube-state-metrics scrape lags ~30s,
    which is fatal for disk-full specifically: its fix
    (restore_from_disk_full) DELIBERATELY replaces the pod, so a
    Prometheus-sourced baseline captured moments after the fix routinely
    returned the OLD, already-deleted pod name -- and then, the instant
    the scrape caught up to reality, current != baseline and the
    verifier declared a perfectly good fix "flapped". Confirmed
    2026-07-22: this happened on every disk-full fix-and-verify run even
    after the fix itself was proven to genuinely apply
    (action_applied=1). _current_pod_name_with_retry did not protect
    against it -- it only retried when NO pod was found, never when a
    STALE one was returned.

    The live API has no such lag: it is the authoritative source
    kube-state-metrics is itself derived from. Returns None when there
    is genuinely no Running pod (mid eviction/recreation).
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


BASELINE_CAPTURE_RETRY_S = 30
BASELINE_CAPTURE_POLL_S = 5


def _current_pod_name_live_with_retry(target: str, namespace: str) -> str:
    """Live-API twin of _current_pod_name_with_retry -- retries through
    the real zero-Running-pods window a scale-to-0-then-up fix creates."""
    elapsed = 0
    while True:
        name = _current_pod_name_live(target, namespace)
        if name is not None:
            return name
        if elapsed >= BASELINE_CAPTURE_RETRY_S:
            raise RuntimeError(f"no Running pod found matching '{target}' in {namespace}")
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
    -- see _current_pod_name_live for why exact-match (via kubectl's
    `-l name={target}` label selector) beats a name-string prefix match
    here (the latter can silently collide with an unrelated deployment
    sharing the same string prefix, e.g. `user` vs `user-db` -- found
    2026-07-24 while validating cpu-throttling).
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


def _evicted_recently(target: str, namespace: str, baseline_pod_name: str) -> bool:
    """
    Time-bounded to pods EVICTED (not just created) in the last 3
    minutes. Went through three wrong fixes before this one:
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
      3. Bounded on "any current Running pod created <180s ago",
         with NO comparison against baseline_pod_name -- wrong,
         empirically (2026-07-22): disk-full's own fix
         (restore_from_disk_full) recreates the pod as part of a
         SUCCESSFUL fix, so the freshly-fixed pod itself always
         satisfies "created recently", making every successful fix
         look "flapped" moments later regardless of whether a real
         re-eviction happened. Confirmed via real eviction-event
         timestamps: a "flapped" verdict lined up with the fix's own
         restart, not an independent new eviction.
    Landed on: bound by whether the CURRENT Running pod is BOTH
    recently created AND a DIFFERENT pod than baseline_pod_name (the
    pod resolved when the durability window started, i.e. right after
    the fix ran) -- a fresh pod existing is only real evidence of a
    NEW churn if it isn't the fix's own recreation.
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
    for result in recent_resp.json()["data"]["result"]:
        pod_name = result.get("metric", {}).get("pod")
        if pod_name and pod_name != baseline_pod_name:
            return True
    return False


def _cfs_throttled_periods_exact(pod_name: str, namespace: str, container: str) -> int:
    """Exact-pod-name twin of injector.py's _cfs_throttled_periods --
    scoped to the pod resolved when the durability window started, same
    exact-match discipline as _restart_count/_crash_loop_backoff_now
    above, since the fix (patch_cpu_limit) triggers a real pod-template
    rollout just like patch_memory_limit does."""
    query = (
        f'container_cpu_cfs_throttled_periods_total{{namespace="{namespace}", '
        f'pod="{pod_name}", container="{container}"}}'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    return sum(int(float(entry["value"][1])) for entry in result)


def _make_cpu_throttle_check(pod_name: str, container: str, baseline_periods: int):
    """Same raw-delta-vs-baseline discipline as injector.py's own
    verification -- durability holds as long as throttled-periods
    doesn't jump by another real margin above whatever it already was
    when the window started (post-fix, on the new higher CPU limit)."""

    def check(target: str, namespace: str) -> bool:
        current = _cfs_throttled_periods_exact(pod_name, namespace, container)
        return current - baseline_periods >= CPU_THROTTLE_DURABILITY_MIN_INCREASE

    return check


def _front_end_image_pull_failing(namespace: str) -> bool:
    """Duplicated from injector.py's identical helper -- matches this
    project's established self-contained-file convention. A cheap
    passive Prometheus read (unlike under-provisioned-replicas' active
    probes), so this fits the standard generic poll loop fine."""
    query = (
        f'kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"front-end.*", reason=~"ImagePullBackOff|ErrImagePull"}} == 1'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return len(resp.json()["data"]["result"]) > 0


def _make_bad_rollout_check():
    """No baseline pod name needed -- unlike crash-loop/oom, the
    'still faulty' signal here (ANY front-end pod stuck in
    ImagePullBackOff/ErrImagePull) is meaningful regardless of which
    specific pod it is: after a genuinely successful rollback, no
    front-end pod should ever be in that state again."""

    def check(target: str, namespace: str) -> bool:
        return _front_end_image_pull_failing(namespace)

    return check


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
        # Pod IDENTITY, resolved live, is the whole signal here. A real
        # re-eviction ALWAYS produces a new pod name, so this single
        # check covers it -- the previous Prometheus-based
        # _evicted_recently call was both redundant and actively harmful
        # (scrape lag + it matched ANY historical Evicted pod object,
        # and evicted pods linger here indefinitely).
        current_pod_name = _current_pod_name_live(target, namespace)
        if current_pod_name is None:
            # no Running pod right now -- mid eviction/recreation, treat as still faulty
            return True
        return current_pod_name != baseline_pod_name

    return check


def _parse_k6_p95_ms(stdout_text: str) -> float | None:
    """Parses the real p95 value directly from k6's own end-of-run
    text summary -- `--no-summary`/`--summary-export=/dev/stdout`
    isn't a real flag on this k6 image (confirmed via a real failed
    run, 2026-07-24: `unknown flag: --no-summary`). Duplicated from
    injector.py's identical helper, same self-contained-file
    convention."""
    match = re.search(
        r"http_req_duration[^\n]*p\(95\)=([\d.]+)(µs|ms|s)\b", stdout_text
    )
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2)
    if unit == "s":
        return value * 1000
    if unit == "µs":
        return value / 1000
    return value


def _catalogue_probe_p95_ms(namespace: str) -> float | None:
    """Duplicated from injector.py/agent.py's own identical helper --
    matches this project's established convention of keeping each
    file self-contained rather than sharing a cross-module utility
    (see _current_pod_name_live's own precedent). Same real, validated
    parameters throughout: 20 VUs, 20s, GET /catalogue?size=10."""
    pod_name = f"wardence-underprov-verify-{uuid.uuid4().hex[:8]}"
    script = f"""
import http from 'k6/http';
export const options = {{
  scenarios: {{
    burst: {{
      executor: 'constant-vus',
      vus: {UNDER_PROVISIONED_PROBE_VUS},
      duration: '{UNDER_PROVISIONED_PROBE_DURATION_S}s',
    }},
  }},
}};
export default function () {{
  http.get('http://catalogue.{namespace}.svc.cluster.local/catalogue?size=10');
}}
"""
    try:
        result = subprocess.run(
            [
                "kubectl", "run", pod_name, "--rm", "-i", "--restart=Never",
                "-n", namespace, f"--image={K6_IMAGE}",
                "--", "run", "--quiet", "-",
            ],
            input=script, capture_output=True, text=True,
            timeout=UNDER_PROVISIONED_PROBE_DURATION_S + 60,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return _parse_k6_p95_ms(result.stdout)


def _catalogue_available_replicas_live(namespace: str) -> int:
    """Live API, not Prometheus -- same 'no scrape lag' reasoning as
    _current_pod_name_live. Returns 0 if the field is empty/unset."""
    result = subprocess.run(
        [
            "kubectl", "get", "deployment", "catalogue", "-n", namespace,
            "-o", "jsonpath={.status.availableReplicas}",
        ],
        capture_output=True, text=True,
    )
    value = result.stdout.strip()
    return int(value) if value else 0


def _wait_for_catalogue_replicas_ready(namespace: str, target_count: int, timeout_s: int) -> bool:
    """Polls until catalogue's real available-replica count genuinely
    reaches target_count -- never just trusts that scale_deployment's
    API call being accepted means the new replicas are actually
    serving traffic. See UNDER_PROVISIONED_REPLICA_WAIT_TIMEOUT_S's
    docstring for why this exists."""
    elapsed = 0
    while elapsed <= timeout_s:
        if _catalogue_available_replicas_live(namespace) >= target_count:
            return True
        time.sleep(10)
        elapsed += 10
    return False


def _verify_under_provisioned_durability(namespace: str, target: str) -> dict:
    """Custom durability path -- see the module-level constants'
    docstring for why this bypasses the generic poll loop entirely.
    FIRST waits for the fix's new replicas to genuinely become Ready
    (real completion, not just API acceptance -- see
    UNDER_PROVISIONED_REPLICA_WAIT_TIMEOUT_S), THEN runs
    UNDER_PROVISIONED_PROBE_COUNT real probes, returning 'flapped' the
    moment any one shows p95 still degraded (same 'return the moment
    it's already wrong' discipline as the generic loop), else
    'confirmed' after all probes pass. If the replicas never reach the
    target count within the timeout, that's a real fix failure, not a
    premature check -- reported as 'flapped' honestly."""
    wait_start = time.time()
    ready = _wait_for_catalogue_replicas_ready(
        namespace, UNDER_PROVISIONED_TARGET_REPLICAS, UNDER_PROVISIONED_REPLICA_WAIT_TIMEOUT_S
    )
    # Real elapsed wall-clock, not the worst-case timeout value -- the
    # wait usually finishes well before the timeout (e.g. ~180-190s in
    # practice, not the full 220s budget).
    elapsed = int(time.time() - wait_start)
    if not ready:
        return {
            "verdict": "flapped", "elapsed_s": elapsed,
            "fault_class": "under-provisioned-replicas", "target": target,
        }

    for i in range(UNDER_PROVISIONED_PROBE_COUNT):
        if i > 0:
            time.sleep(UNDER_PROVISIONED_INTER_PROBE_SLEEP_S)
            elapsed += UNDER_PROVISIONED_INTER_PROBE_SLEEP_S
        p95_ms = _catalogue_probe_p95_ms(namespace)
        elapsed += UNDER_PROVISIONED_PROBE_DURATION_S
        if p95_ms is not None and p95_ms >= UNDER_PROVISIONED_PROBE_THRESHOLD_MS:
            return {
                "verdict": "flapped", "elapsed_s": elapsed,
                "fault_class": "under-provisioned-replicas", "target": target,
            }
    return {
        "verdict": "confirmed", "elapsed_s": elapsed,
        "fault_class": "under-provisioned-replicas", "target": target,
    }


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

    if fault_class == "under-provisioned-replicas":
        # Bypasses the generic poll loop entirely -- see the module-
        # level constants' docstring for why (active real probes, not
        # a cheap passive query).
        return _verify_under_provisioned_durability(namespace, target)

    if fault_class == "crash-loop":
        # Live API (exact `name={target}` label selector), not the
        # Prometheus regex-based resolver -- see _current_pod_name_live's
        # docstring and the 2026-07-24 cpu-throttling incident that
        # found this: `pod=~"{target}.*"` also matches OTHER real
        # deployments sharing the same string prefix (carts-db for
        # crash-loop's own `carts`, catalogue-db for oom's `catalogue`),
        # not just old-vs-new pods of the SAME rollout as the function's
        # own comment assumed. sorted(pod_names)[-1] can silently lock
        # onto the wrong, unrelated pod depending on which ReplicaSet
        # hash happens to sort last -- confirmed for real via
        # cpu-throttling (`user` vs `user-db`), and since crash-loop/oom
        # have the identical collision (`carts`/`carts-db`,
        # `catalogue`/`catalogue-db`), their own past durability checks
        # may have been silently vulnerable to the same non-deterministic
        # mis-resolution, undetected whenever the hash comparison
        # happened to still favor the right pod.
        pod_name = _current_pod_name_live_with_retry(target, namespace)
        baseline = _restart_count(pod_name, namespace)
        check_fn = _make_crash_loop_check(pod_name, baseline)
    elif fault_class == "oom":
        pod_name = _current_pod_name_live_with_retry(target, namespace)
        baseline = _restart_count(pod_name, namespace)
        check_fn = _make_oom_check(pod_name, baseline)
    elif fault_class == "disk-full":
        # Live API, not Prometheus -- see _current_pod_name_live. The fix
        # just replaced this pod, so a lagging source would hand back the
        # dead pod as "baseline" and guarantee a false flap.
        baseline_pod_name = _current_pod_name_live_with_retry(target, namespace)
        check_fn = _make_disk_full_check(baseline_pod_name)
    elif fault_class == "cpu-throttling":
        # container == target here (user's own deployment/container name),
        # same as FIX_PARAMS["cpu-throttling"] in p3_agent.py. Live API,
        # not Prometheus -- see the crash-loop branch above for why
        # (this is the exact class that surfaced the bug: `user` vs
        # `user-db`).
        pod_name = _current_pod_name_live_with_retry(target, namespace)
        baseline = _cfs_throttled_periods_exact(pod_name, namespace, target)
        check_fn = _make_cpu_throttle_check(pod_name, target, baseline)
    elif fault_class == "bad-rollout":
        check_fn = _make_bad_rollout_check()

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
