"""
Live, single-episode smoke test for the real ReAct evidence loop
(react_agent.py) -- COMPARISON ONLY, per the locked 150-episode-floor
rule. Triggers a real fault via the real injector, waits the same
SETTLE_SECONDS every other real pipeline uses, then runs
run_react_diagnosis() against the real live target and prints the full
transcript plus a comparison against both ground truth and
stub_diagnose's own result.

Deliberately does NOT call p3_scorer.py or trust_engine -- this is a
plumbing/sanity check of the loop itself, not a scored episode. Ground
truth and the stub's own diagnosis are read directly from the DB for
comparison, nothing is written back except this file's own
llm_diagnosis_log row (reusing llm_replay_test.py's table).

Usage:
    python3 test_react_agent.py --fault-class crash-loop
    python3 test_react_agent.py --all-classes   # loops FAULT_CONFIG's real classes,
                                                  # prints each result as it finishes
                                                  # (not buffered until the whole run ends)
    python3 test_react_agent.py --compound-signal   # see run_compound_signal_check() below

Ported from the now-retired run_systematic_validation.py (2026-07-31,
folded in rather than deleted outright): run_compound_signal_check()
fires OOM's memory stressor and disk-full's disk-fill CONCURRENTLY
against queue-master, then diagnoses the result -- unscored/exploratory,
there's no single correct ground-truth label for a pod that's genuinely
both OOM- and disk-pressured. Tests whether the diagnoser's priority
ordering (oom checked before Evicted) holds under a genuine two-signal
conflict, not just each signal in isolation. The original version ran
this against the OLD pre-P3 stub-only agent endpoint; this version runs
it through the real run_react_diagnosis() instead, since that's the
diagnoser that actually matters now and had never been tested against a
compound signal at all.
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

from action_proposer import DETERMINISTIC_ACTION_MAP, log_proposal, propose_action  # noqa: E402
from agent import (  # noqa: E402
    call_dl_detector, probe_catalogue_capacity, query_prometheus, stub_diagnose, DL_DETECTOR_SERVICES,
)
from injector import (  # noqa: E402
    FAULT_CONFIG, _cleanup_disk_full_files, apply_manifest, build_oom_manifest,
    delete_chaos_resource, run_disk_full_injection,
)
from llm_replay_test import _same_diagnosis, ensure_llm_diagnosis_log_table  # noqa: E402
from model_backend import PROVIDER_CHAIN  # noqa: E402
from react_agent import run_react_diagnosis  # noqa: E402
from trust_engine import DB_PATH  # noqa: E402

SETTLE_SECONDS = 35  # same convention as run_episodes.py/run_batch_plan.py
COMPOUND_MEMORY_STRESS_SIZE = "600M"  # queue-master's memory limit is 500Mi


def trigger_injection(fault_class: str) -> str | None:
    proc = subprocess.run(
        [sys.executable, "injector.py", "--class", fault_class],
        cwd=Path(__file__).parent, capture_output=True, text=True,
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    match = re.search(r"Episode ([0-9a-f-]{36}): injection verified", proc.stdout)
    return match.group(1) if match else None


def build_tools(target: str, namespace: str) -> dict:
    tools = {
        "query_prometheus": lambda: query_prometheus(target, namespace),
    }
    if target in DL_DETECTOR_SERVICES:
        tools["call_dl_detector"] = lambda: call_dl_detector(target)
    if target == "catalogue":
        tools["probe_catalogue_capacity"] = lambda: probe_catalogue_capacity(namespace)
    return tools


def fetch_ground_truth(conn: sqlite3.Connection, episode_id: str) -> str | None:
    row = conn.execute(
        "SELECT fault_class FROM episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    return row[0] if row else None


def run_one(fault_class: str, force_provider: str | None = None) -> dict:
    """Runs one full real episode through the ReAct loop, prints its own
    result immediately (flushed), and returns the summary dict -- so a
    multi-class caller can print progress as each class finishes rather
    than buffering everything until the whole run is done.

    force_provider (e.g. "deepinfra"): filters PROVIDER_CHAIN down to
    just that provider's own entries before calling run_react_diagnosis,
    so a calibration run can deliberately exercise a specific provider
    (e.g. Nemotron/deepinfra, which normally only fires on a real gemma
    failure) instead of leaving it to chance. Never used for a real
    production episode."""
    cfg = FAULT_CONFIG[fault_class]
    target, namespace = cfg["target"], cfg["namespace"]
    chain = [e for e in PROVIDER_CHAIN if e["provider"] == force_provider] if force_provider else None

    print(f"\n===== {fault_class} (target={target}) =====", flush=True)
    print(f"Triggering real {fault_class} on {target} ({namespace})...", flush=True)
    episode_id = trigger_injection(fault_class)
    if episode_id is None:
        print(f"[{fault_class}] Injection failed -- see injector.py's own output above. Skipping.", flush=True)
        return {"fault_class": fault_class, "status": "injection_failed"}

    print(f"Waiting {SETTLE_SECONDS}s settle...", flush=True)
    time.sleep(SETTLE_SECONDS)

    tool_output = query_prometheus(target, namespace)
    stub_result = stub_diagnose(tool_output)

    tools = build_tools(target, namespace)
    print(f"Running the real ReAct loop against tools: {list(tools)}"
          f"{f' (forced provider={force_provider})' if force_provider else ''}...", flush=True)
    llm_result = run_react_diagnosis(target, namespace, tools, chain=chain, episode_id=episode_id)

    conn = sqlite3.connect(DB_PATH)
    actual_class = fetch_ground_truth(conn, episode_id)
    ensure_llm_diagnosis_log_table(conn)

    llm_diagnosis = llm_result.get("llm_diagnosis")
    conn.execute(
        """
        INSERT INTO llm_diagnosis_log (
            episode_id, actual_class, stub_predicted_class, stub_correct,
            llm_diagnosis, llm_confidence, llm_confidence_source, llm_reasoning,
            provider, model, tier, matches_ground_truth, matches_stub, failed_attempts_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode_id, actual_class, stub_result["diagnosis"],
            int(_same_diagnosis(stub_result["diagnosis"], actual_class)),
            llm_diagnosis, llm_result.get("llm_confidence"),
            llm_result.get("llm_confidence_source"), llm_result.get("llm_reasoning"),
            llm_result.get("provider"), llm_result.get("model"), llm_result.get("tier"),
            int(_same_diagnosis(llm_diagnosis, actual_class)) if llm_diagnosis else None,
            int(_same_diagnosis(llm_diagnosis, stub_result["diagnosis"])) if llm_diagnosis else None,
            json.dumps([a.__dict__ if hasattr(a, "__dict__") else a for a in llm_result.get("failed_attempts", [])]),
        ),
    )
    conn.commit()
    conn.close()

    print("--- Transcript ---", flush=True)
    for line in llm_result.get("transcript", []):
        print(line, flush=True)

    summary = {
        "fault_class": fault_class,
        "episode_id": episode_id,
        "actual_class": actual_class,
        "stub_diagnosis": stub_result["diagnosis"],
        "llm_status": llm_result["status"],
        "llm_diagnosis": llm_diagnosis,
        "llm_confidence": llm_result.get("llm_confidence"),
        "llm_confidence_source": llm_result.get("llm_confidence_source"),
        "provider": llm_result.get("provider"),
        "model": llm_result.get("model"),
        "turns_used": llm_result.get("turns_used"),
        "matches_ground_truth": int(_same_diagnosis(llm_diagnosis, actual_class)) if llm_diagnosis else None,
    }

    # Phase 2 (action_proposer.py) -- only meaningful when the LLM's OWN
    # diagnosis (not ground truth -- same blinding discipline everywhere
    # else) landed on one of the 6 auto-fix classes. COMPARISON-ONLY:
    # never dispatches, only logs to llm_action_proposal_log.
    if llm_diagnosis in DETERMINISTIC_ACTION_MAP:
        # Real fix, 2026-08-01: this call never passed tool_output at all
        # (confirmed via git diff against the last commit -- a pre-
        # existing gap, not a regression from today's edits), meaning
        # the action-proposal prompt always saw "(not available)" for
        # real observed data. Merge the base query_prometheus() output
        # with whatever the ReAct loop's OWN tool calls actually gathered
        # (llm_result["observations"], e.g. catalogue_probe_p95_ms from
        # probe_catalogue_capacity) -- reuses real data already fetched
        # rather than re-invoking a non-free tool a second time.
        merged_tool_output = {**tool_output}
        if "probe_catalogue_capacity" in llm_result.get("observations", {}):
            merged_tool_output["catalogue_probe_p95_ms"] = llm_result["observations"]["probe_catalogue_capacity"]
        print(f"Proposing an action for LLM diagnosis {llm_diagnosis!r}...", flush=True)
        proposal = propose_action(
            llm_diagnosis,
            {"diagnosis": llm_diagnosis, "confidence": llm_result.get("llm_confidence"),
             "reasoning": llm_result.get("llm_reasoning")},
            target, namespace, llm_result.get("provider"), llm_result.get("model"),
            tool_output=merged_tool_output, episode_id=episode_id,
        )
        log_proposal(episode_id, target, namespace, proposal)
        summary["action_proposal"] = {
            "source": proposal.get("source"), "tool_name": proposal.get("tool_name"),
            "params": proposal.get("params"), "tier": proposal.get("tier"),
        }
        print(f"[{fault_class}] Action proposal: {json.dumps(summary['action_proposal'])} "
              f"-- logged to llm_action_proposal_log, NOT dispatched.", flush=True)

    print("--- Result ---", flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    print(f"[{fault_class}] Logged to llm_diagnosis_log -- not scores, not trust_state.", flush=True)
    return summary


def run_compound_signal_check():
    """
    Fires OOM's memory stressor and disk-full's exec-based fill CONCURRENTLY
    against queue-master, then asks the real ReAct loop to diagnose it.
    Not scored -- see module docstring.
    """
    print("\n--- Compound-signal check (not scored, exploratory) ---", flush=True)
    cfg = FAULT_CONFIG["disk-full"]  # queue-master's namespace/target/container

    chaos_name = f"wardence-compound-{uuid.uuid4().hex[:8]}"
    manifest = build_oom_manifest(chaos_name, cfg, size=COMPOUND_MEMORY_STRESS_SIZE)
    apply_manifest(manifest)

    # Confirm the stressor actually reached the cluster before assuming
    # it's live -- apply_manifest succeeding only means the k8s API
    # accepted the request.
    time.sleep(5)
    check = subprocess.run(
        ["kubectl", "get", "stresschaos", chaos_name, "-n", "chaos-mesh"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(f"WARNING: compound-signal stressor {chaos_name} not found after apply -- "
              f"this check may just be disk-full alone, not a genuine compound signal.", flush=True)

    print(f"Memory stressor ({COMPOUND_MEMORY_STRESS_SIZE}) applied to {cfg['target']}, "
          f"now concurrently running the disk-fill loop for {cfg['duration_s']}s...", flush=True)

    try:
        run_disk_full_injection(cfg, cfg["duration_s"])
    finally:
        delete_chaos_resource("stresschaos", chaos_name)
        _cleanup_disk_full_files(cfg["target"], cfg["namespace"], cfg["container"])

    print(f"Waiting {SETTLE_SECONDS}s settle...", flush=True)
    time.sleep(SETTLE_SECONDS)

    tools = build_tools(cfg["target"], cfg["namespace"])
    print(f"Running the real ReAct loop against tools: {list(tools)}...", flush=True)
    llm_result = run_react_diagnosis(cfg["target"], cfg["namespace"], tools)

    print("--- Transcript ---", flush=True)
    for line in llm_result.get("transcript", []):
        print(line, flush=True)

    print(f"Compound-signal diagnosis: {llm_result.get('llm_diagnosis')} "
          f"(confidence={llm_result.get('llm_confidence')})", flush=True)
    print(
        "Expected per the locked priority ordering: 'oom' should win (checked before "
        "Evicted) if both signals are genuinely present.", flush=True
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fault-class", choices=FAULT_CONFIG.keys())
    group.add_argument("--all-classes", action="store_true")
    group.add_argument("--compound-signal", action="store_true")
    parser.add_argument(
        "--force-provider", choices=sorted({e["provider"] for e in PROVIDER_CHAIN}), default=None,
        help="calibration only: filter PROVIDER_CHAIN down to just this provider's entries, "
             "so e.g. deepinfra/Nemotron can be exercised deliberately instead of only on a "
             "real gemma failure. Never use for a real production episode.",
    )
    args = parser.parse_args()

    if args.compound_signal:
        run_compound_signal_check()
    elif args.all_classes:
        results = []
        for fc in FAULT_CONFIG:
            results.append(run_one(fc, force_provider=args.force_provider))
        correct = sum(1 for r in results if r.get("matches_ground_truth"))
        total = sum(1 for r in results if r.get("llm_status") == "diagnosed")
        skipped = sum(1 for r in results if r.get("status") == "injection_failed")
        print(f"\n===== FULL ROSTER SUMMARY =====", flush=True)
        print(f"{correct}/{total} correct vs ground truth ({skipped} class(es) skipped due to injection failure).", flush=True)
    else:
        run_one(args.fault_class, force_provider=args.force_provider)
