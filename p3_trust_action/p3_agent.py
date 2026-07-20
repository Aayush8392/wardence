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

from actions import ALLOWED_ACTIONS  # noqa: E402
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

app = FastAPI()

# predicted diagnosis string -> (fault_class key, action name, action kwargs builder)
ACTION_MAP = {
    "crash-loop": ("crash-loop", "restart_deployment", lambda target: {"name": target}),
}


class HandleRequest(BaseModel):
    target: str
    namespace: str


@app.post("/handle")
def handle(req: HandleRequest):
    tool_output = query_prometheus(req.target, req.namespace)
    diagnosis_result = stub_diagnose(tool_output)
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
    action_result = action_fn(**kwargs_fn(req.target))
    response["action_taken"] = action_name
    response["action_result"] = action_result

    return response
