"""
P3 agent: same diagnosis logic as p2_readonly_loop/agent.py (reused, not
duplicated), extended with a /handle endpoint that ALSO acts when the
diagnosed fault class is trusted (Can-Act).

Still blinded from ground truth -- this agent decides to act based only
on its own diagnosis + the trust state, never the injector's DB. Judging
whether the action was actually correct is the scorer's job (it's the
only piece allowed to see ground truth), not this agent's.

Maps predicted diagnosis string -> fault_class key used by trust_engine.
Only the 3 easy auto-fix classes have an action mapping; anything else
is report-only by construction (no entry in ACTION_MAP).

Usage:
    Terminal 1: kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
    Terminal 2: uvicorn p3_agent:app --reload --app-dir p3_trust_action --port 8001
    Then: POST http://localhost:8001/handle {"target": "carts", "namespace": "sock-shop"}
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from actions import ALLOWED_ACTIONS, get_progress  # noqa: E402
from trust_engine import DB_PATH, ensure_trust_tables, get_trust_state  # noqa: E402

# Both P2 and P3 have a file named agent.py -- a plain `from agent import
# ...` here would resolve to THIS file (already partially loaded) instead
# of P2's, causing a circular import. Load P2's by explicit path instead.
_p2_agent_path = Path(__file__).parent.parent / "p2_readonly_loop" / "agent.py"
_spec = importlib.util.spec_from_file_location("p2_agent", _p2_agent_path)
_p2_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_p2_agent)
query_prometheus = _p2_agent.query_prometheus
stub_diagnose = _p2_agent.stub_diagnose
probe_catalogue_capacity = _p2_agent.probe_catalogue_capacity
UNDER_PROVISIONED_PROBE_THRESHOLD_MS = _p2_agent.UNDER_PROVISIONED_PROBE_THRESHOLD_MS
call_dl_detector = _p2_agent.call_dl_detector
DL_DETECTOR_SERVICES = _p2_agent.DL_DETECTOR_SERVICES

app = FastAPI()

# Extra static params each fix needs beyond the request's own
# target/namespace -- container+new limit for oom, target replica count
# for disk-full. Deliberately NOT shared with p2/injector.py's
# FAULT_CONFIG: these are FIX-side details, injector.py and the blinded
# diagnosis agent should never know about them.
FIX_PARAMS = {
    "oom": {"container": "catalogue", "limit": "400Mi"},  # catalogue's original limit is 200Mi
    "disk-full": {"replicas": 1},
    "cpu-throttling": {"container": "user", "limit": "600m"},  # user's original limit is 300m
    "under-provisioned-replicas": {"replicas": 3},  # catalogue's original count is 1
    "bad-rollout": {},  # rollback_deployment needs only name/namespace, no extra params
}

# predicted diagnosis string -> (fault_class key, action name, action kwargs builder).
# kwargs builders take (target, namespace) -- crash-loop's only ever needed
# target, but oom/disk-full's actions need extra fixed params from
# FIX_PARAMS too, so all three take the same two args for consistency.
ACTION_MAP = {
    "crash-loop": (
        "crash-loop",
        "restart_deployment",
        lambda target, namespace: {"name": target, "namespace": namespace},
    ),
    "oom": (
        "oom",
        "patch_memory_limit",
        lambda target, namespace: {"name": target, "namespace": namespace, **FIX_PARAMS["oom"]},
    ),
    "disk-full": (
        "disk-full",
        "restore_from_disk_full",
        lambda target, namespace: {"name": target, "namespace": namespace, **FIX_PARAMS["disk-full"]},
    ),
    "cpu-throttling": (
        "cpu-throttling",
        "patch_cpu_limit",
        lambda target, namespace: {"name": target, "namespace": namespace, **FIX_PARAMS["cpu-throttling"]},
    ),
    "under-provisioned-replicas": (
        "under-provisioned-replicas",
        "scale_deployment",
        lambda target, namespace: {
            "name": target, "namespace": namespace, **FIX_PARAMS["under-provisioned-replicas"]
        },
    ),
    "bad-rollout": (
        "bad-rollout",
        "rollback_deployment",
        lambda target, namespace: {"name": target, "namespace": namespace},
    ),
}


class HandleRequest(BaseModel):
    target: str
    namespace: str


@app.post("/handle")
def handle(req: HandleRequest):
    tool_output = query_prometheus(req.target, req.namespace)
    diagnosis_result = stub_diagnose(tool_output)
    # under-provisioned-replicas fallback -- mirrors agent.py's own
    # /diagnose endpoint exactly, same reason (see
    # probe_catalogue_capacity's docstring): only fires the real
    # active probe when nothing cheaper already explains this target.
    if diagnosis_result["diagnosis"] == "no anomaly detected" and req.target == "catalogue":
        probe_p95_ms = probe_catalogue_capacity(req.namespace)
        tool_output["catalogue_probe_p95_ms"] = probe_p95_ms
        if probe_p95_ms is not None and probe_p95_ms >= UNDER_PROVISIONED_PROBE_THRESHOLD_MS:
            diagnosis_result = {
                "diagnosis": "under-provisioned-replicas",
                "confidence": 0.6,
                "reasoning": f"active capacity probe against catalogue showed p95={probe_p95_ms}ms "
                             f">= {UNDER_PROVISIONED_PROBE_THRESHOLD_MS}ms threshold (stubbed rule, not LLM)",
            }
    # DL/HMM/SPC fallback -- mirrors agent.py's own /diagnose endpoint
    # exactly (see call_dl_detector's docstring for the full reasoning):
    # a generic, unclassified anomaly flag, never mapped into ACTION_MAP
    # below, so it always stays report-only by construction, same as any
    # other class with no entry there.
    if diagnosis_result["diagnosis"] == "no anomaly detected" and req.target in DL_DETECTOR_SERVICES:
        detector_result = call_dl_detector(req.target)
        tool_output["dl_detector_result"] = detector_result
        if detector_result is not None and detector_result.get("is_anomalous"):
            diagnosis_result = {
                "diagnosis": "log-anomaly detected (unclassified)",
                "confidence": 0.5,
                "reasoning": f"DL/HMM/SPC detector flagged {req.target} as anomalous "
                             f"(track={detector_result.get('track')}) with no Prometheus-based "
                             f"signal to explain it -- real anomaly, but this generic detector "
                             f"cannot identify which specific fault class it is (stubbed rule, not LLM)",
            }
    predicted = diagnosis_result["diagnosis"]

    response = {
        "target": req.target,
        "namespace": req.namespace,
        "tool_output": tool_output,
        **diagnosis_result,
        "trust_state": None,
        "action_taken": None,
        "action_result": None,
    }

    mapping = ACTION_MAP.get(predicted)
    if mapping is None:
        return response  # report-only class, or "no anomaly detected"

    fault_class, action_name, kwargs_fn = mapping

    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    trust_state = get_trust_state(conn, fault_class)
    conn.close()
    response["trust_state"] = trust_state

    if trust_state["state"] != "can_act":
        return response

    action_fn = ALLOWED_ACTIONS[action_name]
    action_result = action_fn(**kwargs_fn(req.target, req.namespace))
    response["action_taken"] = action_name
    response["action_result"] = action_result

    return response


@app.get("/progress/{namespace}/{name}")
def progress(namespace: str, name: str):
    """
    Live-trigger view polls this while a multi-step fix (e.g.
    restore_from_disk_full) is in flight from a concurrent /handle call.
    Works because /handle is a plain `def`, not `async def` -- FastAPI
    runs it in a threadpool, so this GET is served by a different worker
    thread while /handle's POST is still blocked mid-fix, not queued
    behind it. Single-step actions (restart_deployment, patch_memory_limit)
    never populate this -- an empty list here just means either nothing
    is running or the fix was one-shot, not multi-step.
    """
    return {"steps": get_progress(name, namespace)}
