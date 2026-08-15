"""
Phase E -- apply the reviewed keep/reclassify/exclude decisions to the
scores table, with a full audit trail (nothing overwritten silently).

Adds 4 new nullable columns to `scores` (idempotent, safe to run against a
table that already has them):
    phase_e_status           TEXT    -- 'reclassified' | 'excluded' | NULL
    phase_e_note             TEXT    -- the reason, human-readable
    original_correct         INTEGER -- pre-correction value, only set when reclassified
    original_trust_correct   INTEGER -- pre-correction value, only set when reclassified

RECLASSIFY: the diagnosis/action was genuinely correct; only the scoring
verdict was wrong, due to a proven, since-fixed system bug (durability
check / pod-resolution / diagnosis window-or-index bug). correct and
trust_correct (where the episode was can_act) are flipped to 1.
Trust_history/trust_state are NOT touched -- the demotion/streak-reset
really happened and is a true historical fact, only the aggregate
accuracy reporting is corrected.

EXCLUDE: the ground truth itself was wrong (mislabeled 'none' control) or
unsalvageable -- correct/trust_correct are left as originally recorded,
but phase_e_status='excluded' lets downstream aggregate queries
(publish_to_r2.py) skip the episode entirely rather than count it either way.

Dry-run by default -- prints exactly what would change. Pass --apply to
actually write.

Run from WSL2:
    source ~/wardence_venv/bin/activate
    cd /mnt/c/Users/HP/Wardence/p3_trust_action
    python3 phase_e_apply_corrections.py            # dry run
    python3 phase_e_apply_corrections.py --apply    # actually writes
"""
import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

# episode_id -> reason. Only episodes where an action was genuinely
# applied (action_applied=1) get trust_correct flipped too -- the two
# pure-misdiagnosis cases (cpu-throttling, oom) had no action, so
# trust_correct stays untouched (was already NULL for report_only-style
# misdiagnoses, or not applicable).
RECLASSIFY = {
    "26fe8e42-405c-4fdf-9524-b80b649e4b79": "bad-rollout durability-check bug (stale Prometheus / Pending-phase exclusion in verifier.py's _front_end_image_pull_failing_live, Investigation 1) -- the real rollback fix applied cleanly, only the durability verdict was wrong. Fixed and verified 8/8 clean, 2026-07-27.",
    "570b281b-468a-4db3-b3d6-45a27d11daee": "bad-rollout durability-check bug, same as above.",
    "b6e000d9-34d1-4548-9ce0-ac828b412cb8": "bad-rollout durability-check bug, same as above.",
    "70c2ab7a-3b82-4f80-a0b5-ad5776f372f1": "bad-rollout durability-check bug, same as above (second fix attempt, still-open Pending-phase gap, closed 2026-07-27).",
    "1d1f4f9f-c36d-4860-8142-4021eda893fd": "bad-rollout durability-check bug, same as above.",
    "7a4bdccf-054d-4edc-91c8-77ea6c08b434": "bad-rollout durability-check bug, same as above.",
    "68f561b6-a504-4c66-9827-c622f1b62ef2": "crash-loop pod-resolution bug (_current_pod_name_live resolved the stale/dying pod instead of the new one, shared function also used by oom/disk-full/cpu-throttling). Fixed and verified 8/8 clean, 2026-07-27.",
    "a0199c9d-8fb5-4d88-8ec3-f8bed862075a": "crash-loop pod-resolution bug, same as above.",
    "115c1552-bfcc-46d6-9db6-de1f24e04e68": "crash-loop pod-resolution bug, same as above.",
    "8557c678-547a-4c48-8089-fa24d6543d42": "crash-loop pod-resolution bug, same as above.",
    "cd6d21ff-5670-4c5e-944e-93a5e4dc3195": "crash-loop pod-resolution bug, same as above.",
    "65338c04-b042-4910-ae25-6fff9b62e2c9": "crash-loop pod-resolution bug, same as above.",
    "bc54bd30-a8d4-4563-b987-77b22213ddaa": "cpu-throttling diagnosis bug in agent.py (narrow [2m] lookback window + [0]-indexing into an unaggregated Prometheus result picked the wrong/dead pod's series). Confirmed via forced reproduction: same failure shape reproduced then came back correct 4/4 after both fixes landed. Investigation 3, fixed 2026-07-27.",
    "b45e6f61-7b79-4bc9-9125-524cd72bdbc6": "oom diagnosis bug in agent.py ([3m] window too narrow for a reset-rollout-delayed real event). Fixed (widened to [6m]) and verified 5/5 clean, 2026-07-27.",
    "46eb0b18-34d8-4346-b493-261521e5e8f1": "under-provisioned-replicas diagnosis bug -- agent's capacity-probe call had no retry, unlike injector.py's own established retry pattern, so a known-flaky probe returning null read as 'no anomaly'. Fixed (3-attempt retry) and verified 5/5 clean, 2026-07-27.",
    "cb8b46ee-c35b-4f07-add1-11610dcfa5b2": "under-provisioned-replicas diagnosis bug, same as above.",
    "ffcfae61-e72c-4a8e-97d7-cc02d3588c23": "crash-loop's very first can_act fix attempt ever (2026-07-20). verifier.py originally matched pods by name PREFIX, letting a just-terminated old pod's stale restart count contaminate the post-fix baseline and cause a false 'flapped' verdict. Fixed same day by resolving the exact current pod name once and matching exactly on it (documented in wardence_buildlog.md's original P3 build section). The real restart_deployment fix applied cleanly; only the durability verdict was wrong.",
    "52916c3e-efe1-4ecf-ad8a-7d096c58e898": "under-provisioned-replicas' very first can_act fix attempt ever (2026-07-24). durability_elapsed_s=20 -- flapped almost instantly, the classic 'checked before the fix actually took effect' signature. Matches the already-documented 'under-provisioned-replicas premature-durability-check bug' referenced in Investigation 1's writeup (fixed same session -- the class went on to hold a clean 19/19 streak in the original Phase D run). The real scale_deployment fix applied cleanly; only the durability verdict was wrong.",
    "a0e27f54-18cf-41ca-b9fb-4ad642d1a93d": "network-partition diagnosis bug in operator_api.py -- snapshot_at (t0 + SETTLE_SECONDS) was computed as a fixed 35s offset from injection start regardless of live-trigger hold duration, but this class's min_over_time-for-a-drop signal depends on the real ~30-40s iptables propagation delay documented in injector.py -- the fixed 35s settle landed right at that boundary, sometimes before the block was clean. The real fault was genuinely injected and verified (evidence_confirmed=1), only the diagnosis-timing window was wrong. Fixed 2026-08-1x with a per-class SETTLE_SECONDS_OVERRIDE (network-partition: 60s). NOTE: re-verified on only 1 clean re-test so far, not the 4/4 bar used for the cpu-throttling precedent below -- flagged here so this isn't read as more thoroughly re-proven than it actually is.",
}

