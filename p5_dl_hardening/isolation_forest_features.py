"""
Isolation Forest baseline: builds the real windowed template-frequency
feature vectors it needs, for the same 3 DeepLog Tier-1 services
(front-end/orders/user) -- see wardence_context.md's locked "Benchmarked
vs Isolation Forest (5-fold time-series CV, FPR <5% target)" spec.

Isolation Forest doesn't do sequence prediction like DeepLog's LSTM (next-
template forecasting) -- it scores fixed-size feature vectors for
outlier-ness. So this builds a DIFFERENT input shape from Track A's
(context, next_id) pairs: one frequency-count vector per fixed-size
window (how many times each real template appeared in that window).

Two real decisions carried over deliberately, not re-derived from
scratch, to keep the eventual comparison apples-to-apples:
  - Window size = SCORING_WINDOW (20), imported directly from
    deeplog_anomaly_scorer rather than a fresh literal -- DeepLog's own
    anomaly scorer already aggregates hit/miss over 20-event windows,
    so scoring Isolation Forest at the same granularity is what makes
    the two methods' FPRs genuinely comparable, not an apples-to-oranges
    window-size mismatch.
  - Non-overlapping windows (stride = window), same choice Track A/B's
    calibration already made -- overlapping windows are autocorrelated,
    which broke percentile-based calibration once already (Track B's
    HMM scorer). No reason to re-learn that lesson here.

Reads from the real, already-accumulated pipeline stream
(log_sequence_pipeline.py's output) -- no fresh Loki query needed.

Usage:
    python3 isolation_forest_features.py
"""

import json
import os

import numpy as np

from deeplog_anomaly_scorer import SCORING_WINDOW
from deeplog_service_config import DEEPLOG_TIER_1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "streams")
OUT_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "isolation_forest")


def load_sequence(service: str):
    path = os.path.join(STREAM_DIR, f"{service}.jsonl")
    ids = []
    with open(path) as f:
        for line in f:
            ids.append(json.loads(line)["template_id"])
    return ids


def build_frequency_windows(sequence: list, window: int, vocab: list):
    """Real, non-overlapping (stride=window) frequency-count vectors, in
    chronological order -- row i covers events
    [i*window : (i+1)*window). A trailing partial window (fewer than
    `window` real events left) is dropped, same "don't fabricate a
    short window" discipline as every other windowed feature in this
    project."""
    vocab_index = {tid: i for i, tid in enumerate(vocab)}
    n_windows = len(sequence) // window
    vectors = np.zeros((n_windows, len(vocab)), dtype=np.int64)
    for w in range(n_windows):
        chunk = sequence[w * window:(w + 1) * window]
        for tid in chunk:
            vectors[w, vocab_index[tid]] += 1
    return vectors


def process_service(service: str):
    print(f"[{service}]")
    sequence = load_sequence(service)
    vocab = sorted(set(sequence))
    print(f"  {len(sequence)} real events, {len(vocab)} real templates")

    vectors = build_frequency_windows(sequence, SCORING_WINDOW, vocab)
    print(f"  {len(vectors)} real non-overlapping {SCORING_WINDOW}-event "
          f"frequency-vector windows built")

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{service}_windows.npz")
    np.savez(
        out_path,
        vocab=np.array(vocab, dtype=np.int64),
        vectors=vectors,
        window_size=np.array(SCORING_WINDOW),
    )
    print(f"  wrote {out_path}")
    print()


def main():
    for service in DEEPLOG_TIER_1:
        process_service(service)


if __name__ == "__main__":
    main()
