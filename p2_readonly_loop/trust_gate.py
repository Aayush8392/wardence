"""
Phase H step 4: tier gating + the 150-episode-per-class floor, combined
into a single eligibility check.

Two SEPARATE locked rules, both must pass:
1. Model-tier gating (wardence_context.md's Model Strategy, Kimi review
   09 item 4): an episode diagnosed by anything other than the primary
   (top-of-chain) provider does NOT count toward an auto-fix class's
   promotion streak -- it can still earn correctness credit as data,
   just not contribute to can_act promotion. Real reasoning: a
   fallback-tier model's wrong tool call on an auto-fix class has real
   blast radius even inside the RBAC cage; a wrong report-only
   diagnosis does not.
2. The 150-episode-per-class floor (locked 2026-07-30, this session,
   direct user instruction): the floor is per-class REAL LLM-DIAGNOSED
   volume specifically -- NOT total recorded episodes for that class
   regardless of diagnoser. A class like network-latency already has
   176+ real episodes, but ALL of them are stub-diagnosed; none of that
   volume counts toward this floor. The reasoning stated directly by
   the user: the whole point of wiring the LLM early was to start
   accumulating REAL LLM-diagnosed episodes rather than resting on
   stub-diagnosed volume, so reusing the stub's existing volume here
   would undermine that entire premise.

Nothing calls this yet to actually gate a real action or a real
trust_engine.record_outcome call -- there is no real wiring from the
ReAct loop into ACTION_MAP/trust_engine at all right now (see
react_agent.py's own docstring: comparison-only, by construction, no
import of trust_engine/actions.py). This module exists so that wiring,
whenever it happens, has one real, already-agreed-upon check to call
rather than reinventing the two rules above at that point.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

from trust_engine import DB_PATH  # noqa: E402

REAL_LLM_EPISODE_FLOOR = 150


def real_llm_episode_count(fault_class: str) -> int:
    """
    Count of REAL, successfully-diagnosed LLM episodes recorded for this
    class in llm_diagnosis_log -- status='diagnosed' only (an
    invalid_tool_name/max_turns_exceeded/llm_unavailable episode never
    produced a real diagnosis, so it doesn't count toward the floor).
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*) FROM llm_diagnosis_log WHERE actual_class = ? AND llm_diagnosis IS NOT NULL",
        (fault_class,),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def eligible_for_trust_ladder(fault_class: str, diagnosis_tier: str) -> tuple[bool, str]:
    """
    DIAGNOSIS-tier eligibility. Returns (eligible, reason). Both locked
    rules must pass:
      - diagnosis_tier must be "primary" (fallback-tier diagnoses never
        count, regardless of episode volume).
      - the class must have >= REAL_LLM_EPISODE_FLOOR real LLM-diagnosed
        episodes already recorded (NOT counting this current one -- the
        floor must be cleared BEFORE an episode can count, not on the
        episode that happens to cross it).

    Real gap found and fixed via Kimi review 12 (2026-07-30): this used
    to be the ONLY tier check in the file, silently assuming diagnosis-
    tier and action-tier are always the same thing. They aren't --
    action_proposer.py can escalate to a DIFFERENT provider than the one
    that succeeded at diagnosis. See eligible_action_for_streak below
    for the separate, action-specific check.
    """
    if diagnosis_tier != "primary":
        return False, f"diagnosis tier={diagnosis_tier!r}, not 'primary' -- fallback-tier diagnoses never count toward the trust ladder"

    count = real_llm_episode_count(fault_class)
    if count < REAL_LLM_EPISODE_FLOOR:
        return False, (
            f"{fault_class} has {count}/{REAL_LLM_EPISODE_FLOOR} real LLM-diagnosed episodes -- "
            f"below the floor, comparison-only until cleared"
        )
    return True, f"{fault_class} has {count}/{REAL_LLM_EPISODE_FLOOR} real LLM-diagnosed episodes -- eligible"


def eligible_action_for_streak(fault_class: str, action_tier: str) -> tuple[bool, str]:
    """
    ACTION-tier eligibility -- a SEPARATE check from eligible_for_trust_ladder
    above, per Kimi review 12's structural gap: action_proposer.py's
    escalation path (or its deterministic_fallback path) can produce an
    action_tier that differs from the diagnosis_tier that got this
    episode past eligible_for_trust_ladder in the first place. Both
    gates must independently pass "primary" before an action's OUTCOME
    contributes to the promotion streak -- a validated escalated-tier or
    deterministic_fallback action can still be dispatched for real (once
    real dispatch is wired) and still earns correctness credit as data,
    it just never counts toward can_act promotion, same reasoning as
    the diagnosis-tier rule.
    """
    if action_tier != "primary":
        return False, f"action tier={action_tier!r}, not 'primary' -- fallback-tier/deterministic-fallback actions never count toward the promotion streak"
    return True, "action tier='primary' -- eligible (still subject to eligible_for_trust_ladder's own diagnosis-tier + floor check)"


if __name__ == "__main__":
    # Quick real-data status check across the roster, no args needed.
    from react_agent import FAULT_CLASSES

    for fc in FAULT_CLASSES:
        count = real_llm_episode_count(fc)
        status = "CLEARED" if count >= REAL_LLM_EPISODE_FLOOR else "below floor"
        print(f"{fc:35s} {count:4d}/{REAL_LLM_EPISODE_FLOOR}  ({status})")
