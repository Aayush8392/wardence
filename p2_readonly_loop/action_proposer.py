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

This module itself NEVER calls actions.ALLOWED_ACTIONS -- it returns a
plain dict (tool_name/params/source/tier) for the caller to either log
only, or dispatch for real via ALLOWED_ACTIONS itself.

CORRECTED 2026-07-30/31: the 150-episode-per-class floor this used to
be gated on is DROPPED (see wardence_context.md's Model Strategy
section). p3_agent.py's /handle now dispatches this module's proposal
for real when Dimension C (action_trust, see
p3_trust_action/llm_trust_state.py) is "llm_can_act" for the fault
class -- earned via 5 consecutive both-diagnosis-and-action-correct
episodes, revoked instantly on one miss. Below that state, this
module's output is still computed (Dimension C's streak needs the
data) but only logged, never dispatched -- the deterministic mapping
above is what actually runs instead.
"""
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

from model_backend import PROVIDER_CHAIN, _extract_json, call_one, LLMFailure  # noqa: E402
from tool_call_validator import MAX_CPU_LIMIT_M, MAX_MEMORY_LIMIT_MI, MAX_REPLICAS, validate_tool_call  # noqa: E402
from trust_engine import DB_PATH  # noqa: E402
from constraint_checks import _live_replica_count  # noqa: E402

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

# Real, checked-against-source constraint, 2026-08-01: under-provisioned-
# replicas' magnitude signal is NOT a passive tool_output field the way
# oom/cpu-throttling's are -- it's catalogue_probe_p95_ms, produced by an
# ACTIVE probe (agent.py's probe_catalogue_capacity), and its real current-
# replica-count constraint is read live via kubectl at scoring time
# (constraint_checks.py), never passed in the prompt at all. So the
# "null field -> spiral" failure mode this whole redesign is fixing
# structurally CANNOT happen for this class -- no null-fallback needed,
# unlike oom/cpu-throttling below.
#
# Real review-18 resolution (Kimi, 2026-08-01, same chat): hardcoding
# tool selection is safe ONLY for magnitude-sensitive classes, where
# Dimension C already tests PARAMETER judgment via constraint_checks.
# check_safe(), not tool choice -- for the 3 non-magnitude classes
# (restart_deployment/rollback_deployment/restore_from_disk_full),
# tool-only-match IS the entire Dimension C signal, so their schema
# stays open below, not hardcoded.
MAGNITUDE_SENSITIVE_NULL_FALLBACK = {
    "oom": ("patch_memory_limit", "peak_memory_mib"),
    "cpu-throttling": ("patch_cpu_limit", "cpu_throttle_periods_increase"),
}

# Schema-in-prompt (Kimi's Alternative 1, adopted unconditionally --
# reduces validator rejections happening at all, independent of the
# retry/escalation bound above). Caps here are the REAL, already-locked
# constants tool_call_validator.py enforces server-side (MAX_MEMORY_LIMIT_MI/
# MAX_CPU_LIMIT_M/MAX_REPLICAS) -- not new numbers, reused so the prompt
# never asks for something the validator would reject anyway (the old
# "1-10" replicas range here was stale against the real 1-20 cap).
ACTION_SCHEMA_TEXT = f"""Available actions and their EXACT real parameter formats:
- restart_deployment: {{"tool": "restart_deployment", "params": {{"name": "<deployment name>"}}}}
- patch_memory_limit: {{"tool": "patch_memory_limit", "params": {{"name": "...", "container": "...", "limit": "<quantity>"}}}}
  Valid limit examples: "256Mi", "512Mi", "1Gi" (Ki/Mi/Gi/Ti suffix only, no "m"). Must be > the real observed peak_memory_mib and <= {MAX_MEMORY_LIMIT_MI}Mi.
- patch_cpu_limit: {{"tool": "patch_cpu_limit", "params": {{"name": "...", "container": "...", "limit": "<quantity>"}}}}
  Valid limit examples: "300m", "600m", "1" (millicores with "m" suffix, or whole cores as a bare number). Must be <= {MAX_CPU_LIMIT_M}m.
- scale_deployment: {{"tool": "scale_deployment", "params": {{"name": "...", "replicas": <int, 1-{MAX_REPLICAS}>}}}}
  Must be STRICTLY GREATER than the real current_replicas value given in the input data below (never equal to or less than it), and <= {MAX_REPLICAS}.
- rollback_deployment: {{"tool": "rollback_deployment", "params": {{"name": "..."}}}}
- restore_from_disk_full: {{"tool": "restore_from_disk_full", "params": {{"name": "..."}}}}"""

# Fixed per call (no per-episode substitution) -- the rules/schema/
# examples never change, only the real episode data below in
# USER_PROMPT_TEMPLATE does. Split out per Kimi review 18 suggestion #6:
# Cloudflare's endpoint is OpenAI-compatible and supports a real system
# role; keeping fixed instructions there and only variable data in the
# user message is meant to increase formatting compliance.
SYSTEM_PROMPT = f"""You are an SRE agent proposing exactly one fix action for an already-diagnosed real fault. Respond with ONLY one JSON object, no markdown, no other text, no visible reasoning process:
{{"tool": "<tool name>", "params": {{...}}, "reasoning": "<one brief sentence>"}}

