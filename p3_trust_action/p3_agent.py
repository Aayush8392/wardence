"""
P3 agent: same diagnosis logic as p2_readonly_loop/agent.py (reused, not
duplicated), extended with a /handle endpoint that ALSO acts when the
diagnosed fault class is trusted (Can-Act).

Still blinded from ground truth -- this agent decides to act based only
on its own diagnosis + the trust state, never the injector's DB. Judging
whether the action was actually correct is the scorer's job (it's the
only piece allowed to see ground truth), not this agent's.

Maps predicted diagnosis string -> fault_class key used by trust_engine.
Only the 6 auto-fix classes have an action mapping; anything else is
report-only by construction (no entry in ACTION_MAP).

Three-dimension trust structure (2026-07-31 build, see
wardence_context.md's Model Strategy section and
p3_trust_action/llm_trust_state.py): Dimension A above is unchanged.
Dimension B (per-class stub/llm diagnoser mode) and Dimension C
(per-class deterministic_fallback/llm_can_act action trust) now
determine whether THIS response's production diagnosis/action comes
from the stub or the real LLM. Every real /handle call ALSO runs the
real ReAct loop in the background regardless of current mode (the
"continuous background comparison" the whole Dimension B streak depends
on -- real quota cost accepted by direct user instruction, see
wardence_buildlog.md's 2026-07-30/31 session). The response carries the
raw stub AND raw LLM results plus which one actually drove production
behavior (diagnoser_mode_used/action_source) -- p3_scorer.py (the only
piece allowed to see ground truth) is what actually logs to
llm_diagnosis_log/llm_action_proposal_log and updates Dimension B/C
state, same "agent proposes, scorer judges" split as Dimension A always
had.

Split into two endpoints, 2026-07-31 (Kimi review 13's locked decision --
see wardence_buildlog.md's 2026-07-31 session): /handle used to do
diagnosis AND action dispatch in one blocking call/timeout. Now:
  POST /diagnose {"target": ..., "namespace": ...} -> stub + background
    LLM diagnosis only, plus "eligible_for_action" telling the caller
    whether /act is even worth calling.
  POST /act {...fields from /diagnose's response...} -> action proposal
    (if diagnoser_mode_used=="llm") + real dispatch, re-checking trust
    state fresh rather than trusting whatever /diagnose observed.
p3_scorer.py is the only real caller and times each phase independently.

Usage:
    Terminal 1: kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
    Terminal 2: uvicorn p3_agent:app --reload --app-dir p3_trust_action --port 8001
    Then: POST http://localhost:8001/diagnose {"target": "carts", "namespace": "sock-shop"}
"""

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "p2_readonly_loop"))

from actions import ALLOWED_ACTIONS, get_progress  # noqa: E402
from trust_engine import DB_PATH, ensure_trust_tables, get_trust_state  # noqa: E402
from llm_trust_state import (  # noqa: E402
    LLM, STUB, LLM_CAN_ACT, ensure_llm_trust_tables, get_action_trust, get_diagnoser_mode,
)
from action_proposer import DETERMINISTIC_ACTION_MAP, propose_action  # noqa: E402
from react_agent import run_react_diagnosis  # noqa: E402
from misdispatch_guard import ensure_misdispatch_tables, get_safety_hold  # noqa: E402
import dispatch_gate  # noqa: E402

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


class ActRequest(BaseModel):
    target: str
    namespace: str
    predicted: str
    diagnoser_mode_used: str
    confidence: float | None = None
    reasoning: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    tool_output: dict | None = None
    # Ground truth, review 16's dispatch gate -- ONLY ever populated by
    # p3_scorer.py, the sole piece of this system allowed to see it.
    # Used exclusively for the gate's own comparison inside this
    # endpoint; never forwarded to propose_action()/run_react_diagnosis
    # or any other LLM-facing call. None means "no gate check" (e.g. a
    # caller other than the real scorer, or a live-trigger path that
    # hasn't been updated to pass it yet) -- never assumed correct.
    episode_id: str | None = None
    actual_class: str | None = None


def _build_llm_tools(target: str, namespace: str) -> dict:
    """Same real tool set + binding convention as test_react_agent.py's
    build_tools -- kept in sync by hand (same 'duplicated by hand'
    convention this project already uses for FAULT_CLASSES/FIELD_GUIDANCE/
    DETERMINISTIC_ACTION_MAP, not a new pattern)."""
    tools = {"query_prometheus": lambda: query_prometheus(target, namespace)}
    if target in DL_DETECTOR_SERVICES:
        tools["call_dl_detector"] = lambda: call_dl_detector(target)
    if target == "catalogue":
        tools["probe_catalogue_capacity"] = lambda: probe_catalogue_capacity(namespace)
    return tools


