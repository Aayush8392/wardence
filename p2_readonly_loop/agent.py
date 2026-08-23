"""
P2 agent skeleton: diagnose-only, single tool (query_prometheus).

No fix/action capability yet (that's P3, behind the blast-radius cage).
No access to wardence.db / ground-truth labels (blinding preserved).
Reasoning is STUBBED for now (hardcoded rule) -- real LLM call
(Gemini 3 Flash, per locked Model Strategy) gets wired in next step,
same tool-call shape stays.

Usage:
    Terminal 1: kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090
    Terminal 2: uvicorn agent:app --reload
    Then: POST http://localhost:8000/diagnose  {"target": "carts", "namespace": "sock-shop"}
"""

import os
import re
import subprocess
import uuid

import requests
from fastapi import FastAPI
from pydantic import BaseModel

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")

# p5_dl_hardening/detector_service.py -- plain hardcoded local constant,
# matching PROMETHEUS_URL's own pattern (this project's standing local-
# dev convention: manual port-forward/uvicorn per service, no env var or
# config file for a same-machine local service).
DETECTOR_URL = "http://localhost:8010"

# Must be kept in sync with detector_service.py's SUPPORTED_SERVICES
# (front-end/orders/user via DeepLog, catalogue via SPC, queue-master via
# HMM) -- duplicated here rather than importing across p2/p5, matching
# this file's own established convention of keeping each file self-
# contained (see _parse_k6_p95_ms's docstring for the same reasoning).
DL_DETECTOR_SERVICES = {"front-end", "orders", "user", "catalogue", "queue-master"}

# Absolute threshold, not baseline-relative -- the agent only sees one
# snapshot per /diagnose call, unlike injector.py's own verification
# which has a genuine before/after baseline to compare to. Sock Shop's
# normal p95 under traffic_gen's baseline load is well under this
# (injected delay is 500ms, see injector.py NETWORK_LATENCY_DELAY).
HIGH_LATENCY_THRESHOLD_MS = 300

# Real production diagnosis threshold, LOCKED 2026-08-21 session (see
# wardence_buildlog.md's measurement 1-4 chain) -- REPLACES the old
# MEMORY_LEAK_THRESHOLD_MIB absolute check (380MiB, calibrated against the
# now-removed container-stressor mechanism: baseline ~298MiB + ~150MiB
# stressor). That formula is structurally dead against the real production
# mechanism -- shipping's real baseline RSS (~314-334MiB) plus a
# -Xmx128m-capped leak agent has no arithmetic path to 380MiB, and separate
# real measurement (2 real production no-leak samples) proved an absolute
# threshold can't work at all: a freshly-booted JVM's own floor can sit
# BELOW a long-running production JVM's organic floor. Self-referential
# rise-over-baseline is the real fix -- see query_prometheus's heap_rise_kb
# computation below for the full formula.
# 20 MiB -- clears the real measured 8.0 MiB organic ceiling and 1.4-1.7 MiB
# synthetic-load-only ceiling with real margin.
MEMORY_LEAK_RISE_THRESHOLD_KB = 20000

# catalogue-db's max_connections is 151, baseline Threads_connected is
# ~2-3 (both confirmed via direct query, 2026-07-21). 100 sits
# comfortably between normal baseline and the ~142 level reached during
# a real flood (140 injected + baseline).
CONNECTION_POOL_THRESHOLD = 100

# orders' combined (transmit+receive) network throughput, measured
# directly (2026-07-24, not assumed): baseline ~1900-2400 bytes/s
# combined under traffic_gen's normal load, dropping to ~0-100 bytes/s
# combined during a real, verified partition (both directions drop
# together -- confirmed via measure_network_partition_direction_check.py,
# `direction: to` was assumed to only block egress but empirically
# blocks both). 200 sits with a large (~10-20x) real margin below
# baseline and well above the small residual noise observed during a
# real fault.
NETWORK_PARTITION_MAX_THROUGHPUT_BPS = 200

# Real bug found live (2026-07-31, overnight batch): the throughput
# check above can still miss a genuine partition -- confirmed via two
# real misdiagnosed episodes where the min_over_time([2m]) throughput
# query read 2506/9864 bytes/s (at or ABOVE the ~1900-2400 bytes/s
# normal baseline, not below it -- the query's lookback window missed
# the actual low-throughput period during the fault, likely reading
# post-recovery/retry-burst traffic instead; a shuffled multi-hour
# batch run's real diagnosis timing is less predictable than the
# original 2026-07-24 fix assumed). Both misdiagnosed episodes shared
# a second, independent tell the throughput check doesn't use at all:
# p95_latency_ms landed at an exact ~60000ms ceiling -- k6's own
# default request timeout, not an organic percentile. A genuine
# network-latency episode can NEVER produce this: the real injected
# delay is only 500ms (+/-50ms jitter, see NETWORK_LATENCY_DELAY in
# injector.py) -- nothing about that mechanism can push a real p95
# anywhere near 10 real seconds, let alone 60. A request that HANGS
# until timeout (rather than completing slow) is only possible when
# the network is genuinely cut, not merely delayed -- so a p95 this
# high is itself an unambiguous partition signal, independent of
# whether the throughput check happened to catch the same episode.
# 10000ms sits with a large real margin above what 500ms+jitter could
# ever organically produce and a large real margin below the 60000ms
# timeout artifact itself.
NETWORK_PARTITION_LATENCY_SATURATION_MS = 10000

# See oom_sticky_query's own docstring for the full reasoning -- bounds
# the sticky OOMKilled gauge to the specific container instance that was
# actually restarted (container_start_time_seconds), not to unrelated
# pod churn on the same target. 20 minutes is a reasoned starting value
# (comfortably covers retries + a reset-rollout + --shuffle queueing
# stacking worse than the 6-minute case that just failed), not yet
# re-derived from a real measured worst-case batch-run delay -- revisit
# and tighten once that's measured, per Kimi's own recommendation not to
# re-guess a bigger number again without real data behind it.
OOM_STICKY_MAX_CONTAINER_AGE_S = 1200

# user's container_cpu_cfs_throttled_periods_total is a non-resetting
# counter, already nonzero under light idle traffic (553 at measurement
# time, 2026-07-24) -- but increase() over a window is well-defined
# regardless of the counter's absolute starting value, so (unlike a
# gauge) no per-episode baseline is needed here, same reasoning
# injector.py's own verification uses. Real measured margin: baseline
# increase over 60s = 0.0; during-stress increase over 30s ~= 300
# (~600 projected per 60s). 100 sits with a large real margin above
# baseline noise and well below a genuine stressed level.
CPU_THROTTLE_INCREASE_THRESHOLD = 100

