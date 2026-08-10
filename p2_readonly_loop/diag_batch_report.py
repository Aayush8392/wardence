"""
Reusable batch-verification report.

Reconstructs a run_batch_plan.py batch's real membership from its OWN
log file(s) -- not by guessing a DB time window or grabbing "the last N
episodes" (both fragile: the latter breaks the moment any later batch
adds more episodes for the same classes). The log is the durable,
authoritative record of what actually happened, including injection
failures/retries, which never get a DB row at all (injector.py refuses
to fabricate an episode on a real failure) -- that data only exists here.

How batch membership is reconstructed:
  1. Find the log containing "Completed plan archived to <archive file name>"
     -- printed once, by _archive_completed_plan(), naming the exact
     archived plan json. This anchors us to the right batch unambiguously.
  2. Walk backward through output/run_batch_plan_*.log files (sorted by
     their filename timestamp) as long as each one starts by resuming
     the same batch_plan_progress.json (a "Resuming existing plan..."
     line), stopping once a log's own "New plan started: N chunk(s)."
     line is found (the batch's real genesis) or the chain breaks.
     Handles a batch that paused/resumed across multiple runs (e.g. the
     2026-08-04 network-DNS hiccup).
  3. The earliest log's filename timestamp = real window start; the
     archive file's filename timestamp = real window end. Episodes are
     pulled from the DB within that window, per class, oldest-first,
     and the count is cross-checked against what the log itself says
     it recorded per class (not the plan's target -- a class that gave
     up early would under-deliver against target but the log's own
     count is still the ground truth for what really happened).

Usage:
  python3 diag_batch_report.py <path-to-archived-plan.json>
  (defaults to the most recently modified batch_plan_progress_*.json
  in this directory if no path given)
"""
import sqlite3
import json
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = Path(__file__).parent
OUTPUT_DIR = HERE / "output"
DB_PATH = "/home/aayush/wardence_p2_data/wardence.db"

# Classes whose LLM-proposed magnitude value is worth reporting alongside
# cpu-throttling (which the earlier version of this script already showed).
# action_result field name differs per action type.
MAGNITUDE_FIELD_BY_CLASS = {
    "oom": ("patch_memory_limit", "limit"),
    "under-provisioned-replicas": ("scale_deployment", "replicas"),
    "cpu-throttling": ("patch_cpu_limit", "limit"),
}

TS_RE = re.compile(r"(\d{8}T\d{6}Z)")


