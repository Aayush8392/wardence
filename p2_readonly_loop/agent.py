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

import requests
from fastapi import FastAPI
from pydantic import BaseModel

PROMETHEUS_URL = "http://localhost:9090"

# Absolute threshold, not baseline-relative -- the agent only sees one
# snapshot per /diagnose call, unlike injector.py's own verification
# which has a genuine before/after baseline to compare to. Sock Shop's
# normal p95 under traffic_gen's baseline load is well under this
# (injected delay is 500ms, see injector.py NETWORK_LATENCY_DELAY).
HIGH_LATENCY_THRESHOLD_MS = 300

# shipping's baseline working-set memory is ~298MiB (confirmed via a
# direct Prometheus query, 2026-07-21), and injector.py's stressor adds
# ~150MiB on top (see injector.py's MEMORY_LEAK_STRESS_SIZE docstring
# for why it's sized that small -- shipping's real headroom under its
# 500Mi limit is only ~202MiB). 380MiB sits comfortably between the
# ~298MiB normal baseline and the ~448MiB stressed level.
MEMORY_LEAK_THRESHOLD_MIB = 380

# catalogue-db's max_connections is 151, baseline Threads_connected is
# ~2-3 (both confirmed via direct query, 2026-07-21). 100 sits
# comfortably between normal baseline and the ~142 level reached during
# a real flood (140 injected + baseline).
CONNECTION_POOL_THRESHOLD = 100

app = FastAPI()


class DiagnoseRequest(BaseModel):
    target: str
    namespace: str


def query_prometheus(target: str, namespace: str) -> dict:
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
    oom_query = (
        f'(kube_pod_container_status_last_terminated_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="OOMKilled"}} == 1) '
        f'and on(namespace, pod) (increase(kube_pod_container_status_restarts_total'
        f'{{namespace="{namespace}", pod=~"{target}.*"}}[3m]) > 0)'
    )
    evicted_query = (
        f'kube_pod_status_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="Evicted"}} == 1'
    )
    recent_running_pod_query = (
        f'(kube_pod_status_phase{{namespace="{namespace}", pod=~"{target}.*", phase="Running"}} == 1) '
        f'and on(namespace, pod) ((time() - kube_pod_created'
        f'{{namespace="{namespace}", pod=~"{target}.*"}}) < 180)'
    )
    crash_query = (
        f'(kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="CrashLoopBackOff"}} == 1) '
        f'or (increase(kube_pod_container_status_restarts_total{{namespace="{namespace}", '
        f'pod=~"{target}.*"}}[3m]) > 0)'
    )

    oom_resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": oom_query}, timeout=10)
    oom_resp.raise_for_status()
    oom_result = oom_resp.json()["data"]["result"]

    evicted_resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": evicted_query}, timeout=10
    )
    evicted_resp.raise_for_status()
    evicted_result = evicted_resp.json()["data"]["result"]

    recent_running_resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": recent_running_pod_query}, timeout=10
    )
    recent_running_resp.raise_for_status()
    has_recent_replacement_pod = len(recent_running_resp.json()["data"]["result"]) > 0

    crash_resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": crash_query}, timeout=10)
    crash_resp.raise_for_status()
    crash_result = crash_resp.json()["data"]["result"]

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
        latency_resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": latency_query}, timeout=10
        )
        latency_resp.raise_for_status()
        latency_result = latency_resp.json()["data"]["result"]
        # k6's exported value is in SECONDS -- confirmed empirically
        # (2026-07-21), see injector.py's _orders_p95_latency_ms docstring.
        p95_latency_ms = max((float(e["value"][1]) * 1000 for e in latency_result), default=None)
    else:
        p95_latency_ms = None

    # memory-leak: cAdvisor's container_memory_working_set_bytes is a
    # real-time gauge, not a percentile estimator -- but that cuts the
    # other way from the latency metric's problem: it reflects TRUE
    # CURRENT memory, so once injector.py's stressor process ends, the
    # elevated reading disappears almost immediately (unlike a restart
    # count or OOM reason, which stay as sticky historical evidence).
    # By the time this endpoint gets called (injector-end + settle),
    # an instant query would very likely see nothing. max_over_time
    # over a window wide enough to cover the fault's own duration_s
    # (100s) plus the settle gap avoids that -- same fix as the latency
    # query above, different root cause (here it's about a genuinely
    # un-sticky signal, not volatility).
    if target == "shipping":
        memory_query = (
            f'max_over_time(container_memory_working_set_bytes{{namespace="{namespace}", '
            f'pod=~"{target}.*", container="{target}"}}[3m])'
        )
        memory_resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": memory_query}, timeout=10
        )
        memory_resp.raise_for_status()
        memory_result = memory_resp.json()["data"]["result"]
        peak_memory_mib = max(
            (float(e["value"][1]) / (1024 * 1024) for e in memory_result), default=None
        )
    else:
        peak_memory_mib = None

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
            f'pod=~"{target}.*"}}[3m])'
        )
        threads_resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": threads_query}, timeout=10
        )
        threads_resp.raise_for_status()
        threads_result = threads_resp.json()["data"]["result"]
        peak_threads_connected = max(
            (float(e["value"][1]) for e in threads_result), default=None
        )
    else:
        peak_threads_connected = None

    oom_pods = [entry["metric"].get("pod") for entry in oom_result]
    # Only trust the Evicted signal if there's ALSO a freshly-created
    # Running pod for this target -- see query docstring above for why
    # the evicted pod's own metadata can't be trusted for recency.
    evicted_pods_raw = [entry["metric"].get("pod") for entry in evicted_result]
    evicted_pods = evicted_pods_raw if (evicted_pods_raw and has_recent_replacement_pod) else []
    crashlooping_pods = [entry["metric"].get("pod") for entry in crash_result]
    return {
        "oom_pods": oom_pods,
        "evicted_pods": evicted_pods,
        "crashlooping_pods": crashlooping_pods,
        "p95_latency_ms": p95_latency_ms,
        "peak_memory_mib": peak_memory_mib,
        "peak_threads_connected": peak_threads_connected,
    }