# under-provisioned-replicas: catalogue exposes no native request-
# latency metric of its own, and traffic_gen's organic load is far
# below what's needed to trigger real degradation (confirmed via
# measurement, 2026-07-24) -- so unlike every other signal in this
# file, this one is an ACTIVE PROBE, not a passive Prometheus read.
# Same real, validated parameters as injector.py's own mechanism-
# assertion (20 VUs, 20s, 200ms threshold -- see injector.py's
# UNDER_PROVISIONED_* constants for the full real-measurement history).
# Called ONLY as a fallback from diagnose()/handle() -- see those
# functions -- after cheaper signals have already been checked, so
# this never fires during an active oom episode on the same target.
K6_IMAGE = "grafana/k6:latest"
UNDER_PROVISIONED_PROBE_VUS = 20
UNDER_PROVISIONED_PROBE_DURATION_S = 20
# Lowered 200 -> 190, 2026-08-01: real root cause found for the one
# wrong diagnosis in 62 scored episodes -- a real k6 probe measured
# 199.8ms (0.2ms under the old cutoff) on a genuine under-provisioned-
# replicas episode, just 6ms below the lowest-ever correctly-diagnosed
# value (205.83ms) across the full real history. 190 gives real margin
# over the observed miss while staying comfortably under that real
# correct-cluster floor.
UNDER_PROVISIONED_PROBE_THRESHOLD_MS = 190
# Matches injector.py's own MAX_INJECT_ATTEMPTS pattern for this exact
# same probe mechanism -- see probe_catalogue_capacity's docstring for
# the real bug this fixes.
PROBE_MAX_ATTEMPTS = 3


def _parse_k6_p95_ms(stdout_text: str) -> float | None:
    """Parses the real p95 value directly from k6's own end-of-run
    text summary -- `--no-summary`/`--summary-export=/dev/stdout`
    isn't a real flag on this k6 image (confirmed via a real failed
    run, 2026-07-24: `unknown flag: --no-summary`). Duplicated from
    injector.py's identical helper, matching this project's
    established convention of keeping each file self-contained."""
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


def probe_catalogue_capacity(namespace: str) -> float | None:
    """Fires a real k6 burst directly against catalogue and returns
    its real p95, parsed from k6's own text summary. This is a
    deliberate perturbation, not a read-only observation -- a first
    for this file, worth knowing before calling it casually (see the
    real cost/interference tradeoffs discussed before this class was
    built: real cluster load per call, and a real contamination-
    surface risk with connection-pool-exhaustion, which also floods
    catalogue-db through catalogue).

    REAL BUG FIXED (2026-07-27, confirmed via 2 real Phase D
    under-provisioned-replicas misdiagnoses): a single k6 burst
    occasionally fails to produce a valid p95 (returns None) -- an
    already-known, already-observed flakiness in this exact mechanism
    (injector.py's own retry loop routinely logs "p95=Nonems, below
    200ms threshold, retrying" for this same probe). This function had
    NO retry at all -- confirmed directly via both misdiagnosed
    episodes' real tool_output snapshots (catalogue_probe_p95_ms: null
    both times) -- one flaky call at diagnosis time became a missed
    diagnosis, with no recovery. Fixed by retrying up to
    PROBE_MAX_ATTEMPTS times, matching injector.py's own already-proven
    pattern for this same mechanism -- only returns None if every
    attempt genuinely fails."""
    for attempt in range(1, PROBE_MAX_ATTEMPTS + 1):
        pod_name = f"wardence-catprobe-{uuid.uuid4().hex[:8]}"
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
            continue
        if result.returncode != 0:
            continue
        p95 = _parse_k6_p95_ms(result.stdout)
        if p95 is not None:
            return p95
    return None


def call_dl_detector(service: str) -> dict | None:
    """Calls detector_service.py's real POST /detect for one of the 5
    services with real log-based coverage (DeepLog/HMM/SPC, see
    DL_DETECTOR_SERVICES above) -- a generic anomaly flag, NOT a
    per-class classifier (it can say "something's off on this service,"
    never which specific fault class). Same error-handling shape as
    probe_catalogue_capacity: any failure (service unreachable, timeout,
    a real "insufficient_data" response because too few live events were
    pulled) returns None rather than raising, so a flaky/cold detector
    service degrades diagnosis back to Prometheus-only, never crashes
    it."""
    try:
        resp = requests.post(
            f"{DETECTOR_URL}/detect", json={"service": service}, timeout=15
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("status") == "insufficient_data":
        return None
    return data

app = FastAPI()


class DiagnoseRequest(BaseModel):
    target: str
    namespace: str
    # Real evidence-freezing timestamp (RFC3339), optional -- None
    # (every existing caller) preserves today's live "now" query
    # behavior exactly. See _prom_instant_query's docstring for why
    # this exists.
    snapshot_at: str | None = None
    # Real per-episode floor reading (KB), memory-leak only. Unlike
    # snapshot_at (a single frozen "now" instant), this diagnosis needs a
    # SECOND, earlier anchor -- the target pod's own heap_used floor
    # captured by injector.py at settle time (t0+35s) -- so the diagnosis
    # query can compute a self-referential rise-over-baseline rather than
    # an absolute threshold (which measurement showed can't work across a
    # freshly-booted clone vs. a long-running production pod). Optional,
    # same precedent snapshot_at itself set: every existing caller/class
    # is unaffected, zero regression risk.
    baseline_heap_kb: float | None = None


def _prom_instant_query(query: str, snapshot_at: str | None = None) -> list:
    """Real evidence-freezing mechanism (the 'snapshot_at' design locked
    in an earlier Operator design session, wired for real here after a
    genuine live-tested false-negative confirmed it was missing --
    crash-loop's real restart events aged out of its own [3m]
    increase() window during a live-triggered episode whose real
    total elapsed time from injection to diagnosis exceeded 8 minutes
    (a genuinely extended hold + the 5-minute abandonment ceiling
    stacking up), even though the fault genuinely happened and the
    injector's own sticky-signal verification confirmed it in real
    time. Passing Prometheus's real `time=` instant-query parameter
    (RFC3339, same format datetime.isoformat() already produces
    everywhere else in this codebase) shifts EVERY windowed function's
    right edge (max_over_time/min_over_time/increase/subqueries) to
    that fixed point instead of live "now" -- exactly reproducing what
    the query would have seen if it had run right when evidence was
    genuinely ready, regardless of how much real wall-clock time
    passes afterward waiting for a manual resolve click or an
    abandonment ceiling to fire. snapshot_at=None (every existing
    caller, e.g. every batch-run episode) preserves today's exact
    live-query behavior -- purely additive, not a behavior change for
    anything that doesn't opt in."""
    params = {"query": query}
    if snapshot_at is not None:
        params["time"] = snapshot_at
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]["result"]


