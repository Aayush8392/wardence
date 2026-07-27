"""
Phase E scoping -- read-only audit script. Makes NO writes to the DB or
cluster. Pulls real counts/timestamps so we can decide, with real numbers,
what historical episode data is worth keeping/correcting/excluding --
instead of guessing from memory of the buildlog narrative.

Run from WSL2 (same venv as the rest of p2_readonly_loop/p3_trust_action --
the DB lives on the native WSL2 filesystem, not /mnt/c):

    source ~/wardence_venv/bin/activate   # or wherever the kubernetes-capable venv is
    cd /mnt/c/Users/HP/Wardence/p2_readonly_loop
    python3 phase_e_audit.py

Writes a full text report to output/phase_e_audit_report.txt (via the same
_Tee convention as other pipeline modules) and prints to stdout too.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

DB_PATH = Path.home() / "wardence_p2_data" / "wardence.db"
HERE = Path(__file__).resolve().parent
JSONL_PATH = HERE / "phase_d_results.jsonl"
OUTPUT_DIR = HERE / "output"


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    report_path = OUTPUT_DIR / "phase_e_audit_report.txt"
    log_f = open(report_path, "w", encoding="utf-8")
    real_stdout = sys.stdout
    sys.stdout = _Tee(real_stdout, log_f)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 78)
    print("PHASE E AUDIT -- read-only, no writes made")
    print(f"DB: {DB_PATH}")
    print("=" * 78)

    # ---- 1. Raw table counts ----
    print("\n--- 1. Raw table counts ---")
    for table in ("episodes", "scores", "trust_history", "failure_log"):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:16s}: {n}")
        except sqlite3.OperationalError as e:
            print(f"  {table:16s}: TABLE MISSING ({e})")

    # ---- 2. Per-class breakdown from scores (joined to episodes for real target) ----
    print("\n--- 2. Per fault_class breakdown (from scores) ---")
    rows = conn.execute(
        """
        SELECT s.episode_id, s.predicted_class, s.actual_class, s.correct,
               s.action_taken, s.action_applied, s.durability_verdict,
               s.trust_correct, s.scored_at, e.t0, e.target
        FROM scores s
        LEFT JOIN episodes e ON e.episode_id = s.episode_id
        ORDER BY s.scored_at
        """
    ).fetchall()
    print(f"  Total scored episodes: {len(rows)}")

    by_class = defaultdict(list)
    for r in rows:
        by_class[r["actual_class"]].append(r)

    print(f"\n  {'class':28s} {'total':>6s} {'correct':>8s} {'wrong':>6s} "
          f"{'acted':>6s} {'first_scored_at':>20s} {'last_scored_at':>20s}")
    for cls in sorted(by_class):
        recs = by_class[cls]
        total = len(recs)
        correct = sum(1 for r in recs if r["correct"])
        wrong = total - correct
        acted = sum(1 for r in recs if r["action_applied"])
        first_ts = min(r["scored_at"] for r in recs if r["scored_at"])
        last_ts = max(r["scored_at"] for r in recs if r["scored_at"])
        print(f"  {cls:28s} {total:6d} {correct:8d} {wrong:6d} {acted:6d} "
              f"{first_ts:>20s} {last_ts:>20s}")

    durability_counter = Counter(r["durability_verdict"] for r in rows if r["durability_verdict"])
    print(f"\n  Durability verdict breakdown (all classes): {dict(durability_counter)}")

    # ---- 3. All WRONG episodes, full detail (candidates for exclude/reclassify) ----
    print("\n--- 3. All WRONG-verdict episodes (correct=0), full detail ---")
    wrong_rows = [r for r in rows if not r["correct"]]
    print(f"  Total wrong: {len(wrong_rows)}")
    for r in wrong_rows:
        print(f"  {r['scored_at']}  ep={r['episode_id']}  actual={r['actual_class']:24s} "
              f"predicted={r['predicted_class']:24s} action_taken={r['action_taken']} "
              f"action_applied={r['action_applied']} durability={r['durability_verdict']}")

    # ---- 4. Suspicious "none" control mislabels (auto-detected, not hardcoded IDs) ----
    print("\n--- 4. Auto-detected control mislabels (actual_class='none', predicted != 'none') ---")
    control_mislabels = [r for r in rows if r["actual_class"] == "none" and r["predicted_class"] != "none"]
    print(f"  Count: {len(control_mislabels)}")
    for r in control_mislabels:
        print(f"  {r['scored_at']}  ep={r['episode_id']}  target={r['target']}  "
              f"predicted={r['predicted_class']}  (ground truth was 'none' -- may be a mislabeled "
              f"control per Investigation 2's known bug class, or a real false positive -- needs eyeballing)")

    # ---- 5. Trust history -- every promotion/demotion event, in order ----
    print("\n--- 5. trust_history: every state transition, in order ---")
    th_rows = conn.execute(
        "SELECT * FROM trust_history ORDER BY recorded_at"
    ).fetchall()
    print(f"  Total transitions: {len(th_rows)}")
    demotions = []
    promotions = []
    for r in th_rows:
        tag = ""
        if r["state_before"] == "can_act" and r["state_after"] == "report_only":
            tag = "  <-- DEMOTION"
            demotions.append(r)
        elif r["state_before"] == "report_only" and r["state_after"] == "can_act":
            tag = "  <-- PROMOTION"
            promotions.append(r)
        print(f"  {r['recorded_at']}  {r['fault_class']:24s} ep={r['episode_id']}  "
              f"{r['state_before']}({r['streak_before']}) -> {r['state_after']}({r['streak_after']}){tag}")

    print(f"\n  Total demotions: {len(demotions)}  |  Total promotions: {len(promotions)}")
    print("\n  Demotions by class:")
    for cls, cnt in Counter(r["fault_class"] for r in demotions).most_common():
        print(f"    {cls:24s} {cnt}")

    # ---- 6. failure_log (circuit breaker trips) ----
    print("\n--- 6. failure_log (circuit breaker trips) ---")
    fl_rows = conn.execute("SELECT * FROM failure_log ORDER BY recorded_at").fetchall()
    print(f"  Total entries: {len(fl_rows)}")
    for r in fl_rows:
        print(f"  {r['recorded_at']}  class={r['fault_class']}  reason={r['reason']}")

    # ---- 7. Cross-reference against phase_d_results.jsonl (Phase D runs only) ----
    print("\n--- 7. phase_d_results.jsonl cross-reference ---")
    if not JSONL_PATH.exists():
        print(f"  NOT FOUND at {JSONL_PATH} -- skipping (may have been cleaned up or run elsewhere).")
        jsonl_episode_ids = set()
        round_counts = {}
    else:
        pd_entries = []
        with open(JSONL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pd_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        print(f"  Total jsonl entries: {len(pd_entries)}")
        round_counts = Counter(e.get("round_id", "?") for e in pd_entries)
        print(f"  Entries per round_id: {dict(round_counts)}")

        # Each entry covers a pair (two episodes, side a/b) -- collect any
        # episode_id fields present so we can mark which scores rows were
        # part of a formal Phase D pairwise run vs. something else.
        jsonl_episode_ids = set()
        for e in pd_entries:
            for key in ("episode_id", "episode_id_a", "episode_id_b"):
                if key in e and e[key]:
                    jsonl_episode_ids.add(e[key])
            # some shapes nest per-side results -- best-effort walk
            for side_key in ("a", "b", "side_a", "side_b"):
                side = e.get(side_key)
                if isinstance(side, dict) and side.get("episode_id"):
                    jsonl_episode_ids.add(side["episode_id"])
        print(f"  Distinct episode_ids findable in jsonl: {len(jsonl_episode_ids)}")

    # ---- 8. Episodes NOT accounted for by any known Phase D jsonl round ----
    print("\n--- 8. scores rows with NO match in phase_d_results.jsonl (pre-Phase-D / manual / other runs) ---")
    non_pd = [r for r in rows if r["episode_id"] not in jsonl_episode_ids]
    print(f"  Count: {len(non_pd)} of {len(rows)} total scored episodes")
    non_pd_by_class = Counter(r["actual_class"] for r in non_pd)
    print(f"  By class: {dict(non_pd_by_class)}")
    if non_pd:
        first_ts = min(r["scored_at"] for r in non_pd if r["scored_at"])
        last_ts = max(r["scored_at"] for r in non_pd if r["scored_at"])
        print(f"  Timestamp range: {first_ts} .. {last_ts}")

    print("\n" + "=" * 78)
    print("END OF AUDIT -- report also saved to:", report_path)
    print("=" * 78)

    conn.close()
    sys.stdout = real_stdout
    log_f.close()


if __name__ == "__main__":
    main()
