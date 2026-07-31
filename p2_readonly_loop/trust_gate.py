"""
Phase H step 4 (rewritten): tier gating for the three-dimension trust
structure (Dimensions B/C, see p3_trust_action/llm_trust_state.py).

REAL_LLM_EPISODE_FLOOR is GONE -- superseded 2026-07-30/31, see
wardence_context.md's Model Strategy section, "THE 150-EPISODE FLOOR
IS DROPPED", for the full reasoning. Dimension C's own 5-consecutive-
correct requirement already provides the gate the floor used to
provide against a small/lucky sample; a volume floor on top of it was
found to be redundant, not a second independent layer of protection.

What's still real and unchanged from the original design (Kimi review
09 item 4, review 12's structural finding): model-tier gating. An
episode diagnosed or actioned by anything other than the primary
(top-of-chain) provider does NOT count toward either dimension's
promotion streak -- it still earns correctness credit as data (logged
to llm_diagnosis_log / llm_action_proposal_log either way), it just
never gets passed to llm_trust_state.record_diagnoser_outcome /
record_action_outcome. Real reasoning, unchanged: a fallback-tier
model's wrong tool call on an auto-fix class has real blast radius
even inside the RBAC cage; a wrong report-only diagnosis does not.

New in this rewrite: action_is_correct(), the exact-match-vs-tool-only
split Kimi review 13 required (see llm_trust_state.py's
MAGNITUDE_SENSITIVE_ACTIONS) -- the actual "was this action correct"
determination Dimension C's streak needs, which didn't exist anywhere
before now. Diagnosis correctness is a simple string-equality-after-
normalization check the scorer already does (llm_replay_test.py's
_same_diagnosis) -- no new helper needed for that side.

Callers (p3_scorer.py, step 3 of the punch list): call
eligible_for_trust_ladder() before ever calling record_diagnoser_outcome,
and eligible_action_for_streak() + action_is_correct() before ever
calling record_action_outcome. Nothing calls this yet for real --
p3_agent.py's /handle still only calls stub_diagnose (step 3 of the
punch list wires the rest in).
"""


def eligible_for_trust_ladder(diagnosis_tier: str) -> tuple[bool, str]:
    """
    DIAGNOSIS-tier eligibility. Returns (eligible, reason). Only rule
    left: diagnosis_tier must be "primary" -- fallback-tier diagnoses
    never count toward Dimension B's promotion streak, regardless of
    whether they were correct.
    """
    if diagnosis_tier != "primary":
        return False, f"diagnosis tier={diagnosis_tier!r}, not 'primary' -- fallback-tier diagnoses never count toward the diagnoser-mode streak"
    return True, "diagnosis tier='primary' -- eligible"


def eligible_action_for_streak(action_tier: str) -> tuple[bool, str]:
    """
    ACTION-tier eligibility -- a SEPARATE check from
    eligible_for_trust_ladder, per Kimi review 12's structural gap:
    action_proposer.py's escalation path (or its deterministic_fallback
    path) can produce an action_tier that differs from the
    diagnosis_tier that got this episode past the diagnosis-tier check
    in the first place. Both gates must independently pass "primary"
    before an action's outcome contributes to Dimension C's streak.

    Note: "deterministic_fallback" is not an LLM tier at all (it's
    action_proposer.py's own safety-net path when both LLM attempts
    fail) -- it correctly fails this check too, since a deterministic
    fallback proves nothing about the LLM's own action-selection
    reliability.
    """
    if action_tier != "primary":
        return False, f"action tier={action_tier!r}, not 'primary' -- fallback-tier/deterministic-fallback actions never count toward the action-trust streak"
    return True, "action tier='primary' -- eligible"


