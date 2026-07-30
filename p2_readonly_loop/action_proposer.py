"""
Phase H step 3, Phase 2: real single-shot action proposal.

Built after Kimi review 12 (reviews/12_action_proposal_safety_kimi_review.md),
synthesis agreed with the user (2026-07-30): bounded retry/escalation, not
unbounded model-cascading, plus a deterministic safe-default fallback.

Deliberately a SEPARATE module from react_agent.py -- Kimi's structural
gap #3: react_agent.py's own docstring promises zero import of
trust_engine/actions.py (the comparison-only guarantee for Phase 1's
diagnosis loop). tool_call_validator.py imports actions.py, so Phase 2
logic can never live inside react_agent.py without breaking that
guarantee. This module is the one P3 (p3_agent.py) may eventually
import for real dispatch wiring; P2's read-only agent.py never should.

Bounded design (replaces review 10's original "reject -> immediate
report_only" AND the earlier-discussed "cascade the whole 7-provider
chain" idea, both reconsidered after Kimi's adversarial pass):
  1. Ask the SAME provider that succeeded at diagnosis. If its proposal
     fails tool_call_validator, retry that SAME provider once more with
     the rejection reason fed back (same pattern as the evidence loop's
     own parse-failure retry). Real risk this doesn't eliminate, per
     Kimi (b): a deterministic (temperature=0) model with a genuine
     format misunderstanding can fail identically twice -- accepted,
     bounded to 2 real calls, not chased further on this provider.
  2. If BOTH attempts on that provider fail validation, escalate to
     EXACTLY ONE more provider (the chain entry immediately after the
     diagnosis provider's own index) for a single fresh attempt -- NOT
     the whole remaining chain (Kimi's structural gap d4: an unbounded
     cascade is a real multi-minute worst case for a live "Trigger Fix"
     button). If that action validates, it's tagged with THAT
     provider's real tier for the caller's own promotion-streak
     accounting -- action-tier, tracked separately from diagnosis-tier
     (Kimi's structural gap d2: the two can differ).
  3. If that escalated attempt ALSO fails validation, fall back to the
     REAL, already-in-production deterministic action
     (p3_agent.py's own ACTION_MAP/FIX_PARAMS, duplicated by hand below
     -- see the dict's own docstring for why) -- Kimi's Alternative 2.
     Not a new, untested action; the exact action the stub-diagnosed
     production path already dispatches successfully today. Tagged
     tier="deterministic_fallback", which is not an LLM tier at all --
     never LLM-attributed data, and not a regression either (it's
     exactly what already happens today without any of this).

Real cost bound: at most 3 real LLM calls per action proposal (2 on the
first provider + 1 escalated), versus an unbounded worst case if every
provider in the chain were retried twice.

COMPARISON-ONLY, same discipline as react_agent.py, until the 150-
episode-per-class floor clears (see trust_gate.py): this module NEVER
calls actions.ALLOWED_ACTIONS for real. It returns a plain dict for the
caller to log; nothing here dispatches a live Kubernetes action yet.
"""
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

from model_backend import PROVIDER_CHAIN, _extract_json, call_one, LLMFailure  # noqa: E402
from tool_call_validator import validate_tool_call  # noqa: E402
from trust_engine import DB_PATH  # noqa: E402

# Real, already-in-production deterministic mapping (p3_trust_action's
# p3_agent.py's own ACTION_MAP/FIX_PARAMS) -- duplicated by hand here
# (same convention as react_agent.py's FAULT_CLASSES/FIELD_GUIDANCE)
# rather than importing p3_agent.py directly, since that file has real
# import-order complexity of its own (loads P2's agent.py via importlib
# to dodge a filename collision) this module shouldn't have to inherit
# just to read two constant dicts. Keep in sync by hand if either
# changes in p3_agent.py.
DETERMINISTIC_FIX_PARAMS = {
    "oom": {"container": "catalogue", "limit": "400Mi"},
    "disk-full": {"replicas": 1},
    "cpu-throttling": {"container": "user", "limit": "600m"},
    "under-provisioned-replicas": {"replicas": 3},
    "bad-rollout": {},
}

