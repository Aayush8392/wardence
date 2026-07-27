"""
Phase E -- systematic check: for EVERY 'none' control episode ever recorded
(not just the 5 already known wrong-verdict ones), how close was the most
recent real episode on the SAME target before it? Read-only, no writes.

This exists because the 2 07-20 false positives (34a51bc2, 7e516b34) came
from a script (the old 4-class run_systematic_validation.py, predating
phase_d_run.py) that had NO recency/quiet guard at all -- so there could be
other 'none' episodes from that same era that happened to score correct by
coincidence (predicted 'no anomaly detected' even though the target was
genuinely still disturbed), which wouldn't show up as a 'wrong' episode in
scores.correct but would still mean the control's ground truth was
questionable. This script flags by real timing evidence, not by whether the
diagnosis happened to come out right.

Run from WSL2:
    source ~/wardence_venv/bin/activate
    cd /mnt/c/Users/HP/Wardence/p2_readonly_loop
    python3 phase_e_check_none_contamination.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"

OVERLAP_RISK_S = 60   # likely still mid-injection for recurring-kill-style classes
RESIDUAL_RISK_S = 300  # matches TARGET_RECENCY_WINDOW_S, the current live guard's threshold

ALREADY_EXCLUDED = {
    "a901327d-3207-44d6-a349-f76ccf989718",
    "1f7db472-c567-4849-b74d-5551e1bc1b39",
    "ccad4a97-d230-4815-8336-2b166c063b9c",
    "34a51bc2-a818-4319-8ace-ddaba5ab5e5f",
    "7e516b34-3d53-42e7-8b48-ab486c364b14",
}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    none_eps = conn.execute(
        """
        SELECT s.episode_id, s.predicted_class, s.correct, s.scored_at, e.target
        FROM scores s
        LEFT JOIN episodes e ON e.episode_id = s.episode_id
        WHERE s.actual_class = 'none'
        ORDER BY s.scored_at
        """
    ).fetchall()

    print("=" * 100)
    print(f"Checking {len(none_eps)} 'none' control episodes for target-proximity contamination")
    print("=" * 100)
    print(f"\n{'scored_at':20s} {'target':12s} {'predicted':26s} {'correct':7s} {'gap_s':>9s}  {'prior_class':22s} {'prior_action':16s}  flag")
    print("-" * 130)

    overlap = []
    residual = []
    for ep in none_eps:
        prior = conn.execute(
            """
            SELECT s.episode_id, s.actual_class, s.action_taken, s.action_applied, s.scored_at
            FROM scores s
            LEFT JOIN episodes e ON e.episode_id = s.episode_id
            WHERE e.target = ? AND s.scored_at < ?
            ORDER BY s.scored_at DESC
            LIMIT 1
            """,
            (ep["target"], ep["scored_at"]),
        ).fetchone()

        if prior is None:
            gap_s = None
            flag = "CLEAR (no prior episode on this target)"
            prior_class = "-"
            prior_action = "-"
        else:
            gap_row = conn.execute(
                "SELECT (julianday(?) - julianday(?)) * 86400.0", (ep["scored_at"], prior["scored_at"])
            ).fetchone()
            gap_s = gap_row[0]
            prior_class = prior["actual_class"]
            prior_action = f"{prior['action_taken']}/{prior['action_applied']}"
            if gap_s < OVERLAP_RISK_S:
                flag = "OVERLAP_RISK"
                overlap.append((ep, prior, gap_s))
            elif gap_s < RESIDUAL_RISK_S:
                flag = "RESIDUAL_RISK"
                residual.append((ep, prior, gap_s))
            else:
                flag = "CLEAR"

        already = "  [ALREADY EXCLUDED]" if ep["episode_id"] in ALREADY_EXCLUDED else ""
        gap_str = f"{gap_s:.0f}" if gap_s is not None else "n/a"
        print(f"{ep['scored_at']:20s} {ep['target'] or '?':12s} {ep['predicted_class']:26s} "
              f"{str(ep['correct']):7s} {gap_str:>9s}  {prior_class:22s} {prior_action:16s}  {flag}{already}")

    print("\n" + "=" * 100)
    print(f"SUMMARY: {len(overlap)} OVERLAP_RISK (<60s), {len(residual)} RESIDUAL_RISK (60-300s), "
          f"{len(none_eps) - len(overlap) - len(residual)} CLEAR")

    new_overlap = [x for x in overlap if x[0]["episode_id"] not in ALREADY_EXCLUDED]
    new_residual = [x for x in residual if x[0]["episode_id"] not in ALREADY_EXCLUDED]
    print(f"\nOf those, NOT already excluded/known: {len(new_overlap)} new OVERLAP_RISK, {len(new_residual)} new RESIDUAL_RISK")

    if new_overlap:
        print("\n--- NEW OVERLAP_RISK episodes (not already excluded) ---")
        for ep, prior, gap_s in new_overlap:
            print(f"  {ep['episode_id']}  target={ep['target']}  predicted={ep['predicted_class']}  "
                  f"correct={ep['correct']}  gap={gap_s:.0f}s  prior_class={prior['actual_class']}")

    if new_residual:
        print("\n--- NEW RESIDUAL_RISK episodes (not already excluded) ---")
        for ep, prior, gap_s in new_residual:
            print(f"  {ep['episode_id']}  target={ep['target']}  predicted={ep['predicted_class']}  "
                  f"correct={ep['correct']}  gap={gap_s:.0f}s  prior_class={prior['actual_class']}")

    conn.close()


if __name__ == "__main__":
    main()
