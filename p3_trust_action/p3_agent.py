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

import concurrent.futures
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import time
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
from react_agent import run_react_diagnosis, FAULT_CLASSES  # noqa: E402
from model_backend import PROVIDER_CHAIN, call_one, LLMFailure  # noqa: E402
from llm_replay_test import build_prompt  # noqa: E402
from misdispatch_guard import ensure_misdispatch_tables, get_safety_hold  # noqa: E402
import dispatch_gate  # noqa: E402
from constraint_checks import check_safe  # noqa: E402

logger = logging.getLogger(__name__)

# Comparison-sampling addition, 2026-08-05 (design reviewed by Kimi,
# reviews/24_comparison_sampling_background_dispatch_kimi_review.md --
# this is Kimi's recommended architecture, not the original callback/
# Future-based draft that review replaced). Every /diagnose call, in
# addition to the real primary chain below, ALSO fires these two
# fallback-tier models purely to log their diagnosis for comparison --
# never drives production behavior, same "comparison-only" discipline
# as the existing background LLM call. Filtered from PROVIDER_CHAIN by
# provider+model, not hardcoded, so this can never silently drift from
# the real chain's own entries if either ever changes.
COMPARISON_ENTRIES = [
    e for e in PROVIDER_CHAIN
    if (e["provider"], e["model"]) in {
        ("groq", "openai/gpt-oss-120b"),
        ("openrouter", "openai/gpt-oss-20b:free"),
    }
]

# Bounded and module-level -- never created per-request (Kimi review 24,
# point 6: unbounded thread growth). Sized for a handful of concurrent
# episodes' worth of comparison calls, not unlimited. If episodes come
# in faster than this can drain, newer comparison calls queue behind
# older ones rather than spawning more threads -- never blocks the real
# diagnosis either way.
_comparison_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="comparison"
)


def ensure_comparison_sampling_table(conn: sqlite3.Connection):
    """
    Dedicated table for comparison-sampling data (2026-08-05), replacing
    an earlier draft that tried to reuse llm_diagnosis_log -- that
    table's actual_class column is NOT NULL, which would have silently
    failed every single comparison-sampling write (this endpoint never
    sees ground truth, same discipline as the rest of p3_agent.py; only
    p3_scorer.py does). Ground truth stays joinable later via episode_id
    -> episodes.fault_class, same as any other real analysis.

    Single-shot by design, NOT the multi-turn ReAct loop the primary
    chain uses -- real accuracy testing (2026-08-05, 39 real episodes)
    found the short single-shot prompt (same shape as
    llm_replay_test.py's PROMPT_TEMPLATE, fed the SAME tool_output the
    primary chain already gathered) outperforms the long multi-turn
    production prompt for both comparison models, and needs no
    reasoning_effort tuning to do it -- see wardence_context.md's
    session notes for the full real numbers. Deliberately never reuses
    or modifies react_agent.py's SYSTEM_TEMPLATE/run_react_diagnosis --
    the primary/production path is completely untouched by any of this.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comparison_sampling_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT,
            provider TEXT,
            model TEXT,
            diagnosis TEXT,
            confidence REAL,
            confidence_source TEXT,
            reasoning TEXT,
            response_time_ms REAL,
            completed_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
        )
        """
    )
    conn.commit()


