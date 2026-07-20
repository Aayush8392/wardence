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
    """Tool: check whether a matching container is crash-looping.

    CrashLoopBackOff is a transient snapshot state -- kubelet only reports
    it while actively waiting before the next restart attempt, and as
    backoff delay grows the container spends more time transiently
    Running between attempts. Checking only the current state snapshot
    misses crash loops that are genuinely happening but caught mid-Running.

    Instead this asks "did this crash loop recently" as a fact: currently
    in CrashLoopBackOff, OR restarted at all in the last 3 minutes.
    """
    query = (
        f'(kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason="CrashLoopBackOff"}} == 1) '
        f'or (increase(kube_pod_container_status_restarts_total{{namespace="{namespace}", '
        f'pod=~"{target}.*"}}[3m]) > 0)'
    )
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]

    crashlooping_pods = [entry["metric"].get("pod") for entry in result]
    return {"raw_result": result, "crashlooping_pods": crashlooping_pods}


def stub_diagnose(tool_output: dict) -> dict:
    """
    Placeholder for the LLM reasoning step. Hardcoded rule standing in
    for the ReAct loop: any pod currently in CrashLoopBackOff -> report
    crash-loop. Replace this function's body with the real LLM call;
    keep the same tool-output-in, diagnosis-out shape.
    """
    crashlooping_pods = tool_output["crashlooping_pods"]

    if crashlooping_pods:
        return {
            "diagnosis": "crash-loop",
            "confidence": 0.6,
            "reasoning": f"pods in CrashLoopBackOff: {crashlooping_pods} (stubbed rule, not LLM)",
        }
    return {
        "diagnosis": "no anomaly detected",
        "confidence": 0.5,
        "reasoning": "no pods in CrashLoopBackOff (stubbed rule, not LLM)",
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
