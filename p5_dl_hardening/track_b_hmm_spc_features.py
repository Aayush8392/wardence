"""
Track B: builds the two real feature views the HMM/rate-based-SPC track
needs, for catalogue and queue-master (the two DeepLog-Tier-2-thin
services -- see wardence_context.md's "DeepLog real per-service scope
LOCKED" section, and reviews/07_deeplog_coverage_kimi_review.md for why
LSTM/DeepLog is the wrong tool at their vocabulary size but an HMM or
rate-based SPC is legitimate).

Reads from the real, already-accumulated pipeline stream
(log_sequence_pipeline.py's output) -- no fresh Loki query, this is
pure downstream feature extraction. Applies the real, confirmed noise
filter (deeplog_service_config.SERVICE_NOISE_MARKERS) before building
either view, since queue-master's raw stream is 77.8% a dead/unrelated
feature's error spam.

Two outputs per service, written to pipeline_state/track_b/:
  1. <service>_hmm_sequence.json -- the full ordered, noise-filtered
     template_id sequence. What a Baum-Welch HMM fit trains on directly
     (no windowing needed, unlike Track A's DeepLog).
  2. <service>_spc_vectors.jsonl -- one line per BUCKET_SECONDS time
     bucket: real per-template counts (the "rate/mix" signal) plus
     real inter-arrival-time stats (mean/max gap within the bucket --
     the "cadence" signal). What an EWMA/CUSUM monitor consumes.

Usage:
    python3 track_b_hmm_spc_features.py
"""

import json
import os
import statistics
from collections import defaultdict

from deeplog_service_config import DEEPLOG_TIER_2_THIN, is_noise_template

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "streams")
OUT_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_b")

BUCKET_SECONDS = 60


def load_events(service: str):
    stream_path = os.path.join(STREAM_DIR, f"{service}.jsonl")
    events = []
    with open(stream_path) as f:
        for line in f:
            events.append(json.loads(line))
    events.sort(key=lambda e: e["ts_ns"])
    return events


def filter_noise(service: str, events: list):
    kept = [e for e in events if not is_noise_template(service, e["template"])]
    dropped = len(events) - len(kept)
    return kept, dropped


def build_hmm_sequence(events: list):
    """Plain ordered template_id list -- exactly what hmmlearn's
    CategoricalHMM/MultinomialHMM fits on. template_ids are drain3's
    real, persisted cluster IDs from the pipeline run, stable across
    time since the miner state is saved/loaded each run."""
    return [e["template_id"] for e in events]


def build_spc_vectors(events: list, real_template_ids: list):
    """Per-time-bucket real feature vector: template-frequency counts
    (the "mix" Kimi's review flagged -- e.g. Health spiking while List
    vanishes) plus inter-arrival-time stats within the bucket (the
    "cadence" signal -- a memory leak or CPU throttle often stretches
    the gap between events before the mix itself changes).

    Real fix, 2026-07-29: emits ONE ROW PER BUCKET across the full
    observed time range, zero-filled where nothing happened, not just
    buckets that had at least one event. SPC/EWMA needs a continuous,
    fixed-cadence timeline -- a service going silent is exactly the
    kind of thing this track should catch, and a MISSING row is
    indistinguishable from "we didn't collect data here," while an
    explicit all-zero row is a real, meaningful data point."""
    buckets = defaultdict(list)  # bucket_idx -> list of (ts_ns, template_id)
    for e in events:
        bucket_idx = int(e["ts_ns"] / 1e9) // BUCKET_SECONDS
        buckets[bucket_idx].append((e["ts_ns"], e["template_id"]))

    first_bucket = min(buckets.keys())
    last_bucket = max(buckets.keys())

    rows = []
    for bucket_idx in range(first_bucket, last_bucket + 1):
        entries = sorted(buckets.get(bucket_idx, []))
        counts = defaultdict(int)
        for _, tid in entries:
            counts[tid] += 1

        gaps_s = []
        for i in range(1, len(entries)):
            gap_ns = entries[i][0] - entries[i - 1][0]
            gaps_s.append(gap_ns / 1e9)

        rows.append({
            "bucket_start_s": bucket_idx * BUCKET_SECONDS,
            "counts": {str(tid): counts.get(tid, 0) for tid in real_template_ids},
            "total_events": len(entries),
            "mean_gap_s": round(statistics.mean(gaps_s), 4) if gaps_s else None,
            "max_gap_s": round(max(gaps_s), 4) if gaps_s else None,
        })
    return rows


def process_service(service: str):
    print(f"[{service}]")
    events = load_events(service)
    print(f"  {len(events)} real events loaded.")

    filtered, dropped = filter_noise(service, events)
    if dropped:
        print(f"  filtered out {dropped} noise events "
              f"({100 * dropped / len(events):.1f}%).")
    print(f"  {len(filtered)} real events remain for feature extraction.")

    if not filtered:
        print(f"  WARNING: nothing left after filtering -- skipping {service}.")
        return

    real_template_ids = sorted({e["template_id"] for e in filtered})
    print(f"  real template vocabulary after filtering: {real_template_ids}")

    hmm_seq = build_hmm_sequence(filtered)
    spc_rows = build_spc_vectors(filtered, real_template_ids)

    os.makedirs(OUT_DIR, exist_ok=True)
    hmm_path = os.path.join(OUT_DIR, f"{service}_hmm_sequence.json")
    with open(hmm_path, "w") as f:
        json.dump({"template_ids": real_template_ids, "sequence": hmm_seq}, f)
    print(f"  wrote {hmm_path} ({len(hmm_seq)} events)")

    spc_path = os.path.join(OUT_DIR, f"{service}_spc_vectors.jsonl")
    with open(spc_path, "w") as f:
        for row in spc_rows:
            f.write(json.dumps(row) + "\n")
    print(f"  wrote {spc_path} ({len(spc_rows)} time buckets)")
    print()


def main():
    for service in DEEPLOG_TIER_2_THIN:
        process_service(service)


if __name__ == "__main__":
    main()
