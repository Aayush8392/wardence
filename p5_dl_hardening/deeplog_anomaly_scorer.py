"""
Real anomaly-scoring function for Track A's trained LSTMs (front-end/
orders/user) -- see train_track_a_lstm.py for the fit itself.

DeepLog's real anomaly criterion: for a given real context window, is
the ACTUAL next event within the model's top-K predicted candidates?
A single miss isn't necessarily anomalous (even a well-fit model misses
sometimes -- see the real top-1 accuracies: 81-97%). Real signal is the
MISS RATE over a scoring window of consecutive real predictions, same
aggregation-over-a-window principle as Track B's HMM scorer, and
calibrated the same way, applying every real lesson learned building
that one:
  - Threshold is a PERCENTILE of the real held-out miss-rate
    distribution, not mean-K*std (Track B's queue-master min-log-prob
    calibration showed percentile is the safer, distribution-agnostic
    choice).
  - Calibration uses NON-OVERLAPPING scoring windows (confirmed
    necessary in Track B: overlapping windows are autocorrelated,
    which broke percentile calibration the first time).
  - Comparisons use <=, not < (Track B's tie-boundary bug -- a
    percentile-derived threshold can land exactly on a real, populated
    value).
  - The synthetic corrupted test is built by querying the model's OWN
    predicted distribution for the real least-likely next event given
    a real context, not a generic heuristic (Track B's first two
    corrupted-sequence attempts were both wrong in instructive ways
    for exactly this reason).

Uses the TEST split (already built by track_a_deeplog_sequences.py,
untouched during training/model-selection decisions) for calibration --
analogous to how Track B calibrated against its held-out split.

Requires: pip install torch

Usage:
    python3 deeplog_anomaly_scorer.py
"""

import os

import numpy as np
import torch
import torch.nn as nn
from scipy.stats import binom

from deeplog_service_config import DEEPLOG_TIER_1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_A_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_a")

TOP_K = 5  # matches the top-5 metric already reported during training
SCORING_WINDOW = 20  # consecutive real predictions per aggregate miss-rate, matches Track B's WINDOW_SIZE
ANOMALY_PERCENTILE = 99  # miss-rate is "higher = more anomalous", so the upper-tail percentile

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class DeepLogLSTM(nn.Module):
    def __init__(self, vocab_size: int, embedding_dim: int, hidden_size: int):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim)
        self.lstm = nn.LSTM(embedding_dim, hidden_size, num_layers=1, batch_first=True)
        self.output = nn.Linear(hidden_size, vocab_size)

    def forward(self, x):
        emb = self.embedding(x)
        _, (h_n, _) = self.lstm(emb)
        return self.output(h_n[-1])


def load_model_and_data(service: str):
    seq_path = os.path.join(TRACK_A_DIR, f"{service}_sequences.npz")
    seq_data = np.load(seq_path)
    vocab = list(seq_data["vocab"])
    id_to_index = {tid: i for i, tid in enumerate(vocab)}

    model_path = os.path.join(TRACK_A_DIR, f"{service}_lstm.pt")
    checkpoint = torch.load(model_path, map_location=DEVICE, weights_only=True)
    model = DeepLogLSTM(checkpoint["vocab_size"], checkpoint["embedding_dim"], checkpoint["hidden_size"])
    model.load_state_dict(checkpoint["state_dict"])
    model.to(DEVICE).eval()

    X_test = np.vectorize(id_to_index.get)(seq_data["X_test"]).astype(np.int64)
    y_test = np.vectorize(id_to_index.get)(seq_data["y_test"]).astype(np.int64)

    return model, id_to_index, X_test, y_test


def predict_hits(model, X: np.ndarray, y: np.ndarray, k: int):
    """Real per-sample hit/miss: is y_i within the model's real top-k
    prediction for context X_i? Returns a boolean array, one entry per
    real (context, actual_next) sample -- consecutive entries correspond
    to consecutive real timeline positions, since X/y were built with a
    chronological, never-shuffled split."""
    with torch.no_grad():
        X_t = torch.from_numpy(X).to(DEVICE)
        logits = model(X_t)
        top_k_idx = logits.topk(min(k, logits.shape[1]), dim=1).indices.cpu().numpy()
    y_col = y.reshape(-1, 1)
    hits = (top_k_idx == y_col).any(axis=1)
    return hits


def windowed_miss_rates(hits: np.ndarray, window: int, stride: int):
    rates = []
    for start in range(0, len(hits) - window + 1, stride):
        chunk = hits[start:start + window]
        rates.append(1 - chunk.mean())  # miss rate
    return np.array(rates)