DETERMINISTIC_ACTION_MAP = {
    "crash-loop": ("restart_deployment", lambda t, n: {"name": t, "namespace": n}),
    "oom": ("patch_memory_limit", lambda t, n: {"name": t, "namespace": n, **DETERMINISTIC_FIX_PARAMS["oom"]}),
    "disk-full": ("restore_from_disk_full", lambda t, n: {"name": t, "namespace": n, **DETERMINISTIC_FIX_PARAMS["disk-full"]}),
    "cpu-throttling": ("patch_cpu_limit", lambda t, n: {"name": t, "namespace": n, **DETERMINISTIC_FIX_PARAMS["cpu-throttling"]}),
    "under-provisioned-replicas": ("scale_deployment", lambda t, n: {"name": t, "namespace": n, **DETERMINISTIC_FIX_PARAMS["under-provisioned-replicas"]}),
    "bad-rollout": ("rollback_deployment", lambda t, n: {"name": t, "namespace": n}),
}

# Schema-in-prompt (Kimi's Alternative 1, adopted unconditionally --
# reduces validator rejections happening at all, independent of the
# retry/escalation bound above).
ACTION_SCHEMA_TEXT = """Available actions and their EXACT real parameter formats:
- restart_deployment: {"tool": "restart_deployment", "params": {"name": "<deployment name>"}}
- patch_memory_limit: {"tool": "patch_memory_limit", "params": {"name": "...", "container": "...", "limit": "<quantity>"}}
  Valid limit examples: "256Mi", "400Mi", "1Gi" (Ki/Mi/Gi/Ti suffix only, no "m").
- patch_cpu_limit: {"tool": "patch_cpu_limit", "params": {"name": "...", "container": "...", "limit": "<quantity>"}}
  Valid limit examples: "300m", "600m", "1" (millicores with "m" suffix, or whole cores as a bare number).
- scale_deployment: {"tool": "scale_deployment", "params": {"name": "...", "replicas": <int, 1-10>}}
- rollback_deployment: {"tool": "rollback_deployment", "params": {"name": "..."}}
- restore_from_disk_full: {"tool": "restore_from_disk_full", "params": {"name": "...", "replicas": <int, 1-10>}}"""

PROMPT_TEMPLATE = """You are an SRE agent. You have already diagnosed the following real fault:
diagnosis={diagnosis}, target={target}, namespace={namespace}, confidence={confidence}, reasoning="{reasoning}"

{schema}

Propose EXACTLY ONE fix action for this specific fault. Respond with ONLY one JSON object, no markdown, no other text:
{{"tool": "<tool name>", "params": {{...}}, "reasoning": "<one sentence>"}}{feedback}"""


def _propose_once(entry: dict, diagnosis: dict, target: str, namespace: str, feedback: str = "") -> dict:
    prompt = PROMPT_TEMPLATE.format(
        diagnosis=diagnosis["diagnosis"], target=target, namespace=namespace,
        confidence=diagnosis.get("confidence"), reasoning=diagnosis.get("reasoning", ""),
        schema=ACTION_SCHEMA_TEXT, feedback=feedback,
    )
    result = call_one(entry, prompt, timeout=30)
    if isinstance(result, LLMFailure):
        return {"outcome": "provider_failure", "detail": result.__dict__}

    parsed = result.parsed or _extract_json(result.text)
    if not parsed or "tool" not in parsed:
        return {"outcome": "parse_failure", "detail": "no valid {tool, params} JSON in response", "raw": result.text[:300]}

    tool_name, params = parsed.get("tool"), parsed.get("params", {})
    ok, reason = validate_tool_call(tool_name, params)
    if not ok:
        return {
            "outcome": "validator_rejected", "tool_name": tool_name, "params": params, "reason": reason,
            "provider": result.provider, "model": result.model, "tier": result.tier,
        }

    return {
        "outcome": "validated", "tool_name": tool_name, "params": params,
        "provider": result.provider, "model": result.model, "tier": result.tier,
        "reasoning": parsed.get("reasoning"),
    }


def _find_chain_entry(provider: str, model: str) -> Optional[dict]:
    for entry in PROVIDER_CHAIN:
        if entry["provider"] == provider and entry["model"] == model:
            return entry
    return None


