"""
Dispatch gate -- Kimi review 16, built 2026-07-31 after an explicit
design deviation from Kimi's own sketch (confirmed with the user first,
see reviews/16_dispatch_gate_kimi_review.md's tail for the discussion).

What this is: when the diagnosed class is WRONG, the agent is trusted
to act (can_act) on the class it (wrongly) diagnosed, and the action it
would dispatch is actually dangerous for the REAL fault -- redirect the
real kubectl dispatch to the REAL fault's own deterministic fix instead
of letting the wrong one execute. The diagnosis is still scored as
wrong (Dimension A/B/C all unaffected by this module) -- this only
changes what hits the cluster, never what gets measured.

DEVIATION FROM KIMI'S LITERAL DESIGN, deliberate: Kimi's sketch had
p3_agent.py's /act look up ground truth itself via a DB query keyed on
episode_id. Rejected in favor of the caller (p3_scorer.py, the ONLY
piece in this system that's ever allowed to see ground truth) passing
actual_class directly as a plain string. This keeps the gate a pure,
DB-free function -- no new ground-truth DB access inside the agent
service itself, and trivially unit-testable without any test-DB
isolation concern (nothing here ever opens a database connection).

Fire condition (review 16, corrected mid-review after the user caught a
real gap in Kimi's first pass -- same-tool-wrong-magnitude is a real
hazard too, not just cross-tool):
  Fires when predicted_class != actual_class AND EITHER:
    (a) proposed_tool != the real actual-class fix's tool ("cross-tool"), OR
    (b) proposed_tool == actual-class fix's tool, but the tool is
        magnitude-sensitive AND the proposed params fail
        constraint_checks.check_safe() for the REAL actual_class.
  Never fires when the agent said report-only (no proposed action at
  all -- injecting an action the agent never proposed would be acting
  on its behalf, a bigger claim than intended). Never fires when the
  proposed tool matches the real fix AND (not magnitude-sensitive, or
  magnitude-sensitive and passes constraint-satisfaction) -- nothing to
  correct.

What "actual-class fix" means: the REAL production ACTION_MAP entry
for actual_class (p3_agent.py's own mapping, the same one the existing
"deterministic production dispatch" path already uses unvalidated,
since it's hardcoded/pre-vetted -- not action_proposer.py's
DETERMINISTIC_ACTION_MAP, which is comparison-only by design and must
never drive real dispatch).

Ground truth (actual_class) is a parameter to check(), nothing more --
never logged anywhere the diagnosing/proposing agent could read it back
in a future call, never passed to propose_action()/run_react_diagnosis().
"""

import sqlite3
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "p3_trust_action"))
from constraint_checks import check_safe  # noqa: E402

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"


def ensure_gate_tables(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gate_intervention_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT,
            predicted_class TEXT NOT NULL,
            actual_class TEXT NOT NULL,
            proposed_tool TEXT NOT NULL,
            substituted_tool TEXT NOT NULL,
            reason TEXT NOT NULL,
            intervened_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def check(action_map: dict, predicted_class: str, actual_class: str,
          proposed_tool: str, proposed_params: dict, target: str, namespace: str,
          tool_output: "dict | None") -> dict:
    """
    Pure decision function -- no DB access, no side effects. Returns:
      {"substituted": False} -- dispatch the agent's own proposal as-is.
      {"substituted": True, "actual_tool": str, "actual_params": dict,
       "reason": str} -- dispatch the actual-class fix instead.

    action_map: p3_agent.py's real ACTION_MAP (class -> (fault_class,
    action_name, kwargs_fn)) -- passed in rather than imported, so this
    module has zero import-time dependency on p3_agent.py (avoids a
    circular import, same reasoning this project already applies
    elsewhere for FAULT_CLASSES/FIELD_GUIDANCE-style duplication).
    """
    if predicted_class == actual_class:
        return {"substituted": False}

    actual_mapping = action_map.get(actual_class)
    if actual_mapping is None:
        # actual_class has no auto-fix mapping at all (e.g. a
        # report-only-only class) -- nothing to redirect TO, so there's
        # no real fix to substitute in. Leave the agent's own proposal
        # alone; Dimension A/B already handle "wrong diagnosis" scoring
        # regardless of what dispatches.
        return {"substituted": False}

    _, actual_tool, actual_kwargs_fn = actual_mapping
    actual_params = actual_kwargs_fn(target, namespace)

    if proposed_tool != actual_tool:
        return {
            "substituted": True,
            "actual_tool": actual_tool,
            "actual_params": actual_params,
            "reason": f"cross-tool misdispatch: predicted {predicted_class!r} (tool "
                      f"{proposed_tool!r}), actual is {actual_class!r} (needs {actual_tool!r})",
        }

    # Same tool -- only a real hazard if it's magnitude-sensitive AND
    # the proposed value fails constraint-satisfaction for the REAL
    # class. check_safe() with tool_name outside the three
    # magnitude-sensitive actions always returns (True, ...) trivially.
    safe, safety_reason = check_safe(proposed_tool, proposed_params, actual_class, tool_output)
    if not safe:
        return {
            "substituted": True,
            "actual_tool": actual_tool,
            "actual_params": actual_params,
            "reason": f"same tool ({proposed_tool!r}) but unsafe magnitude for actual class "
                      f"{actual_class!r}: {safety_reason}",
        }

    return {"substituted": False}


def log_intervention(conn: sqlite3.Connection, episode_id: "str | None", predicted_class: str,
                      actual_class: str, proposed_tool: str, substituted_tool: str, reason: str):
    ensure_gate_tables(conn)
    conn.execute(
        """
        INSERT INTO gate_intervention_log
            (episode_id, predicted_class, actual_class, proposed_tool, substituted_tool, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (episode_id, predicted_class, actual_class, proposed_tool, substituted_tool, reason),
    )
    conn.commit()