def query_prometheus(
    target: str, namespace: str, snapshot_at: str | None = None, baseline_heap_kb: float | None = None
) -> dict:
    """Tool: check whether a matching container is crash-looping, was OOM-killed,
    was evicted (disk-full), or is seeing elevated request latency
    (network-latency, via p95_latency_ms -- k6's own observed latency
    for URLs matching `target`, not a Chaos Mesh signal).

    CrashLoopBackOff is a transient snapshot state -- kubelet only reports
    it while actively waiting before the next restart attempt, and as
    backoff delay grows the container spends more time transiently
    Running between attempts. Checking only the current state snapshot
    misses crash loops that are genuinely happening but caught mid-Running.

    Instead this asks "did this crash loop recently" as a fact: currently
    in CrashLoopBackOff, OR restarted at all in the last 3 minutes. That
    restart-increase signal alone can't reliably distinguish crash-loop
    from OOM (an OOM kill also increases the restart count on the SAME
    pod), so OOM is checked as its own explicit signal via
    last_terminated_reason and given priority in stub_diagnose below.

    disk-full is structurally different from both: an ephemeral-storage
    breach doesn't restart the container in place, kubelet EVICTS the
    whole pod and a brand-new pod object replaces it. A fresh pod's
    restart counter starts at 0 with no prior data point, so it's
    unlikely (not impossible) to false-trigger the crash-loop
    restart-increase catch-all, but Evicted is still checked and given
    priority over crash-loop for the same reason OOM is.

    Both oom_query and evicted_query are TIME-BOUNDED, not just
    point-in-time state checks -- found the hard way during mixed-class
    validation. last_terminated_reason and status_reason are gauges
    that reflect the MOST RECENT event on a pod/container with no
    expiry: an evicted pod's terminal object lingers (Kubernetes
    doesn't garbage-collect it immediately) and keeps reporting
    reason="Evicted" indefinitely, and an OOM-killed container's
    last_terminated_reason stays "OOMKilled" until that same
    pod/container terminates again for a different reason. Without a
    time bound, ANY later episode on the same target -- even a
    no-fault control run minutes later -- would false-positive on a
    long-resolved fault. oom_query is gated on a restart count increase
    in the last 3 minutes (mirrors crash_query's own pattern).

    evicted_query went through TWO wrong fixes before landing here --
    both plausible, both empirically disproven, worth recording so the
    next person doesn't re-try either:
      1. FIRST gated on the evicted pod's own CREATION time
         (kube_pod_created < 3 min old) -- wrong: a pod can run healthy
         for a long time before finally being evicted, so its creation
         time can be old even though the eviction just happened,
         incorrectly excluding a genuine fresh eviction (caused
         disk-full to false-negative 5/5 in a mixed validation run).
      2. THEN gated on kube_pod_deletion_timestamp, on Kimi's confirmed
         claim that kubelet's eviction manager sets it at/near eviction
         time. Also wrong, empirically: queried this metric directly
         against genuinely-just-evicted queue-master pods and it
         returned NO results at all, even though kube_pod_status_reason
         clearly showed reason="Evicted" for them. On this cluster,
         eviction apparently doesn't reliably populate
         deletion_timestamp -- evicted pods just sit in Failed phase
         indefinitely without it (matches the hours-long-lingering
         evicted pods observed all session). Even a confidently-stated,
         specific external claim needed the same "verify against the
         real cluster" discipline as everything else here.
      3. Landed on: bound the Evicted signal by whether the CURRENT
         Running pod for this target was created recently, instead of
         trusting anything about the evicted pod's own metadata. A
         fresh healthy replacement pod existing is itself strong,
         directly-verifiable evidence a churn (evict + recreate) just
         happened -- confirmed empirically (the replacement pod's
         creationTimestamp landed inside the actual injection window
         during a real disk-full episode). Two separate existence
         checks (can't join across different pod names/objects the way
         same-pod-two-metrics joins work elsewhere in this file),
         combined at the code level below.
    """
    # [6m], not [3m] -- REAL BUG FIXED (2026-07-27, confirmed via a
    # historical Prometheus query at a real Phase D oom misdiagnosis's
    # exact scored_at timestamp): last_terminated_reason=="OOMKilled"
    # correctly showed 1 for the real OOM-killed pod, but that SAME
    # pod's own increase(restarts_total[3m]) showed 0 at that exact
    # instant -- the and-join between the two metrics failed even
    # though the OOM genuinely happened, because the real restart event
    # landed more than 3 minutes before diagnosis time. This episode's
    # own log showed "catalogue's memory limit is 400Mi... resetting
    # before injecting" -- the same "reset-rollout + retry attempts push
    # the real event later than a narrow window assumes" shape already
    # confirmed and fixed for cpu-throttling's [2m]->[6m] widen, just
    # manifesting here as a cross-metric join failure instead of
    # [0]-indexing. Widened to match the same real margin.
    # Real bug found live (2026-07-31): pod=~"{target}.*" over-matches --
    # "catalogue" also matches "catalogue-db" (a completely different
    # service, independently restarted by e.g. connection-pool-
    # exhaustion's own mechanism against catalogue-db specifically).
    # Confirmed live via a direct Prometheus query: catalogue-db really
    # does show up under a bare "catalogue.*" match. Sock Shop has
    # several <service>/<service>-db pairs (orders/orders-db,
    # carts/carts-db, user/user-db), so this wasn't catalogue-specific
    # exposure, just the one we happened to hit. Fixed by anchoring to
    # the real k8s pod-name shape (<name>-<replicaset-hash>-<pod-suffix>,
    # exactly two more hyphen-separated segments, nothing else in
    # between) -- verified this correctly excludes "catalogue-db-..."
    # (3 segments after "catalogue-") while still matching
    # "catalogue-c5cb8d777-5klwz" (exactly 2).
    _pod_re = f'{target}-[^-]+-[^-]+$'
    oom_query = (
        f'(kube_pod_container_status_last_terminated_reason{{namespace="{namespace}", '
        f'pod=~"{_pod_re}", reason="OOMKilled"}} == 1) '
        f'and on(namespace, pod) (increase(kube_pod_container_status_restarts_total'
        f'{{namespace="{namespace}", pod=~"{_pod_re}"}}[6m]) > 0)'
    )
    # Real bug found live (2026-07-31, overnight batch): the [6m] widen
    # above (2026-07-27) fixed the common case, but the restart-count
    # JOIN is still time-bound even though the terminated-reason gauge
    # itself is sticky -- a worse-than-6-minute delay (e.g. a reset-
    # rollout stacked with a retry, during a long interleaved --shuffle
    # run) still fails the join even though the gauge alone would still
    # show OOMKilled==1.
    #
    # FIRST fix attempt (reused evicted_query's own "trust the sticky
    # gauge only if a fresh replacement pod exists" technique) was
    # caught and rejected before shipping, via Kimi review (one-off,
    # same chat as reviews 12-15, not archived): eviction genuinely
    # replaces the pod object (new name), so "a fresh replacement pod
    # exists" is causally tied to eviction. OOM does NOT replace the
    # pod -- kubelet restarts the SAME pod object in place. Since oom
    # and under-provisioned-replicas share the same target (catalogue),
    # "any recent replacement pod" would have let an unrelated
    # under-provisioned-replicas episode's own pod churn wrongly
    # validate a stale OOM reading.
    #
    # REAL fix: bind the sticky gauge to container_start_time_seconds
    # instead -- a real cAdvisor metric (confirmed present on this
    # cluster via a direct query, 2026-07-31), updated whenever THIS
    # SPECIFIC container restarts, in place or not. Causally tied to
    # the actual OOM-restart event, not to unrelated churn on the same
    # target. OOM_STICKY_MAX_CONTAINER_AGE_S (1200s = 20min) is a
    # starting value, not yet re-derived from a real measured worst-case
    # batch-run delay -- revisit and tighten once that's measured, per
    # Kimi's own recommendation not to re-guess a bigger number again.
    oom_sticky_query = (
        f'kube_pod_container_status_last_terminated_reason{{namespace="{namespace}", '
        f'pod=~"{_pod_re}", reason="OOMKilled"}} == 1 '
        f'and on(namespace, pod, container) ((time() - container_start_time_seconds'
        f'{{namespace="{namespace}", pod=~"{_pod_re}"}}) < OOM_STICKY_MAX_CONTAINER_AGE_S)'
    )
    evicted_query = (
        f'kube_pod_status_reason{{namespace="{namespace}", '
        f'pod=~"{_pod_re}", reason="Evicted"}} == 1'
    )
    recent_running_pod_query = (
        f'(kube_pod_status_phase{{namespace="{namespace}", pod=~"{_pod_re}", phase="Running"}} == 1) '
        f'and on(namespace, pod) ((time() - kube_pod_created'
        f'{{namespace="{namespace}", pod=~"{_pod_re}"}}) < 180)'
    )
    crash_query = (
        f'(kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"{_pod_re}", reason="CrashLoopBackOff"}} == 1) '
        f'or (increase(kube_pod_container_status_restarts_total{{namespace="{namespace}", '
        f'pod=~"{_pod_re}"}}[3m]) > 0)'
    )

    oom_result = _prom_instant_query(oom_query, snapshot_at)
    oom_sticky_result = _prom_instant_query(oom_sticky_query, snapshot_at)
    evicted_result = _prom_instant_query(evicted_query, snapshot_at)
    has_recent_replacement_pod = len(_prom_instant_query(recent_running_pod_query, snapshot_at)) > 0
    crash_result = _prom_instant_query(crash_query, snapshot_at)

    # network-latency: no pod-restart signal at all (a network delay
    # doesn't touch the container), so this checks the traffic
    # generator's own observed p95 request latency instead
    # (k6_http_req_duration_p95 via k6's experimental-prometheus-rw
    # output -- NOT trusted for injector.py's ground-truth verification
    # anymore, see injector.py's ABANDONED note, but still useful here).
    # Threshold intentionally lower than the injector's old
    # baseline-relative bar: the agent has no baseline to compare to,
    # only a fixed absolute threshold.
    #
    # max_over_time, not an instant query -- found the hard way
    # (2026-07-21): a genuine network-latency episode still showed
    # p95=2.0-2.4s for almost its entire window, but dipped to
    # 144-185ms for one ~30s stretch right in the middle (confirmed via
    # a Prometheus range query) -- an instant query landing in that
    # dip caused a real false-negative ('no anomaly detected' on an
    # actual fault). Same "time-bounded, not point-in-time" fix already
    # applied to oom_query/evicted_query above, for the same reason:
    # this metric is genuinely volatile moment-to-moment, not just slow
    # to decay after a fault ends.
    # Found the hard way (2026-07-21), via a REAL cross-contamination
    # bug, not just the theoretical risk flagged earlier: this
    # threshold (like the memory-leak and connection-pool ones below)
    # was calibrated for ONE specific service's known baseline/stressed
    # levels (orders, here), not a universal constant. Without gating
    # on target, diagnosing a completely different service's episode
    # would still run this query against THAT service's own metrics --
    # confirmed to actually happen: catalogue-db's own memory (150
    # forked mysql client processes during a connection-pool-exhaustion
    # episode) crossed the memory-leak threshold calibrated for
    # shipping, misdiagnosing 3 real connection-pool-exhaustion
    # episodes as memory-leak in a row. Every arbitrary-threshold check
    # below is now gated on target actually being its own real service
    # -- unlike oom_query/evicted_query/crash_query above, which are
    # genuine "did X happen to whatever pod this is" checks, valid for
    # any target by construction.
    if target == "orders":
        latency_query = f'max_over_time(k6_http_req_duration_p95{{url=~".*{target}.*"}}[2m])'
        latency_result = _prom_instant_query(latency_query, snapshot_at)
        # k6's exported value is in SECONDS -- confirmed empirically
        # (2026-07-21), see injector.py's _orders_p95_latency_ms docstring.
        p95_latency_ms = max((float(e["value"][1]) * 1000 for e in latency_result), default=None)
    else:
        p95_latency_ms = None

    # network-partition: orders' own combined network throughput
    # (cAdvisor's container_network_transmit/receive_bytes_total, BOTH
    # directions -- confirmed empirically, 2026-07-24, that a
    # `direction: to` partition drops both together, not just egress as
    # first assumed; see measure_network_partition_direction_check.py's
    # real result). Deliberately NOT k6_http_req_failed -- confirmed
    # unusable for this class: front-end's own POST /orders call
    # (api/orders/index.js, the `request` library) has no client-side
    # timeout at all, so it hangs indefinitely rather than ever
    # producing an observable k6 failure while the partition holds
    # (checked against front-end's real upstream source, not assumed).
    #
    # FIRST attempt used a plain rate(...[1m]) -- WRONG, found via a
    # real diagnosis miss (2026-07-24): by the time this endpoint gets
    # called (injector-end + 35s settle), a 1m lookback window only
    # overlaps the actual 60s fault for ~25s, averaged together with
    # ~35s of already-recovered normal traffic -- diluting the reading
    # well above the 200 bytes/s threshold and letting diagnosis fall
    # through to the p95_latency_ms check below, which then FALSE-
    # POSITIVED as network-latency (a request that hung during the
    # partition and only completed once it cleared reports as one very
    # slow request to k6 -- indistinguishable from real latency using
    # that signal alone). Same root lesson as memory-leak's un-sticky
    # signal (reverts almost immediately once the fault ends), but
    # inverted: memory-leak needed max_over_time to catch a since-
    # reverted SPIKE; this needs the MINIMUM over a window to catch a
    # since-reverted DROP. Fixed with a PromQL subquery --
    # min_over_time(rate(...)[30s])[2m:30s] samples 30s-windowed
    # throughput every 30s across the last 2 minutes and takes the
    # minimum, correctly catching the nearzero reading from DURING the
    # fault even though live throughput has since recovered. 2m
    # comfortably covers back past t0 given diagnosis happens ~95s
    # after fault start (60s fault + 35s settle).
    if target == "orders":
        throughput_query = (
            f'min_over_time((sum(rate(container_network_transmit_bytes_total{{namespace="{namespace}", '
            f'pod=~"{target}-[^-]+-[^-]+$"}}[30s])))[2m:30s]) + '
            f'min_over_time((sum(rate(container_network_receive_bytes_total{{namespace="{namespace}", '
            f'pod=~"{target}-[^-]+-[^-]+$"}}[30s])))[2m:30s])'
        )
        throughput_result = _prom_instant_query(throughput_query, snapshot_at)
        combined_throughput_bps = (
            float(throughput_result[0]["value"][1]) if throughput_result else None
        )
    else:
        combined_throughput_bps = None

    # init-failure: payment's own kube_pod_status_ready -- confirmed
    # empirically (2026-07-24, see injector.py's PAYMENT_READINESS_PATH_FAULT
    # docstring) that a broken readinessProbe leaves the OLD, healthy
    # pod serving traffic untouched (Ready=true, unaffected) while a
    # NEW pod gets stuck permanently Ready=false -- so a query for ANY
    # payment pod reporting Ready=false only ever matches the
    # genuinely-stuck new pod, no restart-count gate needed (a pod
    # stuck this way never restarts, so it can't collide with
    # crash-loop's own restart-increase signal).
    #
    # FIRST attempt used a plain instant query -- WRONG, found via a
    # real diagnosis miss (2026-07-24, same root mistake as
    # network-partition's own first attempt, re-made despite having
    # just learned it): by the time this endpoint gets called
    # (injector-end + settle), the fault has ALREADY been reverted
    # (injector.py's _restore_init_failure runs immediately after the
    # hold, before the episode is even recorded) -- the broken pod is
    # very likely already scaled back to 0 by diagnosis time, so an
    # instant snapshot sees nothing. Fixed with max_over_time(...[2m]),
    # matching memory-leak/connection-pool's own "catch a since-
    # reverted state" pattern -- confirmed via a direct query that the
    # broken pod's Ready=false DID register within the window, just not
    # at the exact instant of a plain snapshot query.
    if target == "payment":
        ready_false_query = (
            f'max_over_time(kube_pod_status_ready{{namespace="{namespace}", pod=~"{target}-[^-]+-[^-]+$", '
            f'condition="false"}}[2m]) == 1'
        )
        payment_stuck_not_ready = len(_prom_instant_query(ready_false_query, snapshot_at)) > 0
    else:
        payment_stuck_not_ready = False

    # bad-rollout: front-end's own kube_pod_container_status_waiting_reason
    # -- confirmed empirically (2026-07-25, see injector.py's
    # FRONT_END_IMAGE_FAULT docstring) that a bad image tag leaves the
    # OLD, healthy pod serving traffic untouched (same "old stays up,
    # new broken one never comes online" pattern as init-failure) while
    # a NEW pod gets stuck ImagePullBackOff/ErrImagePull -- so a query
    # for ANY front-end pod reporting that waiting reason only ever
    # matches the genuinely-stuck new pod. max_over_time, not an
    # instant query -- unlike some other classes, this fault DOESN'T
    # self-revert before diagnosis time (it's a standing bad config,
    # same shape as under-provisioned-replicas), so an instant query
    # would likely also work, but the windowed version is used
    # proactively for the same scrape-timing safety margin every other
    # class's signal already gets.
    if target == "front-end":
        image_pull_failing_query = (
            f'max_over_time(kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
            f'pod=~"{target}-[^-]+-[^-]+$", reason=~"ImagePullBackOff|ErrImagePull"}}[2m]) == 1'
        )
        front_end_image_pull_failing = len(_prom_instant_query(image_pull_failing_query, snapshot_at)) > 0
    else:
        front_end_image_pull_failing = False

    # session-cart-failure: session-db's own kube_deployment_status_replicas_available
    # -- a genuinely new signal type, distinct from every other class's
    # signature (target pod exists but is broken in some way): here the
    # target has ZERO available replicas at all. Confirmed empirically
    # (2026-07-24, see injector.py's _inject_and_verify_session_cart_failure
    # docstring) that scaling to 0 -- not a process kill/restart -- is
    # deliberate specifically to avoid producing the same restart-
    # increase/CrashLoopBackOff signature crash-loop already owns; the
    # generic oom_query/evicted_query/crash_query checks above correctly
    # return empty here regardless (no pod exists to match at all), so
    # this check never needs its own extra exclusion logic.
    if target == "session-db":
        # min_over_time, not an instant/max query -- the fault is a DROP
        # to 0, and by diagnosis time (fault-end + settle) it's already
        # been restored to 1, so we need to catch the since-reverted LOW
        # point within the window (same "un-sticky signal" lesson as
        # network-partition/init-failure, inverted for a drop rather
        # than a spike).
        min_replicas_query = (
            f'min_over_time(kube_deployment_status_replicas_available'
            f'{{namespace="{namespace}", deployment="{target}"}}[2m])'
        )
        min_replicas_result = _prom_instant_query(min_replicas_query, snapshot_at)
        session_db_replicas_hit_zero = (
            len(min_replicas_result) > 0 and float(min_replicas_result[0]["value"][1]) == 0
        )
    else:
        session_db_replicas_hit_zero = False

    # oom: catalogue's own real peak working-set memory, added 2026-08-03.
    # Real, deliberate scope narrowing 2026-08-21: this dict used to also
    # carry "shipping": "3m" for memory-leak's OLD container-stressor
    # diagnosis -- removed along with that mechanism (see heap_rise_kb
    # below, its real replacement). oom's real production auto-fix
    # (patch_memory_limit) always used a fixed deterministic "400Mi"
    # constant and never needed this value, so the gap was invisible until
    # action_proposer.py's LLM-driven magnitude sizing started depending on
    # a real peak_memory_mib for oom too -- oom silently always saw None
    # here, tripping the "metric missing, fall back to restart_deployment"
    # instruction on every single episode. [6m] window because oom's real
    # verification ceiling is 200s (see injector.py's redesigned
    # _inject_and_verify_oom) -- same reasoning as cpu-throttling's own
    # 2m->6m widening for a class whose real fault window can run long.
    _PEAK_MEMORY_TARGETS = {"catalogue": "6m"}
    if target in _PEAK_MEMORY_TARGETS:
        memory_query = (
            f'max_over_time(container_memory_working_set_bytes{{namespace="{namespace}", '
            f'pod=~"{target}-[^-]+-[^-]+$", container="{target}"}}[{_PEAK_MEMORY_TARGETS[target]}])'
        )
        memory_result = _prom_instant_query(memory_query, snapshot_at)
        peak_memory_mib = max(
            (float(e["value"][1]) / (1024 * 1024) for e in memory_result), default=None
        )
    else:
        peak_memory_mib = None

    # memory-leak: real production replacement (2026-08-21 session, LOCKED
    # design -- see wardence_buildlog.md for the full measurement chain),
    # REPLACES the old container_memory_working_set_bytes/MEMORY_LEAK_
    # THRESHOLD_MIB absolute check above. Self-referential rise-over-
    # baseline, not an absolute threshold -- real measurement proved a
    # fixed number can't work across a freshly-booted JVM and a long-
    # running production one (their real absolute floors aren't
    # comparable). `heap_used` is shipping's own real, now-scraped JVM
    # heap metric (KB, classic-Actuator shape, confirmed present and
    # scraped via add_shipping_metrics_scrape.sh). `baseline_heap_kb` is
    # injector.py's own real per-episode floor reading (min_over_time,
    # captured at settle time BEFORE the leak agent starts ramping,
    # persisted to episodes.memory_leak_baseline_heap_kb) -- threaded
    # through from the caller (None for every non-memory-leak class/every
    # caller not yet updated to pass it, same zero-regression precedent
    # snapshot_at itself set). Real bug found and fixed 2026-08-21: the
    # original 60s window was sized ONLY for the live-trigger case (where
    # snapshot_at freezes evaluation to a moment DURING the active hold),
    # comfortably spanning the real governor release/recover cadence
    # (~8-20s cycles) so a single transient GC dip doesn't false-negative.
    # It was never sized for CLI/batch-mode diagnosis (no snapshot_at,
    # "now" is used), where the real fault has ALREADY fully self-reverted
    # (settle+180s hold+release all complete) before scoring ever runs --
    # confirmed via a real historical Prometheus range query on a genuine
    # miss (heap_rise_kb=16725 reported, real true peak was +36MB three
    # minutes earlier, 60s window only reached back to a post-peak
    # governor dip). Widened to 300s: comfortably covers the full real
    # 180s hold regardless of where the peak lands within it, plus the
    # standard 35s settle-wait margin this project uses elsewhere, plus
    # real headroom for scrape/query lag -- generous margin over the
    # minimum, matching the same "widen conservatively" precedent already
    # used for oom's window (3m->6m) for the identical failure shape.
    if target == "shipping" and baseline_heap_kb is not None:
        heap_query = f'max_over_time(heap_used{{namespace="{namespace}", pod=~"{target}-[^-]+-[^-]+$"}}[300s])'
        heap_result = _prom_instant_query(heap_query, snapshot_at)
        peak_heap_kb = max((float(e["value"][1]) for e in heap_result), default=None)
        heap_rise_kb = (peak_heap_kb - baseline_heap_kb) if peak_heap_kb is not None else None
    else:
        heap_rise_kb = None

    # connection-pool-exhaustion: mysql_global_status_threads_connected,
    # via a mysqld_exporter sidecar deployed specifically for this class
    # (2026-07-21) -- Sock Shop ships no MySQL exporter by default,
    # confirmed empirically (an initial query for any mysql_* metric
    # came back completely empty). Same live-gauge staleness concern as
    # memory-leak's working-set metric applies here too (the flood gets
    # cleaned up once injector.py verifies it, so the elevated count
    # reverts almost immediately) -- max_over_time applied from the
    # start this time, not added after a bug, having already learned
    # that lesson twice this session.
    if target == "catalogue-db":
        threads_query = (
            f'max_over_time(mysql_global_status_threads_connected{{namespace="{namespace}", '
            f'pod=~"{target}-[^-]+-[^-]+$"}}[3m])'
        )
        threads_result = _prom_instant_query(threads_query, snapshot_at)
        peak_threads_connected = max(
            (float(e["value"][1]) for e in threads_result), default=None
        )
    else:
        peak_threads_connected = None

    # cpu-throttling: user's own container_cpu_cfs_throttled_periods_total,
    # via increase() over a window wide enough to cover the fault's full
    # 60s duration plus the settle gap -- NOT max_over_time (that's for
    # gauges; this is a monotonic counter, so increase() over the window
    # directly gives the real delta during that period regardless of the
    # counter's absolute starting value, avoiding the "already nonzero at
    # idle" trap a raw/instant read of this metric would fall into).
    #
    # WIDENED 2m -> 6m (2026-07-25, real Phase D false negative, episode
    # bc54bd30, pair 8): this is a live RATE, not a sticky historical
    # fact like oom's last_terminated_reason -- once the stressor stops
    # accruing new throttled periods, increase() over a short window
    # genuinely decays back toward 0 within minutes, even though the
    # counter's absolute value never resets. A 2m window is fine for the
    # common single-attempt case (~97s observed elapsed_s end-to-end),
    # but this class is also one of the few (along with oom) whose real,
    # successful fix (patch_cpu_limit) permanently raises user's CPU
    # limit -- nothing else reverts it -- so _ensure_cpu_throttle_baseline
    # must patch it back and wait out a real rollout (kubectl rollout
    # status) before the NEXT injection can even start. That reset,
    # stacked with a retry attempt, pushed one real episode's actual
    # last-throttling-activity well outside a 2m lookback by diagnosis
    # time (elapsed_s=421.6 vs. the normal ~97s, tool_output showed a
    # clean cpu_throttle_periods_increase=0.0 -- the fault genuinely
    # landed, per the injector's own mechanism assertion, but had fully
    # aged out of view by the time diagnosis ran). Checked every other
    # baseline-reset class before picking this fix: under-provisioned-
    # replicas re-probes live (no window to age out of); bad-rollout's
    # signal doesn't self-revert at all; init-failure/session-cart-
    # failure revert on every attempt (successful or not), so their own
    # reset-rollout never becomes a real per-episode cost the way
    # oom's/cpu-throttling's do. oom is unaffected by this specific bug
    # despite sharing the same reset-rollout cost, because its own
    # signal (last_terminated_reason) is sticky, not a decaying rate.
    # 6m gives real margin over a 180s rollout wait + a failed 95s retry
    # + settle, without needing to guess an even larger number.
    if target == "user":
        throttle_query = (
            f'increase(container_cpu_cfs_throttled_periods_total{{namespace="{namespace}", '
            f'pod=~"{target}-[^-]+-[^-]+$", container="{target}"}}[6m])'
        )
        throttle_result = _prom_instant_query(throttle_query, snapshot_at)
        # SECOND real bug found the same session as the 2m->6m window fix
        # (2026-07-26, direct repro): during a CPU-limit reset rollout
        # (_ensure_cpu_throttle_baseline), the OLD and NEW `user` pods
        # both still match pod=~"user-.*" and both still report to
        # Prometheus for several minutes -- confirmed via a direct
        # historical query at the exact diagnosis timestamp, which
        # returned TWO series: the old, already-dead pod at increase=0,
        # and the new pod (the real fault target) at increase=626.28.
        # The previous code took throttle_result[0] -- the FIRST entry
        # Prometheus happened to return, which silently was the dead
        # pod's 0, discarding the real signal sitting in result[1].
        # Same root shape as verifier.py's earlier user/user-db prefix-
        # regex collision, but here it's the SAME target's own old and
        # new pod overlapping during a rollout, not a different service.
        # Fixed by taking max() across every matched series, same
        # pattern already used for peak_memory_mib/peak_threads_connected
        # above -- correct because a genuinely stale/dead pod can only
        # ever report 0 or a low leftover value, never a higher one than
        # the real active pod during a real fault.
        cpu_throttle_periods_increase = max(
            (float(e["value"][1]) for e in throttle_result), default=None
        )
    else:
        cpu_throttle_periods_increase = None

    oom_pods = [entry["metric"].get("pod") for entry in oom_result]
    if not oom_pods:
        # Fast join found nothing (real delay exceeded the [6m] restart-
        # count window) -- fall back to the sticky-gauge-bound-by-
        # container-start-time query, which is already causally scoped
        # to the actual restarted container (see oom_sticky_query's own
        # docstring) -- no separate has_recent_replacement_pod trust
        # condition needed here, unlike evicted_pods below.
        oom_pods = [entry["metric"].get("pod") for entry in oom_sticky_result]
    # Only trust the Evicted signal if there's ALSO a freshly-created
    # Running pod for this target -- see query docstring above for why
    # the evicted pod's own metadata can't be trusted for recency.
    evicted_pods_raw = [entry["metric"].get("pod") for entry in evicted_result]
    evicted_pods = evicted_pods_raw if (evicted_pods_raw and has_recent_replacement_pod) else []
    crashlooping_pods = [entry["metric"].get("pod") for entry in crash_result]

    # current_replicas: the real, live desired replica count
    # (kube_deployment_spec_replicas -- same DESIRED, not READY,
    # semantics as constraint_checks.py's own kubectl .spec.replicas
    # read, so the LLM's proposal and the live safety check reason
    # about the same number). Added 2026-08-06 -- under-provisioned-
    # replicas' tool_output never carried this field at all, so the LLM
    # proposing scale_deployment had no real current count to reason
    # "propose something greater than" from, and kept echoing the
    # SYSTEM_PROMPT's own few-shot example value (3) instead, which
    # always failed constraint_checks.check_safe's "> current" bound.
    # Frozen at the same snapshot_at as every other field above for
    # consistency (one diagnosis-time tool_output = one consistent point
    # in time) -- doesn't compromise action-sizing safety: the actual
    # patch-time safety check (constraint_checks.py's own live kubectl
    # .spec.replicas read) re-verifies the REAL current value
    # independently right before dispatch, this field is only ever a
    # reasoning aid for the diagnoser's proposed magnitude.
    replicas_query = f'kube_deployment_spec_replicas{{namespace="{namespace}", deployment="{target}"}}'
    replicas_result = _prom_instant_query(replicas_query, snapshot_at)
    current_replicas = int(float(replicas_result[0]["value"][1])) if replicas_result else None

    return {
        "oom_pods": oom_pods,
        "evicted_pods": evicted_pods,
        "crashlooping_pods": crashlooping_pods,
        "p95_latency_ms": p95_latency_ms,
        "combined_throughput_bps": combined_throughput_bps,
        "payment_stuck_not_ready": payment_stuck_not_ready,
        "session_db_replicas_hit_zero": session_db_replicas_hit_zero,
        "peak_memory_mib": peak_memory_mib,
        "peak_threads_connected": peak_threads_connected,
        "cpu_throttle_periods_increase": cpu_throttle_periods_increase,
        "front_end_image_pull_failing": front_end_image_pull_failing,
        "current_replicas": current_replicas,
        "heap_rise_kb": heap_rise_kb,
    }


