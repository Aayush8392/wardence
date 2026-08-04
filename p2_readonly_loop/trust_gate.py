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
                       ground_truth_tool: str, ground_truth_params: dict,
                       tool_output: "dict | None" = None) -> tuple[bool, str]:
    """
    Determines whether a proposed action counts as "correct" for
    Dimension C's streak.

    REWRITTEN 2026-07-31 (review 14's conclusion, finally built):
    exact-match on magnitude-sensitive params was the original fix for
    Kimi review 13's real gap ("same tool regardless of parameter value"
    is unsafe for patch_memory_limit/patch_cpu_limit/scale_deployment),
    but review 14 concluded exact-match is too narrow -- it penalizes a
    safe-but-different value (450Mi vs. the hardcoded 400Mi constant)
    as if it were dangerous. Replaced with constraint_checks.check_safe()
    -- same real physical-safety bounds now shared with the dispatch
    gate (review 16), so "correct" here means "safe for the real fault
    class," not "matches one specific historical constant."

    - patch_memory_limit / patch_cpu_limit / scale_deployment: tool name
      must match AND the proposed value must pass check_safe() against
      the REAL fault_class (this function's fault_class param IS the
      ground truth -- callers must never pass a predicted/diagnosed
      class here).
    - restart_deployment / rollback_deployment / restore_from_disk_full:
      tool-name match only -- no magnitude parameter to abuse.

    ground_truth_tool: the deterministic mapping's own real answer for
    this fault_class (action_proposer.py's DETERMINISTIC_ACTION_MAP),
    NOT the actual production ACTION_MAP -- comparison-only, same
    blinding discipline as the rest of Phase H. ground_truth_params is
    no longer used for magnitude comparison (kept in the signature for
    caller compatibility). tool_output: the episode's real
    query_prometheus result (needed for patch_memory_limit's
    peak_memory_mib bound) -- callers must pass this explicitly;
    omitting it makes any patch_memory_limit proposal fail closed
    (constraint_checks.check_safe treats missing peak_memory_mib as
    unverifiable, not safe-by-default).
    """
    from llm_trust_state import MAGNITUDE_SENSITIVE_ACTIONS  # local import: p3_trust_action on sys.path only when needed
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "p3_trust_action"))
    from constraint_checks import check_safe

    if tool_name != ground_truth_tool:
        return False, f"tool mismatch: proposed {tool_name!r}, expected {ground_truth_tool!r}"

    if tool_name not in MAGNITUDE_SENSITIVE_ACTIONS:
        return True, f"tool-only match required for {tool_name!r} -- tool name matches, no magnitude parameter to check"

    # BUG FIXED 2026-08-06: this used to overwrite the real `tool_output`
    # parameter (passed in explicitly by every caller, per the docstring
    # above) with `ground_truth_params.get("_tool_output")` -- a key
    # DETERMINISTIC_ACTION_MAP's params-builder lambdas never set, so it
    # was always None. check_safe() therefore always saw tool_output=None
    # for patch_memory_limit and failed closed on every single oom
    # episode ("no real peak_memory_mib available"), even when the real
    # tool_output the caller passed in had a genuine, non-null peak
    # reading. Just use the real parameter directly.
    safe, reason = check_safe(tool_name, params, fault_class, tool_output)
    if not safe:
        return False, f"constraint-satisfaction failed for {tool_name!r}: {reason}"
    return True, f"constraint-satisfaction passed for {tool_name!r}: {reason}"


if __name__ == "__main__":
    # Quick sanity check, no args needed -- exercises both branches of
    # action_is_correct() against real DETERMINISTIC_ACTION_MAP shapes.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

    # tool_output passed explicitly for the two patch_memory_limit cases
    # below -- required since the 2026-08-06 fix (see the comment above
    # action_is_correct's check_safe() call): omitting it now correctly
    # fails closed (no real peak_memory_mib to verify safety against),
    # so a self-test claiming a magnitude proposal is "safe" must supply
    # the real peak reading it's safe relative to, same as every real
    # caller (p3_scorer.py) already does.
    cases = [
        ("oom", "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"},
         "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"}, True,
         {"peak_memory_mib": 50}),
        ("oom", "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "4Gi"},
         "patch_memory_limit", {"name": "catalogue", "namespace": "sock-shop", "container": "catalogue", "limit": "400Mi"}, False,
         {"peak_memory_mib": 50}),
        ("crash-loop", "restart_deployment", {"name": "front-end", "namespace": "sock-shop"},
         "restart_deployment", {"name": "front-end", "namespace": "sock-shop"}, True, None),
        # Real bounds check, not just fail-closed-on-missing-data: current=3
        # (real diagnosis-time baseline), proposed=2 is NOT > current -- the
        # exact real shape of the UPR bug fixed 2026-08-06 (constraint_checks.py
        # used to compare against a live-re-read POST-dispatch value instead
        # of this snapshot).
        ("under-provisioned-replicas", "scale_deployment", {"name": "catalogue", "namespace": "sock-shop", "replicas": 2},
         "scale_deployment", {"name": "catalogue", "namespace": "sock-shop", "replicas": 3}, False,
         {"current_replicas": 3}),
        # And the genuine safe case: current=1 (real fault-time baseline),
        # proposed=3 IS > current and <= MAX_REPLICAS(20).
        ("under-provisioned-replicas", "scale_deployment", {"name": "catalogue", "namespace": "sock-shop", "replicas": 3},
         "scale_deployment", {"name": "catalogue", "namespace": "sock-shop", "replicas": 3}, True,
         {"current_replicas": 1}),
    ]
    for fault_class, tool, params, gt_tool, gt_params, expected, tool_output in cases:
        ok, reason = action_is_correct(fault_class, tool, params, gt_tool, gt_params, tool_output=tool_output)
        status = "PASS" if ok == expected else "FAIL"
        print(f"[{status}] {fault_class:30s} expected={expected!s:5s} got={ok!s:5s}  {reason}")

    print()
    print("Tier gate checks:")
    for tier in ("primary", "fallback", "deterministic_fallback"):
        print(f"  diagnosis tier={tier!r}: {eligible_for_trust_ladder(tier)}")
        print(f"  action tier={tier!r}: {eligible_action_for_streak(tier)}")