def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _ts_to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def find_archive_path() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    candidates = sorted(HERE.glob("batch_plan_progress_*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        print("No batch_plan_progress_*.json found and none given on the command line.")
        sys.exit(1)
    return candidates[-1]


def find_log_chain(archive_path: Path) -> list[Path]:
    all_logs = sorted(
        OUTPUT_DIR.glob("run_batch_plan_*.log"),
        key=lambda p: p.name,
    )
    if not all_logs:
        print(f"No run_batch_plan_*.log files found in {OUTPUT_DIR}")
        sys.exit(1)

    anchor_text = f"Completed plan archived to {archive_path.name}"
    anchor_idx = None
    for i, log_path in enumerate(all_logs):
        if anchor_text in log_path.read_text(encoding="utf-8", errors="replace"):
            anchor_idx = i
            break
    if anchor_idx is None:
        print(f"Could not find any log containing '{anchor_text}'.")
        print("Falling back to just the archive file's own timestamp for the window "
              "(injection-failure/retry data will be unavailable).")
        return []

    chain = [all_logs[anchor_idx]]
    i = anchor_idx
    while True:
        text = chain[0].read_text(encoding="utf-8", errors="replace")
        if "New plan started:" in text:
            break  # found the real genesis, stop walking backward
        if i == 0:
            break  # ran out of earlier logs to check
        prev = all_logs[i - 1]
        prev_text = prev.read_text(encoding="utf-8", errors="replace")
        if "Resuming existing plan from batch_plan_progress.json" in text:
            chain.insert(0, prev)
            i -= 1
        else:
            break
    return chain


LINE_ATTEMPT = re.compile(r"^--- (\S+) attempt (\d+) \((\d+)/(\d+) real episodes so far, this chunk\) ---")
LINE_SUCCESS = re.compile(r"^--- real episode recorded, took ([\d.]+)s \((\S+): (\d+)/(\d+), this chunk\) ---")
LINE_FAILURE = re.compile(
    r"^\s*WARNING: no real episode was recorded this attempt \((\d+) -> (\d+) in the DB\) -- "
    r"retrying \((\d+)/(\d+) consecutive real failures for (\S+)\)\."
)
LINE_GAVE_UP = re.compile(
    r"^\s*(\d+) consecutive real failures for (\S+) -- giving up on this class"
)
LINE_SUMMARY = re.compile(
    r"^=== Batch summary: real wall-clock time [\d.]+h \((\d+)s\), "
    r"(\d+) real episode\(s\) recorded, (\d+) real injection failure\(s\)"
)


def parse_log_chain(chain: list[Path]) -> dict:
    successes_by_class = defaultdict(int)
    failures_by_class = defaultdict(int)
    failure_events = []  # (class, before, after, consec, cap)
    gave_up_classes = []
    summary_line = None

    for log_path in chain:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = LINE_SUCCESS.match(line)
            if m:
                _, cls, _, _ = m.groups()
                successes_by_class[cls] += 1
                continue
            m = LINE_FAILURE.match(line)
            if m:
                before, after, consec, cap, cls = m.groups()
                failures_by_class[cls] += 1
                failure_events.append((cls, int(before), int(after), int(consec), int(cap)))
                continue
            m = LINE_GAVE_UP.match(line)
            if m:
                consec, cls = m.groups()
                gave_up_classes.append((cls, int(consec)))
                continue
            m = LINE_SUMMARY.match(line)
            if m:
                summary_line = line.strip()

    return {
        "successes_by_class": dict(successes_by_class),
        "failures_by_class": dict(failures_by_class),
        "failure_events": failure_events,
        "gave_up_classes": gave_up_classes,
        "summary_line": summary_line,
    }


def main():
    archive_path = find_archive_path()
    print(f"Archive plan file: {archive_path.name}\n")

    with open(archive_path) as f:
        plan = json.load(f)
    # REAL BUG FIXED (2026-08-1x, network-partition false MISMATCH on the
    # T065344Z batch): this used to be Counter(item["class"] for item in
    # plan["plan"]), which counts PLAN CHUNKS, not real episodes. A
    # class's chunk count and its real episode total only diverge when
    # _build_shuffled_plan()'s tail-drain block fires (the last class
    # standing gets ONE chunk with target=remaining>1, not fragmented
    # into single-episode chunks) -- confirmed exactly this on that
    # batch's network-partition: 14 chunks of target=1 + 1 chunk of
    # target=2 = 16 real episodes, correctly matching every other class,
    # but only 15 chunk entries, which falsely flagged as a MISMATCH.
    # Summing each chunk's own "target" field is the real per-class total.
    plan_classes = Counter()
    for item in plan["plan"]:
        plan_classes[item["class"]] += item["target"]

    m = TS_RE.search(archive_path.name)
    if not m:
        print("Could not parse a timestamp out of the archive filename.")
        sys.exit(1)
    window_end_dt = _parse_ts(m.group(1))

    log_chain = find_log_chain(archive_path)
    if log_chain:
        print("Log chain (chronological):")
        for lp in log_chain:
            print(f"  {lp.name}")
        window_start_dt = _parse_ts(TS_RE.search(log_chain[0].name).group(1))
        parsed = parse_log_chain(log_chain)
    else:
        window_start_dt = None
        parsed = {"successes_by_class": {}, "failures_by_class": {}, "failure_events": [],
                   "gave_up_classes": [], "summary_line": None}

    if window_start_dt is None:
        # No usable log chain -- fall back to a generous window ending at
        # the archive timestamp, sized off the plan's own episode count
        # (better than nothing, but genuinely a fallback, not the real thing).
        print("\nWARNING: falling back to a guessed window (no log chain found).")
        window_start_dt = window_end_dt  # will show 0 rows; forces the user to notice

    window_start = _ts_to_iso(window_start_dt)
    window_end = _ts_to_iso(window_end_dt)
    print(f"\nReal batch window (from log filenames): {window_start} .. {window_end} UTC\n")

    print("Log-reported real successes per class vs. plan target:")
    all_classes = sorted(set(plan_classes) | set(parsed["successes_by_class"]))
    for cls in all_classes:
        got = parsed["successes_by_class"].get(cls, 0)
        target = plan_classes.get(cls, 0)
        flag = "" if got == target else "  <-- MISMATCH"
        print(f"  {cls}: {got}/{target}{flag}")

    print(f"\nInjection failures/retries this batch: "
          f"{sum(parsed['failures_by_class'].values())} total")
    for cls, n in sorted(parsed["failures_by_class"].items()):
        print(f"  {cls}: {n} retry event(s)")
    for cls, before, after, consec, cap in parsed["failure_events"]:
        print(f"    retry: class={cls} db_count {before}->{after} "
              f"(consecutive {consec}/{cap})")

    if parsed["gave_up_classes"]:
        print(f"\nClasses that GAVE UP short of target: {len(parsed['gave_up_classes'])}")
        for cls, consec in parsed["gave_up_classes"]:
            print(f"  {cls}: gave up after {consec} consecutive real failures")
    else:
        print("\nNo class gave up short of target.")

    if parsed["summary_line"]:
        print(f"\nLog's own final summary line:\n  {parsed['summary_line']}")

    # ---- Now pull the real DB rows for this window and score them ----
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT e.episode_id, e.fault_class, e.target, e.t0,
               s.predicted_class, s.actual_class, s.correct, s.confidence,
               s.action_taken, s.durability_verdict, s.trust_correct
        FROM episodes e
        LEFT JOIN scores s ON e.episode_id = s.episode_id
        WHERE e.t0 >= ? AND e.t0 <= ?
        ORDER BY e.t0 ASC
    """, (window_start, window_end))
    rows = cur.fetchall()

    print(f"\nReal DB episodes in window: {len(rows)}")
    found_classes = Counter(r["fault_class"] for r in rows)
    for cls in sorted(set(found_classes) | set(plan_classes)):
        db_n = found_classes.get(cls, 0)
        log_n = parsed["successes_by_class"].get(cls, db_n)
        flag = "" if db_n == log_n else "  <-- DB/log mismatch"
        print(f"  {cls}: db={db_n} log={log_n}{flag}")

    scored = [r for r in rows if r["correct"] is not None]
    unscored = [r for r in rows if r["correct"] is None]
    if unscored:
        print(f"\nUnscored episodes (no scores row yet): {len(unscored)}")
        for r in unscored:
            print(f"  {r['episode_id']} class={r['fault_class']} t0={r['t0']}")

    wrong = [r for r in scored if r["correct"] == 0]
    print(f"\nWrong diagnoses: {len(wrong)}")
    for r in wrong:
        print(f"  {r['episode_id']} class={r['fault_class']} "
              f"predicted={r['predicted_class']} actual={r['actual_class']}")

    bad_trust = [r for r in scored if r["trust_correct"] == 0]
    print(f"\ntrust_correct == 0 rows: {len(bad_trust)}")
    for r in bad_trust:
        print(f"  {r['episode_id']} class={r['fault_class']} "
              f"action={r['action_taken']} durability={r['durability_verdict']}")

    flapped = [r for r in scored
               if r["durability_verdict"] and r["durability_verdict"] not in ("held", "confirmed", "n/a")]
    print(f"\nDurability verdicts NOT held/confirmed: {len(flapped)}")
    for r in flapped:
        print(f"  {r['episode_id']} class={r['fault_class']} verdict={r['durability_verdict']}")

    print("\nPer-class accuracy:")
    by_class = defaultdict(list)
    for r in scored:
        by_class[r["fault_class"]].append(r)
    for cls in sorted(by_class):
        items = by_class[cls]
        n_correct = sum(1 for r in items if r["correct"] == 1)
        print(f"  {cls}: {n_correct}/{len(items)} correct")

    episode_ids = tuple(r["episode_id"] for r in rows)
    placeholders = ",".join("?" * len(episode_ids)) if episode_ids else "''"

    cur.execute(f"""
        SELECT episode_id, fault_class, correct, state_before, state_after
        FROM trust_history
        WHERE episode_id IN ({placeholders}) AND state_before != state_after
    """, episode_ids)
    trust_changes = cur.fetchall()
    print(f"\nDimension A trust_state changes: {len(trust_changes)}")
    for r in trust_changes:
        print(f"  {r['episode_id']} class={r['fault_class']} correct={r['correct']} "
              f"{r['state_before']} -> {r['state_after']}")

    cur.execute(f"""
        SELECT episode_id, fault_class, correct, state_before, state_after
        FROM action_trust_history
        WHERE episode_id IN ({placeholders}) AND state_before != state_after
    """, episode_ids)
    action_changes = cur.fetchall()
    print(f"\nDimension C action_trust changes: {len(action_changes)}")
    for r in action_changes:
        print(f"  {r['episode_id']} class={r['fault_class']} correct={r['correct']} "
              f"{r['state_before']} -> {r['state_after']}")

    cur.execute(f"""
        SELECT episode_id, fault_class, correct, mode_before, mode_after
        FROM diagnoser_mode_history
        WHERE episode_id IN ({placeholders}) AND mode_before != mode_after
    """, episode_ids)
    mode_changes = cur.fetchall()
    print(f"\nDimension B diagnoser_mode changes: {len(mode_changes)}")
    for r in mode_changes:
        print(f"  {r['episode_id']} class={r['fault_class']} correct={r['correct']} "
              f"{r['mode_before']} -> {r['mode_after']}")

    # ---- Proposed-value comparison for the 3 magnitude-sensitive classes ----
    cur.execute(f"""
        SELECT episode_id, action_taken, action_result
        FROM episode_snapshots
        WHERE episode_id IN ({placeholders})
    """, episode_ids)
    snap_by_id = {r["episode_id"]: r for r in cur.fetchall()}

    for cls, (expected_action, field) in MAGNITUDE_FIELD_BY_CLASS.items():
        cls_rows = [r for r in rows if r["fault_class"] == cls]
        if not cls_rows:
            continue
        print(f"\n{cls} proposed values ({len(cls_rows)} episode(s)):")
        for r in cls_rows:
            snap = snap_by_id.get(r["episode_id"])
            if not snap or not snap["action_result"]:
                print(f"  {r['episode_id']} -- no action_result recorded")
                continue
            try:
                ar = json.loads(snap["action_result"])
            except (json.JSONDecodeError, TypeError):
                print(f"  {r['episode_id']} -- unparseable action_result")
                continue
            value = ar.get(field, "?")
            applied = ar.get("applied", "?")
            print(f"  {r['episode_id']}: action={ar.get('action')} {field}={value} applied={applied}")

    conn.close()


if __name__ == "__main__":
    main()
