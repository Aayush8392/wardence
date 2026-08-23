#!/usr/bin/env python3
# One-time bootstrap for a genuinely fresh wardence.db -- calls every real
# ensure_*_table function across the codebase against a single connection,
# so a fresh deployment doesn't discover each missing table one at a time
# as different code paths get exercised for the first time (hit for real,
# twice, on wardence-prod 2026-08-2x: `scores`/`episode_snapshots` first,
# then `comparison_sampling_log` separately -- see wardence_buildlog.md's
# matching sessions).
#
# Safe to rerun -- every ensure_* function is CREATE TABLE IF NOT EXISTS
# (idempotent), same as running injector.ensure_db() alone always was.
#
# Usage (from the repo root, with the venv activated):
#   python3 deploy/bootstrap_fresh_db.py

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "p2_readonly_loop"))
sys.path.insert(0, str(REPO_ROOT / "p3_trust_action"))

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"


def main():
    DB_PATH.parent.mkdir(exist_ok=True)

    # episodes table + column migrations -- must run first, everything
    # else assumes `episodes` already exists.
    import injector
    injector.ensure_db()

    conn = sqlite3.connect(DB_PATH)

    import scorer as p2_scorer
    import action_proposer
    import llm_replay_test
    import quota_tracker

    import accounts
    import circuit_breaker
    import dispatch_gate
    import llm_trust_state
    import misdispatch_guard
    import trust_engine
    import p3_scorer
    import p3_agent

    ensure_calls = [
        ("p2_scorer.ensure_scores_table", p2_scorer.ensure_scores_table),
        ("action_proposer.ensure_action_proposal_log_table", action_proposer.ensure_action_proposal_log_table),
        ("llm_replay_test.ensure_llm_diagnosis_log_table", llm_replay_test.ensure_llm_diagnosis_log_table),
        ("quota_tracker.ensure_quota_table", quota_tracker.ensure_quota_table),
        ("quota_tracker.ensure_exhaustion_table", quota_tracker.ensure_exhaustion_table),
        ("quota_tracker.ensure_call_log_table", quota_tracker.ensure_call_log_table),
        ("accounts.ensure_accounts_tables", accounts.ensure_accounts_tables),
        ("circuit_breaker.ensure_circuit_breaker_table", circuit_breaker.ensure_circuit_breaker_table),
        ("dispatch_gate.ensure_gate_tables", dispatch_gate.ensure_gate_tables),
        ("llm_trust_state.ensure_llm_trust_tables", llm_trust_state.ensure_llm_trust_tables),
        ("misdispatch_guard.ensure_misdispatch_tables", misdispatch_guard.ensure_misdispatch_tables),
        ("trust_engine.ensure_trust_tables", trust_engine.ensure_trust_tables),
        ("p3_scorer.ensure_scores_table", p3_scorer.ensure_scores_table),
        ("p3_scorer.ensure_episode_snapshots_table", p3_scorer.ensure_episode_snapshots_table),
        ("p3_agent.ensure_comparison_sampling_table", p3_agent.ensure_comparison_sampling_table),
    ]

    for name, fn in ensure_calls:
        fn(conn)
        print(f"  ok: {name}")

    conn.commit()
    conn.close()
    print(f"\nDone. All known tables ensured on {DB_PATH}.")


if __name__ == "__main__":
    main()