EXCLUDE = {
    "34a51bc2-a818-4319-8ace-ddaba5ab5e5f": "Mislabeled 'none' control -- prior real disk-full episode on queue-master had no fix action applied (P2 was diagnosis-only, no fix actions existed yet), only ~187s before this control was scored, and another real disk-full episode followed 8 minutes later. Predates phase_d_run.py's recency guard entirely (this was the original 4-class run_systematic_validation.py script, 2026-07-20).",
    "7e516b34-3d53-42e7-8b48-ab486c364b14": "Mislabeled 'none' control -- prior real crash-loop episode on carts started only 12 seconds before this control's t0; crash-loop's injection mechanism runs recurring kills for a 40s window, so this control was very likely scored WHILE the real fault was still actively firing. Predates phase_d_run.py's recency guard entirely (2026-07-20).",
    "a901327d-3207-44d6-a349-f76ccf989718": "Mislabeled 'none' control -- front-end was genuinely still broken (bad-rollout's injector has no self-revert on a successful report-only injection, and nothing in the pipeline ever fixed it while report_only). The agent's diagnosis was CORRECT; the control's own ground-truth label was wrong. Investigation 2, phase_d_run.py's pick_healthy_none_target fixed 2026-07-25.",
    "1f7db472-c567-4849-b74d-5551e1bc1b39": "Mislabeled 'none' control, same as above.",
    "ccad4a97-d230-4815-8336-2b166c063b9c": "Mislabeled 'none' control -- catalogue's capacity probe (203.83ms) reflected genuine ambient load from 3 other real fixes completing on different targets in the ~90s immediately prior, not noise on an idle system. A real scale_deployment action was applied against a healthy target as a result. phase_d_run.py's none-control picker only checked per-target recency, not system-wide -- fixed 2026-07-27 (wait_for_system_quiet).",
    # Found via phase_e_check_none_contamination.py (2026-07-27): 8 more
    # 'none' controls from the 2026-07-20/07-21 P2/early-P3 era, all
    # scored a "no anomaly detected" -- CORRECT verdict, but the ground
    # truth is questionable for the same reason as 34a51bc2/7e516b34
    # above: each one's most recent real fault on the SAME target had
    # action_taken=None (nothing ever fixed it -- P2 was diagnosis-only,
    # no fix actions existed yet), and the gap before the control was
    # scored was under the 300s recency threshold this project later
    # adopted (2026-07-24+). These correctly scored 'correct' by
    # coincidence (the stub diagnoser happened to say "no anomaly"), but
    # the control's own ground truth ("target is healthy") is not
    # trustworthy for the same reason as the already-excluded pair.
    "5dc353b0-c78d-476f-a017-2fe00488f5c3": "Ground-truth-questionable 'none' control -- prior real crash-loop episode on carts had no fix action applied, only 11s before this control was scored. Same mechanism as 7e516b34 above, found via systematic contamination check.",
    "136afe3f-96a0-4847-8847-c7f849624658": "Ground-truth-questionable 'none' control -- prior real disk-full episode on queue-master had no fix action applied, only 160s before this control was scored. Same mechanism as 34a51bc2 above, found via systematic contamination check.",
    "b75be496-f255-4436-9189-88924d344566": "Ground-truth-questionable 'none' control -- prior real oom episode on catalogue had no fix action applied, only 160s before this control was scored. Found via systematic contamination check.",
    "18f3ba0a-a605-4a96-9eca-fa37b0d1e129": "Ground-truth-questionable 'none' control -- prior real oom episode on catalogue had no fix action applied, only 189s before this control was scored. Found via systematic contamination check.",
    "51d644dc-7587-442c-910d-97eb8abead72": "Ground-truth-questionable 'none' control -- prior real disk-full episode on queue-master had no fix action applied, only 240s before this control was scored. Found via systematic contamination check.",
    "8190dcfe-9177-4995-bcf1-20fc0d188e49": "Ground-truth-questionable 'none' control -- prior real disk-full episode on queue-master had no fix action applied, only 190s before this control was scored. Found via systematic contamination check.",
    "3320d222-aef4-4718-88ef-7cc6fc653ffc": "Ground-truth-questionable 'none' control -- prior real crash-loop episode on carts had no fix action applied, only 189s before this control was scored. Found via systematic contamination check.",
    "151fcd9f-ab57-4dc0-b43d-7cd6ccb383a9": "Ground-truth-questionable 'none' control -- prior real oom episode on catalogue had no fix action applied, only 251s before this control was scored. Found via systematic contamination check.",
}