def calibrate_threshold(hits: np.ndarray):
    """Real threshold via a Binomial model, not an empirical percentile
    of windowed miss rates. Real bug found and fixed, 2026-07-29:
    percentile calibration broke for `orders`/`user`, whose real top-5
    hit rate is 100.0% -- their held-out miss-rate distribution
    collapses to a constant 0, so the 99th percentile of an all-zero
    array is trivially 0, and every future normal window (also ~0
    misses) then satisfies `miss_rate >= 0` and gets flagged (confirmed:
    100% false-positive rate). Real, more principled fix: model the
    number of misses in a SCORING_WINDOW as Binomial(n=SCORING_WINDOW,
    p=real_single_event_miss_rate), using the FULL test set's miss rate
    (a much larger, more stable estimate than any windowed empirical
    sample) as p. Pick the smallest miss-count k such that
    P(misses >= k) <= target FPR -- this correctly handles both a
    near-perfect model (small p -> even k=1 is already rare enough) and
    a real relative baseline (front-end's real p~=3.1% independently
    reproduces the same 0.20 threshold the percentile method already
    got right there, confirming this approach doesn't regress the case
    that was already working)."""
    p_miss = float(1 - hits.mean())
    target_fpr = 1 - ANOMALY_PERCENTILE / 100

    k = 1
    while k <= SCORING_WINDOW:
        p_at_least_k = float(binom.sf(k - 1, SCORING_WINDOW, p_miss))  # P(X >= k)
        if p_at_least_k <= target_fpr:
            break
        k += 1

    threshold = k / SCORING_WINDOW
    rates = windowed_miss_rates(hits, SCORING_WINDOW, SCORING_WINDOW)
    return threshold, rates, p_miss, k


def synthetic_corrupted_test(model, X_test: np.ndarray, threshold: float, index_to_id: dict):
    """Real corrupted scoring window, grounded in the model's OWN
    predicted distribution -- takes one real context from the test set,
    finds whichever real template the model itself considers LEAST
    likely to follow it, and forces that as the observed "next" event
    repeatedly across a full scoring window. Not a generic heuristic --
    directly queries the same model whose threshold we're testing."""
    sample_context = X_test[len(X_test) // 2:len(X_test) // 2 + 1]
    with torch.no_grad():
        logits = model(torch.from_numpy(sample_context).to(DEVICE))
        least_likely_idx = int(logits.argmin(dim=1).item())

    forced_y = np.full(SCORING_WINDOW, least_likely_idx, dtype=np.int64)
    forced_X = np.repeat(sample_context, SCORING_WINDOW, axis=0)

    hits = predict_hits(model, forced_X, forced_y, TOP_K)
    miss_rate = 1 - hits.mean()
    flagged = miss_rate >= threshold
    return miss_rate, flagged


def score_service(service: str):
    print(f"[{service}]")
    model, id_to_index, X_test, y_test = load_model_and_data(service)
    index_to_id = {v: k for k, v in id_to_index.items()}

    hits = predict_hits(model, X_test, y_test, TOP_K)
    print(f"  {len(hits)} real test predictions, top-{TOP_K} hit rate: {hits.mean() * 100:.1f}%")

    threshold, held_out_rates, p_miss, k = calibrate_threshold(hits)
    print(f"  real single-event miss probability (full test set): {p_miss:.4f}")
    print(f"  real Binomial-derived threshold: {k}/{SCORING_WINDOW} misses "
          f"(P(misses>={k}) <= {1 - ANOMALY_PERCENTILE / 100:.4f} target FPR) -> rate={threshold:.4f}")

    false_positives = int((held_out_rates >= threshold).sum())
    fp_rate = false_positives / len(held_out_rates)
    print(f"  real held-out self-check: {false_positives}/{len(held_out_rates)} windows "
          f"flagged ({fp_rate * 100:.1f}% false-positive rate on known-normal data)")

    corrupted_rate, corrupted_flagged = synthetic_corrupted_test(model, X_test, threshold, index_to_id)
    print(f"  synthetic corrupted-sequence check (forced to the model's own least-likely "
          f"prediction): miss_rate={corrupted_rate:.4f} -> "
          f"{'FLAGGED (correct)' if corrupted_flagged else 'NOT flagged (unexpected)'}")
    print()


def main():
    for service in DEEPLOG_TIER_1:
        score_service(service)


if __name__ == "__main__":
    main()