def propose_action(fault_class: str, diagnosis: dict, target: str, namespace: str,
                    diagnosis_provider: str, diagnosis_model: str) -> dict:
    """
    fault_class: the LLM's OWN diagnosed class (not ground truth -- this
    proposes an action for whatever the loop just diagnosed, same
    blinding discipline as the rest of this project).
    diagnosis: the dict run_react_diagnosis() returned (needs
    "diagnosis"/"confidence"/"reasoning").
    diagnosis_provider/diagnosis_model: from that SAME result -- used to
    locate where in PROVIDER_CHAIN to start (Kimi's structural gap d1:
    never blindly restart from chain[0]).

    Returns a dict, always with "source" in {"llm_primary_retry",
    "llm_escalated", "deterministic_fallback", "not_auto_fix_class"}.
    COMPARISON-ONLY -- never calls actions.ALLOWED_ACTIONS for real.
    """
    if fault_class not in DETERMINISTIC_ACTION_MAP:
        return {"source": "not_auto_fix_class", "fault_class": fault_class}

    diagnosis_entry = _find_chain_entry(diagnosis_provider, diagnosis_model)
    attempts = []

    if diagnosis_entry is not None:
        # Attempt 1 + 1 retry, SAME provider that succeeded at diagnosis.
        attempt = _propose_once(diagnosis_entry, diagnosis, target, namespace)
        attempts.append(attempt)
        if attempt["outcome"] == "validated":
            return {**attempt, "source": "llm_primary_retry", "fault_class": fault_class, "attempts": attempts}

        if attempt["outcome"] == "validator_rejected":
            feedback = f'\nYour previous attempt was rejected: {attempt["reason"]}. Correct it and try again.'
            attempt2 = _propose_once(diagnosis_entry, diagnosis, target, namespace, feedback=feedback)
            attempts.append(attempt2)
            if attempt2["outcome"] == "validated":
                return {**attempt2, "source": "llm_primary_retry", "fault_class": fault_class, "attempts": attempts}

        # Escalate to exactly ONE more provider (the next entry after the
        # diagnosis provider's own index) -- never the whole remaining chain.
        diag_idx = PROVIDER_CHAIN.index(diagnosis_entry)
        if diag_idx < len(PROVIDER_CHAIN) - 1:
            escalated_entry = PROVIDER_CHAIN[diag_idx + 1]
            attempt3 = _propose_once(escalated_entry, diagnosis, target, namespace)
            attempts.append(attempt3)
            if attempt3["outcome"] == "validated":
                return {**attempt3, "source": "llm_escalated", "fault_class": fault_class, "attempts": attempts}

    # Deterministic safe-default fallback (Kimi's Alternative 2) -- the
    # SAME action the stub-diagnosed production path already uses for
    # this class, not a new untested action. Also the path taken if
    # diagnosis_provider/model couldn't be matched back to a real chain
    # entry at all (shouldn't happen, but never a reason to skip
    # proposing a real action for an auto-fix class).
    tool_name, params_fn = DETERMINISTIC_ACTION_MAP[fault_class]
    return {
        "source": "deterministic_fallback", "fault_class": fault_class, "tool_name": tool_name,
        "params": params_fn(target, namespace), "provider": None, "model": None,
        "tier": "deterministic_fallback", "attempts": attempts,
    }


def ensure_action_proposal_log_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_action_proposal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT NOT NULL,
            fault_class TEXT NOT NULL,
            source TEXT NOT NULL,
            tool_name TEXT,
            params_json TEXT,
            provider TEXT,
            model TEXT,
            tier TEXT,
            matches_deterministic INTEGER,
            attempts_json TEXT,
            logged_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
        )
        """
    )
    conn.commit()


def matches_deterministic_action(fault_class: str, tool_name: str, params: dict, target: str, namespace: str) -> Optional[bool]:
    """None if fault_class isn't an auto-fix class at all (nothing to compare against)."""
    if fault_class not in DETERMINISTIC_ACTION_MAP:
        return None
    det_tool, det_params_fn = DETERMINISTIC_ACTION_MAP[fault_class]
    return tool_name == det_tool and params == det_params_fn(target, namespace)


def log_proposal(episode_id: str, target: str, namespace: str, proposal: dict):
    """COMPARISON-ONLY logging -- writes to llm_action_proposal_log only,
    never scores/trust_state. Safe to call regardless of tier/floor."""
    conn = sqlite3.connect(DB_PATH)
    ensure_action_proposal_log_table(conn)
    fault_class = proposal.get("fault_class")
    tool_name, params = proposal.get("tool_name"), proposal.get("params")
    matches = (
        matches_deterministic_action(fault_class, tool_name, params, target, namespace)
        if tool_name is not None else None
    )
    conn.execute(
        """
        INSERT INTO llm_action_proposal_log (
            episode_id, fault_class, source, tool_name, params_json,
            provider, model, tier, matches_deterministic, attempts_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode_id, fault_class, proposal.get("source"), tool_name,
            json.dumps(params) if params is not None else None,
            proposal.get("provider"), proposal.get("model"), proposal.get("tier"),
            int(matches) if matches is not None else None,
            json.dumps(proposal.get("attempts", []), default=lambda o: getattr(o, "__dict__", str(o))),
        ),
    )
    conn.commit()
    conn.close()
