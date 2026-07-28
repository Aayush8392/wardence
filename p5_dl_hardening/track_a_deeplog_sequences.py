"""
Track A: builds the real sliding-window (window, next_id) sequences for
DeepLog, for front-end/orders/user -- see check_deeplog_window_size.py
for the real, data-driven window-size check this is built on (window=10
confirmed as a reasonable elbow point across all 3 services' real
entropy curves, not copied blindly from DeepLog's original paper).

Reads from the real, already-accumulated pipeline stream
(log_sequence_pipeline.py's output) -- no fresh Loki query needed.

Real THREE-WAY chronological split (train/val/test), not the two-way
split Track B used -- Track A involves real gradient-based LSTM
training, which needs a genuine validation signal (for early stopping,
to detect overfitting DURING training) that's distinct from the final
held-out test set (touched only once, after training decisions are
already locked in). Track B's simpler statistical fits (HMM Baum-Welch,
EWMA/CUSUM baselines) never needed that middle set. Never shuffled --
this is time-series; shuffling would leak future patterns into training.

Usage:
    python3 track_a_deeplog_sequences.py
"""

import json
import os

import numpy as np

from deeplog_service_config import DEEPLOG_TIER_1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "streams")
OUT_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_a")

WINDOW_SIZE = 10  # locked via check_deeplog_window_size.py, real entropy-elbow + sample-density check

TRAIN_FRACTION = 0.70
VAL_FRACTION = 0.15
TEST_FRACTION = 0.15  # train + val + test == 1.0


def load_sequence(service: str):
    path = os.path.join(STREAM_DIR, f"{service}.jsonl")
    ids = []
    with open(path) as f:
        for line in f:
            ids.append(json.loads(line)["template_id"])
    return ids


def build_windows(sequence: list, window: int):
    """Real, standard next-token construction: every overlapping
    (window, next_id) pair, stride=1 -- DeepLog trains on ALL real
    windows, not a sampled subset."""
    X, y = [], []
    for i in range(len(sequence) - window):
        X.append(sequence[i:i + window])
        y.append(sequence[i + window])
    return np.array(X, dtype=np.int64), np.array(y, dtype=np.int64)


def chronological_split(X: np.ndarray, y: np.ndarray):
    n = len(X)
    train_end = int(n * TRAIN_FRACTION)
    val_end = int(n * (TRAIN_FRACTION + VAL_FRACTION))
    return {
        "train": (X[:train_end], y[:train_end]),
        "val": (X[train_end:val_end], y[train_end:val_end]),
        "test": (X[val_end:], y[val_end:]),
    }


def process_service(service: str):
    print(f"[{service}]")
    sequence = load_sequence(service)
    vocab = sorted(set(sequence))
    n_vocab = len(vocab)
    print(f"  {len(sequence)} real events, {n_vocab} real templates -> vocab size {n_vocab}")

    X, y = build_windows(sequence, WINDOW_SIZE)
    print(f"  {len(X)} real (window={WINDOW_SIZE}, next_id) pairs built")

    splits = chronological_split(X, y)
    for name, (X_split, y_split) in splits.items():
        print(f"    {name}: {len(X_split)} real pairs "
              f"({100 * len(X_split) / len(X):.1f}%)")

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{service}_sequences.npz")
    np.savez(
        out_path,
        vocab=np.array(vocab, dtype=np.int64),
        X_train=splits["train"][0], y_train=splits["train"][1],
        X_val=splits["val"][0], y_val=splits["val"][1],
        X_test=splits["test"][0], y_test=splits["test"][1],
        window_size=np.array(WINDOW_SIZE),
    )
    print(f"  wrote {out_path}")
    print()


def main():
    assert abs(TRAIN_FRACTION + VAL_FRACTION + TEST_FRACTION - 1.0) < 1e-9, \
        "split fractions must sum to 1.0"
    for service in DEEPLOG_TIER_1:
        process_service(service)


if __name__ == "__main__":
    main()