def action_is_correct(fault_class: str, tool_name: str, params: dict,
                       ground_truth_tool: str, ground_truth_params: dict) -> tuple[bool, str]:
    """
    Determines whether a proposed action counts as "correct" for
    Dimension C's streak, per Kimi review 13's real, verified gap:
    "same tool regardless of parameter value" is genuinely unsafe for
    the three magnitude-sensitive actions specifically (a
    validator-passing but absurd 4Gi/4000m/replicas=20 could
    destabilize this lab's thin real headroom while still earning
    streak credit under a looser bar).

    - patch_memory_limit / patch_cpu_limit / scale_deployment: EXACT
      match required on both tool name and every parameter value
      (including the magnitude-bearing one -- "limit"/"replicas").
    - restart_deployment / rollback_deployment / restore_from_disk_full:
      tool-name match only -- no magnitude parameter to abuse, and
      these three's other params (name/namespace) are just addressing
      the same already-known target, not a safety-relevant choice.

    ground_truth_tool/ground_truth_params: the deterministic mapping's
    own real answer for this fault_class (action_proposer.py's
    DETERMINISTIC_ACTION_MAP), NOT the actual production ACTION_MAP --
    comparison-only, same blinding discipline as the rest of Phase H.
    """
    from llm_trust_state import MAGNITUDE_SENSITIVE_ACTIONS  # local import: p3_trust_action on sys.path only when needed

    if tool_name != ground_truth_tool:
        return False, f"tool mismatch: proposed {tool_name!r}, expected {ground_truth_tool!r}"

    if tool_name not in MAGNITUDE_SENSITIVE_ACTIONS:
        return True, f"tool-only match required for {tool_name!r} -- tool name matches, no magnitude parameter to check"

    # CORRECTED 2026-07-31, real bug caught live testing oom's Dimension
    # C promotion: "namespace" is never part of what the LLM is asked to
    # supply -- action_proposer.py's own ACTION_SCHEMA_TEXT (the exact
    # prompt shown to the model) doesn't list "namespace" as a param for
    # ANY of the 6 tools. ground_truth_params always includes it anyway
    # (DETERMINISTIC_ACTION_MAP's lambdas build {"name": t, "namespace":
    # n, ...} unconditionally), so checking every ground-truth key
    # verbatim meant EVERY real magnitude-sensitive proposal would fail
    # on a field the model was structurally never given a chance to
    # supply -- confirmed live: a real, correct-tool, real-value
    # proposal ({"name": "catalogue", "container": "catalogue", "limit":
    # "512Mi"}) got flagged as a namespace mismatch instead of the real,
    # intended signal (512Mi vs. production's 400Mi). Exclude keys the
    # LLM was never asked for from the exact-match check -- it only
    # penalizes what the model actually had a chance to get right.
    _NOT_ASKED_OF_LLM = {"namespace"}
    for key, expected in ground_truth_params.items():
        if key in _NOT_ASKED_OF_LLM:
            continue
        actual = params.get(key)
        if actual != expected:
            return False, f"magnitude-sensitive param mismatch on {key!r}: proposed {actual!r}, expected {expected!r} (exact match required for {tool_name!r})"

    return True, f"exact match on all magnitude-sensitive params for {tool_name!r}"


if __name__ == "__main__":
    # Quick sanity check, no args needed -- exercises both branches of
    # action_is_correct() against real DETERMINISTIC_ACTION_MAP shapes.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

    cases = [
        ("oom", "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"},
         "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"}, True),
        ("oom", "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "4Gi"},
         "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"}, False),
        ("crash-loop", "restart_deployment", {"name": "front-end", "namespace": "sock-shop"},
         "restart_deployment", {"name": "front-end", "namespace": "sock-shop"}, True),
        ("under-provisioned-replicas", "scale_deployment", {"name": "catalogue", "namespace": "sock-shop", "replicas": 20},
         "scale_deployment", {"name": "catalogue", "namespace": "sock-shop", "replicas": 3}, False),
    ]
    for fault_class, tool, params, gt_tool, gt_params, expected in cases:
        ok, reason = action_is_correct(fault_class, tool, params, gt_tool, gt_params)
        status = "PASS" if ok == expected else "FAIL"
        print(f"[{status}] {fault_class:30s} expected={expected!s:5s} got={ok!s:5s}  {reason}")

    print()
    print("Tier gate checks:")
    for tier in ("primary", "fallback", "deterministic_fallback"):
        print(f"  diagnosis tier={tier!r}: {eligible_for_trust_ladder(tier)}")
        print(f"  action tier={tier!r}: {eligible_action_for_streak(tier)}")