def ensure_phase_e_columns(conn: sqlite3.Connection):
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
    new_cols = {
        "phase_e_status": "TEXT",
        "phase_e_note": "TEXT",
        "original_correct": "INTEGER",
        "original_trust_correct": "INTEGER",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE scores ADD COLUMN {col} {col_type}")
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually write changes (default: dry run)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_phase_e_columns(conn)

    print("=" * 78)
    print(f"Phase E corrections -- {'APPLYING' if args.apply else 'DRY RUN (pass --apply to write)'}")
    print("=" * 78)

    print(f"\n--- RECLASSIFY ({len(RECLASSIFY)} episodes) ---")
    for ep_id, reason in RECLASSIFY.items():
        row = conn.execute(
            "SELECT correct, trust_correct, action_applied, phase_e_status FROM scores WHERE episode_id = ?",
            (ep_id,),
        ).fetchone()
        if row is None:
            print(f"  SKIP {ep_id}: not found in scores table")
            continue
        if row["phase_e_status"] is not None:
            print(f"  SKIP {ep_id}: already has phase_e_status={row['phase_e_status']!r}, not touching again")
            continue
        new_trust_correct = 1 if row["action_applied"] else row["trust_correct"]
        print(f"  {ep_id}: correct {row['correct']} -> 1, trust_correct {row['trust_correct']} -> {new_trust_correct}")
        print(f"      reason: {reason}")
        if args.apply:
            conn.execute(
                """
                UPDATE scores
                SET original_correct = ?, original_trust_correct = ?,
                    correct = 1, trust_correct = ?,
                    phase_e_status = 'reclassified', phase_e_note = ?
                WHERE episode_id = ?
                """,
                (row["correct"], row["trust_correct"], new_trust_correct, reason, ep_id),
            )

    print(f"\n--- EXCLUDE ({len(EXCLUDE)} episodes) ---")
    for ep_id, reason in EXCLUDE.items():
        row = conn.execute(
            "SELECT correct, phase_e_status FROM scores WHERE episode_id = ?", (ep_id,)
        ).fetchone()
        if row is None:
            print(f"  SKIP {ep_id}: not found in scores table")
            continue
        if row["phase_e_status"] is not None:
            print(f"  SKIP {ep_id}: already has phase_e_status={row['phase_e_status']!r}, not touching again")
            continue
        print(f"  {ep_id}: marked excluded (correct value {row['correct']} left as-is, just tagged)")
        print(f"      reason: {reason}")
        if args.apply:
            conn.execute(
                "UPDATE scores SET phase_e_status = 'excluded', phase_e_note = ? WHERE episode_id = ?",
                (reason, ep_id),
            )

    if args.apply:
        conn.commit()
        print("\nCommitted.")
    else:
        print("\nDry run only -- nothing written. Re-run with --apply to commit these changes.")

    conn.close()


if __name__ == "__main__":
    main()