def stub_diagnose(tool_output: dict) -> dict:
    """
    Placeholder for the LLM reasoning step. Hardcoded rule standing in
    for the ReAct loop. OOM, Evicted, and bad-rollout's own image-pull
    signal are all checked before crash-loop: each can otherwise get
    swept up by the broader restart-increase catch-all the crash-loop
    signal watches. Replace this function's body with the real LLM
    call; keep the same tool-output-in, diagnosis-out shape.
    """
    oom_pods = tool_output["oom_pods"]
    evicted_pods = tool_output["evicted_pods"]
    crashlooping_pods = tool_output["crashlooping_pods"]
    p95_latency_ms = tool_output["p95_latency_ms"]
    combined_throughput_bps = tool_output["combined_throughput_bps"]
    payment_stuck_not_ready = tool_output["payment_stuck_not_ready"]
    session_db_replicas_hit_zero = tool_output["session_db_replicas_hit_zero"]
    # peak_memory_mib deliberately NOT extracted here (2026-08-21) -- its
    # only consumer inside this function was the old memory-leak absolute-
    # threshold check, now replaced by heap_rise_kb below. Real remaining
    # consumer is action_proposer.py's own magnitude sizing, which reads
    # tool_output["peak_memory_mib"] directly, unaffected by this.
    peak_threads_connected = tool_output["peak_threads_connected"]
    cpu_throttle_periods_increase = tool_output["cpu_throttle_periods_increase"]
    # .get(), not [...] -- unlike every other field above (always present,
    # every tool_output build includes them), heap_rise_kb is a brand-new
    # 2026-08-21 field; any tool_output captured/replayed from before this
    # key existed genuinely won't have it, and that must read as "no
    # signal" (None), never a KeyError.
    heap_rise_kb = tool_output.get("heap_rise_kb")

    if oom_pods:
        return {
            "diagnosis": "oom",
            "confidence": 0.6,
            "reasoning": f"pods with last termination reason OOMKilled: {oom_pods} (stubbed rule, not LLM)",
        }
    if evicted_pods:
        return {
            "diagnosis": "disk-full",
            "confidence": 0.6,
            "reasoning": f"pods with status reason Evicted: {evicted_pods} (stubbed rule, not LLM)",
        }
    # Checked BEFORE crashlooping_pods -- real bug found 2026-08-03, same
    # "broader restart-increase catch-all steals a more specific signal's
    # episode" shape as OOM/Evicted being checked before crash-loop above,
    # just never extended to bad-rollout. crash_query's restart-increase
    # OR-clause fires on ANY restart for ANY reason in the last 3 minutes,
    # not just genuine crash-looping. Confirmed live (multiple real batch
    # episodes, predicted='crash-loop' actual='bad-rollout'): bad-rollout's
    # own reset step (rolling front-end's image back to baseline before
    # the NEXT injection) is itself a real rollout that can leave residual
    # restart activity on front-end bleeding into the very next episode's
    # 3-minute lookback window, even though nothing is actually crash-
    # looping. front_end_image_pull_failing is the more specific, direct
    # signal for this target and should win.
    if tool_output["front_end_image_pull_failing"]:
        return {
            "diagnosis": "bad-rollout",
            "confidence": 0.6,
            "reasoning": "a front-end pod is stuck in ImagePullBackOff/ErrImagePull -- a bad "
                         "deploy, not a crash-loop or generic restart (stubbed rule, not LLM)",
        }
    if crashlooping_pods:
        return {
            "diagnosis": "crash-loop",
            "confidence": 0.6,
            "reasoning": f"pods in CrashLoopBackOff: {crashlooping_pods} (stubbed rule, not LLM)",
        }
    if payment_stuck_not_ready:
        return {
            "diagnosis": "init-failure",
            "confidence": 0.6,
            "reasoning": "a payment pod is reporting Ready=false with no restart -- stuck, never "
                         "becoming ready, not crash-looping (stubbed rule, not LLM)",
        }
    if session_db_replicas_hit_zero:
        return {
            "diagnosis": "session-cart-failure",
            "confidence": 0.6,
            "reasoning": "session-db had zero available replicas within the recent window -- "
                         "the session/cart store itself is down, not a crash-loop or generic "
                         "restart (stubbed rule, not LLM)",
        }
    # REORDERED 2026-08-03, after Kimi review 20 (reviews/20_network_partition_
    # latency_discrimination_kimi_review.md) plus real production data from a
    # 290-episode batch: a present, mid-range p95_latency_ms (300-10000ms) is
    # checked BEFORE combined_throughput_bps now, not after. Real reason,
    # empirically confirmed (not assumed): combined_throughput_bps alone
    # cannot discriminate the two classes -- real network-partition episodes'
    # own throughput readings (26-89 bytes/s) directly overlap false-positive
    # network-latency episodes' readings (32-73 bytes/s), confirmed via two
    # independent real Prometheus signals (bytes AND packet counts both
    # falsified as clean discriminators, see the same review). But p95_latency_ms,
    # when present and in the 300-10000ms band, is a CLEAN signal -- across
    # 10 real ground-truth network-partition episodes, not one ever showed a
    # mid-range p95 (they land at ~60000ms, the k6 client-timeout ceiling, or
    # None entirely). The old ordering let a real, diagnostic p95 value get
    # short-circuited by the throughput check every time (confirmed live: one
    # real episode with p95=4706ms was still misdiagnosed as network-partition
    # solely because throughput also happened to be low that episode).
    if p95_latency_ms is not None and p95_latency_ms >= NETWORK_PARTITION_LATENCY_SATURATION_MS:
        return {
            "diagnosis": "network-partition",
            "confidence": 0.6,
            "reasoning": f"p95 request latency {p95_latency_ms}ms >= {NETWORK_PARTITION_LATENCY_SATURATION_MS}ms "
                         f"saturation ceiling -- a request hanging until client timeout, not organic latency "
                         f"(the real network-latency mechanism only ever injects 500ms+jitter) (stubbed rule, not LLM)",
        }
    if p95_latency_ms is not None and p95_latency_ms >= HIGH_LATENCY_THRESHOLD_MS:
        return {
            "diagnosis": "network-latency",
            "confidence": 0.6,
            "reasoning": f"p95 request latency {p95_latency_ms}ms >= {HIGH_LATENCY_THRESHOLD_MS}ms threshold (stubbed rule, not LLM)",
        }
    # Falls through to here only when p95_latency_ms is None (no k6 sample
    # landed in the lookback window -- confirmed via review 20 to be a real,
    # currently-unresolved ambiguous zone between network-partition and a
    # sparse-traffic network-latency episode, not fixable by reordering or
    # by any passive metric tried so far) or below the latency threshold.
    if combined_throughput_bps is not None and combined_throughput_bps < NETWORK_PARTITION_MAX_THROUGHPUT_BPS:
        return {
            "diagnosis": "network-partition",
            "confidence": 0.6,
            "reasoning": f"orders' combined network throughput {combined_throughput_bps:.1f} bytes/s "
                         f"< {NETWORK_PARTITION_MAX_THROUGHPUT_BPS} bytes/s threshold (stubbed rule, not LLM)",
        }
    if heap_rise_kb is not None and heap_rise_kb >= MEMORY_LEAK_RISE_THRESHOLD_KB:
        return {
            "diagnosis": "memory-leak",
            "confidence": 0.6,
            "reasoning": f"shipping's JVM heap rose {heap_rise_kb:.0f}KB ({heap_rise_kb / 1024:.1f}MiB) over its "
                         f"own real per-episode baseline >= {MEMORY_LEAK_RISE_THRESHOLD_KB}KB "
                         f"({MEMORY_LEAK_RISE_THRESHOLD_KB / 1024:.0f}MiB) threshold, no restart/OOM/eviction "
                         f"observed (stubbed rule, not LLM)",
        }
    if peak_threads_connected is not None and peak_threads_connected >= CONNECTION_POOL_THRESHOLD:
        return {
            "diagnosis": "connection-pool-exhaustion",
            "confidence": 0.6,
            "reasoning": f"peak MySQL Threads_connected {peak_threads_connected} >= {CONNECTION_POOL_THRESHOLD} threshold (stubbed rule, not LLM)",
        }
    if (
        cpu_throttle_periods_increase is not None
        and cpu_throttle_periods_increase >= CPU_THROTTLE_INCREASE_THRESHOLD
    ):
        return {
            "diagnosis": "cpu-throttling",
            "confidence": 0.6,
            "reasoning": f"user's CFS throttled-periods increased by {cpu_throttle_periods_increase} "
                         f">= {CPU_THROTTLE_INCREASE_THRESHOLD} threshold over the last 2m "
                         f"(stubbed rule, not LLM)",
        }
    return {
        "diagnosis": "no anomaly detected",
        "confidence": 0.5,
        "reasoning": "no pods in CrashLoopBackOff, no recent OOM kill, no eviction, no elevated latency/memory/connections (stubbed rule, not LLM)",
    }


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest):
    tool_output = query_prometheus(
        req.target, req.namespace, snapshot_at=req.snapshot_at, baseline_heap_kb=req.baseline_heap_kb
    )
    result = stub_diagnose(tool_output)
    # under-provisioned-replicas fallback: only fires the real active
    # probe when nothing cheaper already explains this target, and
    # only for catalogue -- see probe_catalogue_capacity's docstring
    # for why this is deliberately NOT gathered eagerly like every
    # other signal (avoids firing a real burst during an active oom
    # episode on the same target).
    if result["diagnosis"] == "no anomaly detected" and req.target == "catalogue":
        probe_p95_ms = probe_catalogue_capacity(req.namespace)
        tool_output["catalogue_probe_p95_ms"] = probe_p95_ms
        if probe_p95_ms is not None and probe_p95_ms >= UNDER_PROVISIONED_PROBE_THRESHOLD_MS:
            result = {
                "diagnosis": "under-provisioned-replicas",
                "confidence": 0.6,
                "reasoning": f"active capacity probe against catalogue showed p95={probe_p95_ms}ms "
                             f">= {UNDER_PROVISIONED_PROBE_THRESHOLD_MS}ms threshold (stubbed rule, not LLM)",
            }
    # DL/HMM/SPC fallback (call_dl_detector) -- only consulted if Prometheus's
    # own rules (and, for catalogue, the capacity probe above) still found
    # nothing. Deliberately reports a single generic, unclassified diagnosis
    # rather than guessing a specific fault class from an anomaly flag alone
    # (option A, decided 2026-07-29 -- see call_dl_detector's docstring: this
    # detector cannot tell WHICH class it is, only that something is off, and
    # building per-service class-guessing logic into the stub isn't worth it
    # given the whole stub gets replaced once the real LLM is wired in).
    if result["diagnosis"] == "no anomaly detected" and req.target in DL_DETECTOR_SERVICES:
        detector_result = call_dl_detector(req.target)
        tool_output["dl_detector_result"] = detector_result
        if detector_result is not None and detector_result.get("is_anomalous"):
            result = {
                "diagnosis": "log-anomaly detected (unclassified)",
                "confidence": 0.5,
                "reasoning": f"DL/HMM/SPC detector flagged {req.target} as anomalous "
                             f"(track={detector_result.get('track')}) with no Prometheus-based "
                             f"signal to explain it -- real anomaly, but this generic detector "
                             f"cannot identify which specific fault class it is (stubbed rule, not LLM)",
            }
    return {
        "target": req.target,
        "namespace": req.namespace,
        "tool_output": tool_output,
        **result,
    }