def stub_diagnose(tool_output: dict) -> dict:
    """
    Placeholder for the LLM reasoning step. Hardcoded rule standing in
    for the ReAct loop. OOM and Evicted are checked before crash-loop:
    both can otherwise get swept up by the broader restart-increase
    catch-all the crash-loop signal watches. Replace this function's
    body with the real LLM call; keep the same tool-output-in,
    diagnosis-out shape.
    """
    oom_pods = tool_output["oom_pods"]
    evicted_pods = tool_output["evicted_pods"]
    crashlooping_pods = tool_output["crashlooping_pods"]
    p95_latency_ms = tool_output["p95_latency_ms"]
    peak_memory_mib = tool_output["peak_memory_mib"]
    peak_threads_connected = tool_output["peak_threads_connected"]

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
    if crashlooping_pods:
        return {
            "diagnosis": "crash-loop",
            "confidence": 0.6,
            "reasoning": f"pods in CrashLoopBackOff: {crashlooping_pods} (stubbed rule, not LLM)",
        }
    if p95_latency_ms is not None and p95_latency_ms >= HIGH_LATENCY_THRESHOLD_MS:
        return {
            "diagnosis": "network-latency",
            "confidence": 0.6,
            "reasoning": f"p95 request latency {p95_latency_ms}ms >= {HIGH_LATENCY_THRESHOLD_MS}ms threshold (stubbed rule, not LLM)",
        }
    if peak_memory_mib is not None and peak_memory_mib >= MEMORY_LEAK_THRESHOLD_MIB:
        return {
            "diagnosis": "memory-leak",
            "confidence": 0.6,
            "reasoning": f"peak working-set memory {peak_memory_mib}MiB >= {MEMORY_LEAK_THRESHOLD_MIB}MiB threshold, "
                         f"no restart/OOM/eviction observed (stubbed rule, not LLM)",
        }
    if peak_threads_connected is not None and peak_threads_connected >= CONNECTION_POOL_THRESHOLD:
        return {
            "diagnosis": "connection-pool-exhaustion",
            "confidence": 0.6,
            "reasoning": f"peak MySQL Threads_connected {peak_threads_connected} >= {CONNECTION_POOL_THRESHOLD} threshold (stubbed rule, not LLM)",
        }
    return {
        "diagnosis": "no anomaly detected",
        "confidence": 0.5,
        "reasoning": "no pods in CrashLoopBackOff, no recent OOM kill, no eviction, no elevated latency/memory/connections (stubbed rule, not LLM)",
    }


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest):
    tool_output = query_prometheus(req.target, req.namespace)
    result = stub_diagnose(tool_output)
    return {
        "target": req.target,
        "namespace": req.namespace,
        "tool_output": tool_output,
        **result,
    }