def _compare_and_log(entry: dict, tool_output: dict, episode_id: str | None):
    """
    Fire-and-forget comparison task -- runs in _comparison_executor,
    entirely decoupled from the /diagnose request/response cycle that
    queued it. Single-shot (call_one, not the multi-turn loop): builds
    the same short prompt llm_replay_test.py uses, fed the SAME
    tool_output the primary chain already gathered this episode (no
    redundant real Prometheus query).
    """
    prompt = build_prompt(tool_output)
    t0 = time.perf_counter()
    # Real retry-on-429, 2026-08-05 -- OpenRouter's free gpt-oss-20b:free
    # route has a documented, confirmed-live shared-pool rate-limit
    # flakiness (see reviews/session notes); the accuracy-testing scripts
    # that proved this route's real 100%-of-successful-calls accuracy
    # all retried up to 3x on a 429 -- a bare single attempt here was
    # found live to silently drop data on both real test episodes.
    result = call_one(entry, prompt, timeout=30, episode_id=episode_id)
    retries = 0
    while (
        isinstance(result, LLMFailure)
        and result.failure_type == "rate_limited"
        # Real correction, found live 2026-08-05: a "free-models-per-day"
        # rejection is a DAILY cap (OpenRouter, 50/day, account-wide) --
        # retrying within the same day can NEVER succeed until the real
        # UTC reset, so retrying just burns ~5s/attempt for nothing on
        # every episode once the daily quota is spent. Only retry a
        # genuine short-burst rate limit (no daily-cap signature in the
        # error body), which IS worth a short wait.
        and "free-models-per-day" not in result.detail
        and retries < 3
    ):
        retries += 1
        time.sleep(5)
        result = call_one(entry, prompt, timeout=30, episode_id=episode_id)
    response_time_ms = (time.perf_counter() - t0) * 1000

    if isinstance(result, LLMFailure):
        # NOTE: logger.warning(msg, extra={...}) does NOT print those
        # extra fields under Python's default logging config (a real gap
        # found live, 2026-08-05 -- the terminal only ever showed the
        # bare "comparison call failed" string, no detail). Interpolate
        # directly into the message instead so a real failure is always
        # visible without needing a DB query to diagnose it.
        logger.warning(
            "comparison call failed for %s %s episode=%s (after %d retries): %s",
            entry["provider"], entry["model"], episode_id, retries, result,
        )
        return

    parsed = result.parsed  # call_one() guarantees a valid parsed dict on success, never None here

    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_comparison_sampling_table(conn)
        conn.execute(
            """
            INSERT INTO comparison_sampling_log (
                episode_id, provider, model, diagnosis, confidence,
                confidence_source, reasoning, response_time_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id, result.provider, result.model,
                parsed.get("diagnosis"), result.confidence, result.confidence_source,
                parsed.get("reasoning"), response_time_ms,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("comparison DB write failed", extra={"episode_id": episode_id})


def _openrouter_should_fire(episode_id: str) -> bool:
    """
    Coverage-first gating for OpenRouter's REAL 50 free-model-requests/
    day cap (found live 2026-08-05 -- 'free-models-per-day', account-
    wide, not per-model). Groq isn't gated at all -- its real 1,000/day
    headroom makes this unnecessary there.

    Rule: fire for this episode's real fault class UNLESS (a) that
    class already has >=1 real OpenRouter comparison row logged TODAY
    (UTC, matching the real reset boundary) AND (b) at least one other
    real class still has zero samples today -- i.e. spend the scarce
    daily budget guaranteeing full-roster coverage first (13 real
    classes incl. "none"), then stop gating once every class has been
    sampled at least once, letting the remaining ~37 real slots/day go
    to whichever episodes come up next.

    Reads episodes.fault_class directly -- a real, deliberate, narrow
    exception to this endpoint's usual ground-truth blindness. This
    value is used ONLY to decide whether to fire a comparison-only
    background call; it is never passed into any prompt or allowed to
    influence the real diagnosis/dispatch path, same category of use as
    dispatch_gate.py/misdispatch_guard.py already reading ground truth
    for backend decisions without it ever reaching the model.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        row = conn.execute(
            "SELECT fault_class FROM episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            return True  # no ground truth on record yet -- fail open, don't block on a lookup gap
        actual_class = row[0]

        covered_today = {
            r[0] for r in conn.execute(
                """
                SELECT DISTINCT e.fault_class
                FROM comparison_sampling_log c
                JOIN episodes e ON e.episode_id = c.episode_id
                WHERE c.provider = 'openrouter' AND date(c.completed_at) = date('now')
                """
            ).fetchall()
        }
    finally:
        conn.close()

    if len(covered_today) >= len(FAULT_CLASSES):
        return True  # full roster already covered today -- no more gating, spend freely
    return actual_class not in covered_today

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
    # Real per-call token/Neuron attribution, 2026-08-01 -- optional, not
    # ground truth (unlike ActRequest's actual_class field below), just
    # an identifier so provider_call_log rows can be tied back to a real
    # episode instead of reconstructed via timestamp-window diffing.
    # None means "no attribution" (e.g. a caller other than p3_scorer.py).
    episode_id: str | None = None


class ActRequest(BaseModel):
    target: str
    namespace: str
    predicted: str
    diagnoser_mode_used: str
    confidence: float | None = None
    reasoning: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    # The LLM's OWN raw diagnosis this episode, distinct from `predicted`
    # above -- `predicted` is whichever diagnosis is AUTHORITATIVE for
    # production (the stub's, while diagnoser_mode_used=="stub"). Added
    # 2026-08-05 (real fix, see wardence_buildlog.md's Kimi review 23
    # session) so Dimension C's background action-proposal comparison can
    # run off the LLM's own belief even when it isn't what's driving
    # production -- mirrors Dimension B's own "always compute, only
    # conditionally drive production" split, which C never had until now.
    llm_diagnosis: str | None = None
    llm_confidence: float | None = None
    llm_reasoning: str | None = None
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
        _t0 = time.perf_counter()
        llm_result = run_react_diagnosis(req.target, req.namespace, llm_tools, episode_id=req.episode_id)
        # Real wall-clock duration of the primary chain's own call
        # (2026-08-05, paired with the same field on comparison-sampling
        # rows below) -- lets all 4 models be compared on speed, not
        # just accuracy. Covers the full multi-turn loop for whichever
        # provider actually answered, not a single-turn number.
        llm_result["response_time_ms"] = (time.perf_counter() - _t0) * 1000

        # Comparison-sampling addition, 2026-08-05 -- fire-and-forget,
        # queued on the bounded module-level executor and returns
        # immediately; /diagnose's own response time is unaffected
        # (Kimi review 24's recommended design, reviews/24_..., mechanism
        # since simplified from that review's sketch to a single-shot
        # call reusing tool_output -- see ensure_comparison_sampling_
        # table's docstring for why). Gated behind the same env-var
        # pattern as WARDENCE_STUB_ONLY above -- flip off in .env to
        # fully revert with no code change.
        if os.environ.get("SAMPLE_COMPARISON_MODELS"):
            for entry in COMPARISON_ENTRIES:
                # OpenRouter's real 50/day cap needs coverage-first
                # gating (see _openrouter_should_fire's docstring); Groq
                # has ample headroom and is never gated.
                if entry["provider"] == "openrouter" and not _openrouter_should_fire(req.episode_id):
                    # NOTE: logger.info() is silently swallowed under
                    # Python's default logging config (root level
                    # WARNING) -- same real gap found earlier in this
                    # file. logger.warning() used here so a skip is
                    # always visible, not because this is actually a
                    # warning-severity event.
                    logger.warning(
                        "skipping OpenRouter comparison call for episode=%s -- class already covered today",
                        req.episode_id,
                    )
                    continue
                _comparison_executor.submit(_compare_and_log, entry, tool_output, req.episode_id)

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

    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    ensure_llm_trust_tables(conn)
    ensure_misdispatch_tables(conn)

    # Dimension C background comparison, real fix 2026-08-05 (Kimi review
    # 23, reviews/23_trust_dimension_ABC_redesign_kimi_review.md, plus a
    # follow-up round the same day). This block is DELIBERATELY placed
    # BEFORE the real-dispatch gating below and no longer depends on
    # req.predicted's (the AUTHORITATIVE diagnosis's) own mapping/
    # trust_state passing first -- previously this whole endpoint was
    # only ever CALLED AT ALL (by p3_scorer.py) when the authoritative
    # diagnosis's own class was already can_act, so if the stub said
    # "none" or a report-only class while the LLM privately believed it
    # was a real auto-fix fault, C got zero data that episode. Triggering
    # off the LLM's OWN diagnosis (req.llm_diagnosis) instead mirrors
    # Dimension B's own "always compute in the background, regardless of
    # what's driving production" split, which C never had until now.
    #
    # Real quota-cost gate, added the same session after direct owner
    # pushback: propose_action is a genuine extra LLM call, so this only
    # fires when the LLM's OWN diagnosed class is ALREADY Dimension-A
    # can_act -- Dimension C is explicitly documented as "only
    # meaningful once A=can_act AND B=llm" (llm_trust_state.py's own
    # docstring), so spending real quota proposing an action for a class
    # still stuck in report_only would buy data that could never be
    # used for anything. Checked against the LLM's OWN believed class,
    # never req.predicted's -- these can legitimately differ when B=stub.
    proposal = None
    if req.llm_diagnosis is not None and req.llm_diagnosis in DETERMINISTIC_ACTION_MAP:
        llm_class_trust_state = get_trust_state(conn, req.llm_diagnosis)
        if llm_class_trust_state["state"] == "can_act":
            llm_production_result = {
                "diagnosis": req.llm_diagnosis, "confidence": req.llm_confidence, "reasoning": req.llm_reasoning,
            }
            proposal = propose_action(
                req.llm_diagnosis, llm_production_result, req.target, req.namespace,
                req.llm_provider, req.llm_model, tool_output=req.tool_output,
                episode_id=req.episode_id,
            )
            # Raw, pre-veto/pre-dispatch proposal -- p3_scorer.py's
            # record_llm_trust scores THIS against real ground truth,
            # regardless of what actually got dispatched below (Kimi
            # review 23, Gap 4: scoring the post-veto dispatched tool
            # instead would inflate C's streak with veto overrides it
            # never actually verified).
            response["llm_action_proposal"] = proposal

    # Real dispatch -- gated on the AUTHORITATIVE diagnosis's own
    # mapping/trust_state/safety_hold, unchanged from before this
    # session's restructuring (only the background comparison above
    # became independent of it, not real dispatch).
    mapping = ACTION_MAP.get(req.predicted)
    if mapping is None:
        conn.close()
        return response

    fault_class, action_name, kwargs_fn = mapping
    trust_state = get_trust_state(conn, fault_class)
    action_trust = get_action_trust(conn, fault_class)
    safety_hold = get_safety_hold(conn, fault_class)
    conn.close()

    if trust_state["state"] != "can_act" or safety_hold["active"]:
        return response

    # Dispatch the LLM's own proposal only when production is ACTUALLY
    # LLM-driven this episode (B=llm, so req.predicted == req.llm_diagnosis
    # == fault_class already, meaning `proposal` above was computed for
    # exactly this class) AND Dimension C says trust the LLM's action
    # choice AND the tool-agreement veto passes (2026-08-05, same Kimi
    # review): does the LLM's proposed tool match the deterministic tool
    # for its OWN diagnosis? A mismatch means the LLM chose an
    # inconsistent tool for what it itself just diagnosed -- fall through
    # to the deterministic dispatch below instead of trusting it, without
    # touching Dimension C's own scoring (still scores the raw proposal
    # above, not this veto's outcome). This is additive to, not a
    # replacement for, dispatch_gate.py's existing ground-truth-based
    # redirect below -- that one only works because this lab always
    # knows real ground truth; this veto works even where it doesn't
    # (e.g. a genuine unattended live episode), so both stay.
    if (req.diagnoser_mode_used == LLM and action_trust["state"] == LLM_CAN_ACT
            and proposal is not None and proposal.get("tool_name") is not None):
        tool_agreement_ok = proposal["tool_name"] == action_name
        # Real gap found and fixed 2026-08-06, live: this veto used to
        # ONLY check tool-NAME agreement, never the proposed PARAMS --
        # meaning a correctly-diagnosed episode from an already-trusted
        # class could dispatch a genuinely unsafe magnitude (confirmed
        # live twice: oom proposed 8Mi, real dispatch attempted and only
        # failed because Kubernetes' own admission rules happened to
        # reject it as invalid -- pure luck, not a real safeguard;
        # under-provisioned-replicas proposed 2, which DID dispatch for
        # real with no check at all, since K8s has no equivalent built-in
        # floor on replica count). dispatch_gate.check() below never
        # catches this either -- it only fires on a WRONG diagnosis
        # (predicted != actual), and both these episodes were correctly
        # diagnosed. check_safe() is the same function Dimension C's
        # scoring already trusts -- now also gates the REAL dispatch,
        # not just the score. Uses req.predicted, never actual_class --
        # this is the real production path, which must stay blinded to
        # ground truth same as everywhere else (and must work even in a
        # genuine unattended live episode with no ground truth at all).
        params_safe = True
        safety_reason = "tool has no magnitude parameter -- trivially safe"
        if tool_agreement_ok:
            params_safe, safety_reason = check_safe(
                proposal["tool_name"], proposal["params"], req.predicted, req.tool_output,
            )
        if tool_agreement_ok and params_safe:
            action_taken, action_result, action_source, gate_substitution = _dispatch_with_gate(
                req.predicted, proposal["tool_name"], proposal["params"], proposal["source"], req,
            )
            response["action_taken"] = action_taken
            response["action_result"] = action_result
            response["action_source"] = action_source
            response["gate_substitution"] = gate_substitution
            return response
        # else: falls through to the deterministic dispatch below --
        # tool-agreement veto and/or the new params-safety veto fired,
        # C's scoring above is unaffected (still scores the raw
        # pre-veto proposal, never this veto's outcome).

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