{ACTION_SCHEMA_TEXT}

Each diagnosis maps to EXACTLY ONE correct tool -- you MUST use that exact tool, never substitute a different one, and do not reconsider the diagnosis (it is already confirmed correct):
- crash-loop -> restart_deployment
- oom -> patch_memory_limit
- disk-full -> restore_from_disk_full
- cpu-throttling -> patch_cpu_limit
- under-provisioned-replicas -> scale_deployment
- bad-rollout -> rollback_deployment
EXCEPTION: if the specific real metric needed to size a magnitude-sensitive action is null/missing in the observed data below (peak_memory_mib for oom, cpu_throttle_periods_increase for cpu-throttling), do NOT attempt that action at all -- use restart_deployment instead. This is a direct instruction, not something to reason your way to -- do not deliberate between options when the required data is simply absent.
If the observed data contains signals that could suggest a different diagnosis than the one given, IGNORE them -- the diagnosis is already confirmed correct, do not re-evaluate it.

Examples (illustrative values, not real production defaults -- compute your own answer from the real data given to you, don't copy these numbers):
Input: diagnosis="oom", peak_memory_mib=290
Output: {{"tool": "patch_memory_limit", "params": {{"name": "catalogue", "container": "catalogue", "limit": "512Mi"}}, "reasoning": "512Mi provides headroom above the observed 290Mi peak."}}

Input: diagnosis="oom", peak_memory_mib=null
Output: {{"tool": "restart_deployment", "params": {{"name": "catalogue"}}, "reasoning": "No peak memory data available, magnitude-sensitive fix not possible."}}

Input: diagnosis="cpu-throttling", cpu_throttle_periods_increase=340
Output: {{"tool": "patch_cpu_limit", "params": {{"name": "user", "container": "user", "limit": "800m"}}, "reasoning": "Limit raised above the observed throttling pressure."}}

Input: diagnosis="under-provisioned-replicas", current_replicas=3
Output: {{"tool": "scale_deployment", "params": {{"name": "catalogue", "replicas": 5}}, "reasoning": "Scaled above the real current count of 3 to add real headroom."}}

Input: diagnosis="crash-loop", target="carts"
Output: {{"tool": "restart_deployment", "params": {{"name": "carts"}}, "reasoning": "Crash-loop is resolved by a clean restart."}}

Input: diagnosis="bad-rollout", target="front-end"
Output: {{"tool": "rollback_deployment", "params": {{"name": "front-end"}}, "reasoning": "A bad rollout is resolved by rolling back to the previous known-good revision, not a restart."}}

Input: diagnosis="disk-full", target="queue-master"
Output: {{"tool": "restore_from_disk_full", "params": {{"name": "queue-master"}}, "reasoning": "Disk-full requires the dedicated restore action, not a plain restart."}}"""

USER_PROMPT_TEMPLATE = """Diagnosed fault: diagnosis={diagnosis}, target={target}, namespace={namespace}, confidence={confidence}, reasoning="{reasoning}"

Real observed data at diagnosis time:
{tool_output}

Propose exactly one fix action for this fault.{feedback}"""


def _propose_once(entry: dict, diagnosis: dict, target: str, namespace: str, fault_class: str,
                   tool_output: dict | None = None, feedback: str = "",
                   episode_id: str | None = None) -> dict:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        diagnosis=diagnosis["diagnosis"], target=target, namespace=namespace,
        confidence=diagnosis.get("confidence"), reasoning=diagnosis.get("reasoning", ""),
        tool_output=json.dumps(tool_output, default=str) if tool_output else "(not available)",
        feedback=feedback,
    )
    result = call_one(entry, user_prompt, timeout=30, system_prompt=SYSTEM_PROMPT, episode_id=episode_id)
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

    # Real semantic self-consistency check, added 2026-08-03 after disk-full's
    # tool-mismatch demotion (a schema-valid but WRONG tool -- restart_deployment
    # instead of restore_from_disk_full -- sailed through as "validated" with zero
    # chance to retry, since validate_tool_call only checks shape/safety, never
    # whether the tool actually matches the diagnosis it was proposed for).
    # Generic across every auto-fix class via DETERMINISTIC_ACTION_MAP -- any
    # future class added there automatically inherits this check, no per-class
    # special-casing needed. Deliberately reuses the "validator_rejected" outcome
    # contract (not a new outcome type) so it flows through propose_action's
    # existing same-provider-retry-with-feedback path for free.
    expected_entry = DETERMINISTIC_ACTION_MAP.get(fault_class)
    if expected_entry is not None and tool_name != expected_entry[0]:
        return {
            "outcome": "validator_rejected", "tool_name": tool_name, "params": params,
            "reason": f"tool mismatch: diagnosis {fault_class!r} requires tool {expected_entry[0]!r}, not {tool_name!r}",
            "provider": result.provider, "model": result.model, "tier": result.tier,
        }

    if tool_name == "restore_from_disk_full":
        # Real fix, 2026-08-03: replicas here was never a genuine capacity
        # judgment call the way scale_deployment's is -- it's a fixed,
        # already-validated recovery target (scale-to-0 -> wait -> scale-
        # to-1), and check_safe() never validated it (its own docstring:
        # "only meaningful for the three magnitude-sensitive actions"), so
        # an LLM-proposed replicas value here was previously unchecked and
        # unenforced. Removed from the LLM's decision space entirely --
        # forced to the same real, tested value the deterministic fallback
        # already uses, never left to the model to guess.
        params = {**params, **DETERMINISTIC_FIX_PARAMS["disk-full"]}

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
                    diagnosis_provider: str, diagnosis_model: str, tool_output: dict | None = None,
                    episode_id: str | None = None) -> dict:
    """
    fault_class: the LLM's OWN diagnosed class (not ground truth -- this
    proposes an action for whatever the loop just diagnosed, same
    blinding discipline as the rest of this project).
    diagnosis: the dict run_react_diagnosis() returned (needs
    "diagnosis"/"confidence"/"reasoning").
    diagnosis_provider/diagnosis_model: from that SAME result -- used to
    locate where in PROVIDER_CHAIN to start (Kimi's structural gap d1:
    never blindly restart from chain[0]).
    tool_output: the REAL observed metrics from /diagnose's own
    query_prometheus call (2026-07-31 addition, real bug found live-
    testing: without this, the model has no actual numbers to size a
    magnitude parameter from and defaults to a generic/typical-looking
    value instead of reasoning from this container's real headroom --
    confirmed live, oom proposed 512Mi twice in a row against a real
    tested-safe 400Mi, with zero access to the real peak_memory_mib
    that would have let it reason about it). Optional/None-safe so this
    doesn't become a hard dependency for any future caller that doesn't
    have it handy.

    Returns a dict, always with "source" in {"llm_primary_retry",
    "llm_escalated", "deterministic_fallback", "not_auto_fix_class"}.
    COMPARISON-ONLY -- never calls actions.ALLOWED_ACTIONS for real.
    """
    if fault_class not in DETERMINISTIC_ACTION_MAP:
        return {"source": "not_auto_fix_class", "fault_class": fault_class}

    # Real bug found 2026-08-03: unlike oom/cpu-throttling, under-
    # provisioned-replicas' magnitude constraint (scale_deployment must
    # propose STRICTLY MORE than the real live replica count, per
    # constraint_checks.py's check_safe) was never surfaced into the
    # prompt at all -- the model was asked to satisfy a constraint it
    # had zero visibility into, and had no way to do so reliably.
    # Confirmed live: every real proposal across multiple batches
    # guessed 2 or 3 replicas (a plausible-looking small number with no
    # grounding) against a real current count of 3, failing
    # constraint-satisfaction every time. Surfaced here the same way
    # peak_memory_mib/cpu_throttle_periods_increase already ground
    # oom/cpu-throttling's proposals -- read live (same helper
    # check_safe itself uses at validation time), merged into the
    # tool_output dict the model actually sees. If the live read fails
    # (current_replicas ends up null in the prompt), check_safe already
    # fails closed on a missing value -- no new unsafe path opened here.
    if fault_class == "under-provisioned-replicas":
        tool_output = {**(tool_output or {}), "current_replicas": _live_replica_count(target, namespace)}

    diagnosis_entry = _find_chain_entry(diagnosis_provider, diagnosis_model)
    attempts = []

    if diagnosis_entry is not None:
        # Attempt 1 + 1 retry, SAME provider that succeeded at diagnosis.
        attempt = _propose_once(diagnosis_entry, diagnosis, target, namespace, fault_class, tool_output=tool_output, episode_id=episode_id)
        attempts.append(attempt)
        if attempt["outcome"] == "validated":
            return {**attempt, "source": "llm_primary_retry", "fault_class": fault_class, "attempts": attempts}

        if attempt["outcome"] == "validator_rejected":
            feedback = f'\nYour previous attempt was rejected: {attempt["reason"]}. Correct it and try again.'
            attempt2 = _propose_once(diagnosis_entry, diagnosis, target, namespace, fault_class, tool_output=tool_output, feedback=feedback, episode_id=episode_id)
            attempts.append(attempt2)
            if attempt2["outcome"] == "validated":
                return {**attempt2, "source": "llm_primary_retry", "fault_class": fault_class, "attempts": attempts}

        # Escalate to exactly ONE more provider (the next entry after the
        # diagnosis provider's own index) -- never the whole remaining chain.
        diag_idx = PROVIDER_CHAIN.index(diagnosis_entry)
        if diag_idx < len(PROVIDER_CHAIN) - 1:
            escalated_entry = PROVIDER_CHAIN[diag_idx + 1]
            attempt3 = _propose_once(escalated_entry, diagnosis, target, namespace, fault_class, tool_output=tool_output, episode_id=episode_id)
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