def _normalize_predicted_class(predicted: str) -> str | None:
    """Maps a diagnosis string (stub's own vocabulary) to the fault_class
    key llm_trust_state's tables are keyed on. Returns None for the
    generic DL-detector fallback ("log-anomaly detected (unclassified)")
    -- that's not a real taxonomy class, so it has no diagnoser_mode row
    and Dimension B/C never apply to it (stays stub-only by construction,
    same as it already was before this session)."""
    if predicted == "no anomaly detected":
        return "none"
    if predicted == "log-anomaly detected (unclassified)":
        return None
    return predicted


@app.post("/diagnose")
def diagnose(req: HandleRequest):
    """
    Phase 1 of the split (2026-07-31, Kimi review 13's locked decision):
    stub + background LLM diagnosis only. NEVER calls propose_action or
    dispatches a real action -- that's /act's job, phase 2, with its own
    independent timeout. Splitting means a slow/stuck action-proposal
    call can never also blow out the diagnosis call's timeout budget,
    and vice versa; the caller (p3_scorer.py) times each phase
    separately rather than one large combined wait.

    Returns "eligible_for_action": whether this diagnosis landed on an
    auto-fix class AND that class is currently can_act (Dimension A) --
    the caller uses this to decide whether /act is even worth calling,
    same as the old /handle's early-return-on-report-only behavior.
    """
    tool_output = query_prometheus(req.target, req.namespace)
    stub_result = stub_diagnose(tool_output)
    # under-provisioned-replicas fallback -- mirrors agent.py's own
    # /diagnose endpoint exactly, same reason (see
    # probe_catalogue_capacity's docstring): only fires the real
    # active probe when nothing cheaper already explains this target.
    if stub_result["diagnosis"] == "no anomaly detected" and req.target == "catalogue":
        probe_p95_ms = probe_catalogue_capacity(req.namespace)
        tool_output["catalogue_probe_p95_ms"] = probe_p95_ms
        if probe_p95_ms is not None and probe_p95_ms >= UNDER_PROVISIONED_PROBE_THRESHOLD_MS:
            stub_result = {
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
    if stub_result["diagnosis"] == "no anomaly detected" and req.target in DL_DETECTOR_SERVICES:
        detector_result = call_dl_detector(req.target)
        tool_output["dl_detector_result"] = detector_result
        if detector_result is not None and detector_result.get("is_anomalous"):
            stub_result = {
                "diagnosis": "log-anomaly detected (unclassified)",
                "confidence": 0.5,
                "reasoning": f"DL/HMM/SPC detector flagged {req.target} as anomalous "
                             f"(track={detector_result.get('track')}) with no Prometheus-based "
                             f"signal to explain it -- real anomaly, but this generic detector "
                             f"cannot identify which specific fault class it is (stubbed rule, not LLM)",
            }

    # Continuous background LLM comparison -- ALWAYS runs, regardless of
    # this class's current diagnoser_mode, so Dimension B's streak has
    # real data from every real episode (2026-07-30/31 locked decision,
    # real quota cost accepted). Never touches trust_engine/ACTION_MAP
    # directly here -- run_react_diagnosis is comparison-only by its own
    # construction; whether its result DRIVES production behavior below
    # is decided purely by the current diagnoser_mode read from the DB.
    #
    # WARDENCE_STUB_ONLY escape hatch, added 2026-07-31: real Dimension
    # C magnitude-correctness issue found live-testing tonight (see
    # reviews/14), plus real quota concerns for an unattended overnight
    # run -- set this env var to skip the LLM call entirely for this
    # run, forcing pure stub-only behavior (diagnoser_mode_used always
    # STUB, since llm_result never reaches "diagnosed"). Not a permanent
    # toggle, not wired into any config file -- just an explicit,
    # temporary way to fall back to the pre-Phase-H behavior for one
    # session without touching any of the real Phase H code.
    if os.environ.get("WARDENCE_STUB_ONLY"):
        llm_result = {"status": "llm_unavailable", "llm_diagnosis": None,
                       "failed_attempts": [], "detail": "WARDENCE_STUB_ONLY set -- LLM call skipped"}
    else:
        llm_tools = _build_llm_tools(req.target, req.namespace)
        llm_result = run_react_diagnosis(req.target, req.namespace, llm_tools)

    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    ensure_llm_trust_tables(conn)
    fault_class_key = _normalize_predicted_class(stub_result["diagnosis"])
    diagnoser_mode = get_diagnoser_mode(conn, fault_class_key)["mode"] if fault_class_key else STUB
    conn.close()

    diagnoser_mode_used = STUB
    production_result = stub_result
    if diagnoser_mode == LLM and llm_result["status"] == "diagnosed":
        # Dimension B says trust the LLM for this class, and it actually
        # produced a diagnosis this call -- let it drive production
        # behavior. A quota-exhausted/unavailable/max-turns LLM always
        # falls back to the stub's own answer (safe default, never
        # silently act on nothing), same discipline as every other
        # "known-dead provider -> report_only" rule already locked in
        # this project.
        diagnoser_mode_used = LLM
        production_result = {
            "diagnosis": llm_result["llm_diagnosis"],
            "confidence": llm_result["llm_confidence"],
            "reasoning": llm_result["llm_reasoning"],
        }

    predicted = production_result["diagnosis"]

    response = {
        "target": req.target,
        "namespace": req.namespace,
        "tool_output": tool_output,
        **production_result,
        "trust_state": None,
        "eligible_for_action": False,
        # New fields for p3_scorer.py -- it has the episode_id this
        # request doesn't, so IT logs llm_diagnosis_log/
        # llm_action_proposal_log and updates Dimension B/C state, not
        # this endpoint. Raw stub/LLM results both included so the
        # scorer can compare each against ground truth independently.
        "diagnoser_mode_used": diagnoser_mode_used,
        "stub_result": stub_result,
        "llm_result": llm_result,
    }

    mapping = ACTION_MAP.get(predicted)
    if mapping is None:
        return response  # report-only class, or "no anomaly detected"

    fault_class, _action_name, _kwargs_fn = mapping

    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    ensure_misdispatch_tables(conn)
    trust_state = get_trust_state(conn, fault_class)
    safety_hold = get_safety_hold(conn, fault_class)
    conn.close()
    response["trust_state"] = trust_state
    # A safety hold (misdispatch_guard.py) blocks dispatch WITHOUT being
    # a Dimension A demotion -- streak stays intact, only the real
    # dispatch is paused. See misdispatch_guard.py's own module
    # docstring for why this is a separate mechanism from trust_state.
    response["eligible_for_action"] = trust_state["state"] == "can_act" and not safety_hold["active"]

    return response


def _dispatch_with_gate(predicted_class: str, proposed_tool: str, proposed_params: dict,
                         action_source: str, req: "ActRequest") -> tuple[str, dict, str, "dict | None"]:
    """
    Real dispatch, gate-checked first (review 16). If req.actual_class
    is None (caller didn't pass ground truth -- e.g. a live-trigger path
    not yet wired to it), the gate is skipped entirely and the agent's
    own proposal dispatches unmodified, same as before this change.

    Never touches req.actual_class for anything except this comparison
    -- not returned in a form that reaches propose_action/
    run_react_diagnosis, not stored anywhere the diagnosing agent could
    read it back on a future call.

    Returns (action_taken, action_result, action_source,
    gate_substitution). gate_substitution is None unless a real
    redirect happened, in which case it's the full honest record (what
    the agent proposed, what actually dispatched, why) -- p3_scorer.py
    copies this verbatim into episode_snapshots.gate_substitution for
    the Replay Viewer, so a substitution is never silently invisible.
    """
    if req.actual_class is not None:
        gate_result = dispatch_gate.check(
            ACTION_MAP, predicted_class, req.actual_class,
            proposed_tool, proposed_params, req.target, req.namespace, req.tool_output,
        )
        if gate_result["substituted"]:
            conn = sqlite3.connect(DB_PATH)
            dispatch_gate.log_intervention(
                conn, req.episode_id, predicted_class, req.actual_class,
                proposed_tool, gate_result["actual_tool"], gate_result["reason"],
            )
            conn.close()
            action_fn = ALLOWED_ACTIONS[gate_result["actual_tool"]]
            action_result = action_fn(**gate_result["actual_params"])
            gate_substitution = {
                "predicted_class": predicted_class,
                "actual_class": req.actual_class,
                "proposed_tool": proposed_tool,
                "proposed_params": proposed_params,
                "substituted_tool": gate_result["actual_tool"],
                "substituted_params": gate_result["actual_params"],
                "reason": gate_result["reason"],
            }
            return gate_result["actual_tool"], action_result, "gate_substituted", gate_substitution

    action_fn = ALLOWED_ACTIONS[proposed_tool]
    action_result = action_fn(**proposed_params)
    return proposed_tool, action_result, action_source, None


@app.post("/act")
def act(req: ActRequest):
    """
    Phase 2 of the split -- only ever called by the caller when
    /diagnose said eligible_for_action=True. Re-checks trust_state AND
    action_trust FRESH from the DB rather than trusting whatever
    /diagnose observed (a real, if narrow, improvement over the old
    single-call /handle: trust state could in principle change between
    the two calls, e.g. a circuit-breaker trip landing in the gap -- a
    stale eligibility check would have been a real safety gap the old
    single-call design never had to worry about, since it checked and
    acted in one atomic request).

    A safe no-op (all fields None) if trust_state no longer says
    can_act by the time this runs, or if `predicted` doesn't map to an
    auto-fix class at all -- never raises just because the world moved
    on since /diagnose ran.
    """
    response = {
        "action_taken": None, "action_result": None, "action_source": None,
        "llm_action_proposal": None, "gate_substitution": None,
    }

    mapping = ACTION_MAP.get(req.predicted)
    if mapping is None:
        return response

    fault_class, action_name, kwargs_fn = mapping

    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    ensure_llm_trust_tables(conn)
    ensure_misdispatch_tables(conn)
    trust_state = get_trust_state(conn, fault_class)
    action_trust = get_action_trust(conn, fault_class)
    safety_hold = get_safety_hold(conn, fault_class)
    conn.close()

    if trust_state["state"] != "can_act" or safety_hold["active"]:
        return response

    production_result = {"diagnosis": req.predicted, "confidence": req.confidence, "reasoning": req.reasoning}

    # Background comparison for Dimension C too, same "always compute,
    # only conditionally dispatch" split as diagnosis has -- lets the
    # action-trust streak accumulate real data even while this class is
    # still on deterministic_fallback. Only meaningful when the LLM was
    # the one that diagnosed this episode -- propose_action's own
    # diagnosis-provider lookup needs a real provider/model to escalate
    # from.
    if req.diagnoser_mode_used == LLM and fault_class in DETERMINISTIC_ACTION_MAP:
        proposal = propose_action(
            fault_class, production_result, req.target, req.namespace,
            req.llm_provider, req.llm_model, tool_output=req.tool_output,
        )
        response["llm_action_proposal"] = proposal

        if action_trust["state"] == LLM_CAN_ACT and proposal.get("tool_name") is not None:
            # Dimension C says trust the LLM's own action choice for this
            # class -- dispatch WHAT IT PROPOSED (which may already be
            # the deterministic_fallback shape if both LLM attempts
            # failed validation; propose_action always returns something
            # dispatchable-or-not, never raises). Gate-checked first
            # (review 16) -- if req.predicted is wrong and this proposal
            # would misfire on the real fault, the real dispatch is
            # redirected; Dimension C's own scoring in p3_scorer.py is
            # untouched either way (still scores what the LLM proposed).
            action_taken, action_result, action_source, gate_substitution = _dispatch_with_gate(
                req.predicted, proposal["tool_name"], proposal["params"], proposal["source"], req,
            )
            response["action_taken"] = action_taken
            response["action_result"] = action_result
            response["action_source"] = action_source
            response["gate_substitution"] = gate_substitution
            return response

    # Deterministic production dispatch -- still the path for:
    # stub-mode classes, LLM-mode classes not yet Dimension-C-trusted,
    # and any class outside DETERMINISTIC_ACTION_MAP entirely. Gate-
    # checked the same as the LLM path above -- a wrong STUB diagnosis
    # is just as real a misdispatch risk as a wrong LLM one.
    action_taken, action_result, action_source, gate_substitution = _dispatch_with_gate(
        req.predicted, action_name, kwargs_fn(req.target, req.namespace), "deterministic_production", req,
    )
    response["action_taken"] = action_taken
    response["action_result"] = action_result
    response["gate_substitution"] = gate_substitution
    response["action_source"] = action_source

    return response


@app.get("/progress/{namespace}/{name}")
def progress(namespace: str, name: str):
    """
    Live-trigger view polls this while a multi-step fix (e.g.
    restore_from_disk_full) is in flight from a concurrent /act call.
    Works because /act is a plain `def`, not `async def` -- FastAPI
    runs it in a threadpool, so this GET is served by a different worker
    thread while /act's POST is still blocked mid-fix, not queued
    behind it. Single-step actions (restart_deployment, patch_memory_limit)
    never populate this -- an empty list here just means either nothing
    is running or the fix was one-shot, not multi-step.
    """
    return {"steps": get_progress(name, namespace)}
