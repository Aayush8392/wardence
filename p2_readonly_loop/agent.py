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

app = FastAPI()


class DiagnoseRequest(BaseModel):
    target: str
    namespace: str


def query_prometheus(target: str, namespace: str) -> dict:
    """Tool: check whether a matching container is crash-looping, was OOM-killed,
    or was evicted (disk-full).

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
    return {
        "diagnosis": "no anomaly detected",
        "confidence": 0.5,
        "reasoning": "no pods in CrashLoopBackOff, no recent OOM kill, no eviction (stubbed rule, not LLM)",
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
