"""
Isolation Forest baseline -- fits, cross-validates, and scores the
windowed frequency-vector features (isolation_forest_features.py) for
the 3 DeepLog Tier-1 services, per wardence_context.md's locked spec:
"Benchmarked vs Isolation Forest (5-fold time-series CV, FPR <5%
target)". The point isn't to ship this as a production detector -- it's
the sanity check that justifies using DeepLog's LSTM at all: does the
LSTM actually beat a simple, off-the-shelf unsupervised method on the
same data, at the same window granularity?

Real methodology, matching every other detector's calibration
discipline already proven in this project:
  - 5-FOLD TIME-SERIES CV (sklearn's TimeSeriesSplit -- expanding
    window, never shuffled): fold k trains on everything before a
    cutoff, scores everything after it. Distinct from Track A's single
    chronological 70/15/15 split -- the locked spec calls for CV here
    specifically, not the simpler split.
  - Threshold = the 5th PERCENTILE of the fold's own TRAIN scores
    (IsolationForest's decision_function: higher = more normal, lower/
    negative = more anomalous), not a fixed contamination guess --
    target FPR <5% means "at most 5% of genuinely normal windows get
    flagged", so the threshold is picked directly from that target,
    same "derive the threshold from the real target, don't guess"
    discipline as deeplog_anomaly_scorer's Binomial threshold.
  - Real synthetic-anomaly check, same discipline as every other
    detector: NOT a generic heuristic -- forces a window built entirely
    from whichever real template is LEAST frequent in the full training
    data (the frequency-vector-space equivalent of DeepLog's "force the
    model's own least-likely next event"), fit and scored against a
    model trained on the complete real dataset.

Requires: pip install scikit-learn

Usage:
    python3 fit_isolation_forest_baseline.py
"""

import os

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.model_selection import TimeSeriesSplit

from deeplog_service_config import DEEPLOG_TIER_1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IF_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "isolation_forest")

N_SPLITS = 5  # locked spec: "5-fold time-series CV"
TARGET_FPR = 0.05  # locked spec: "FPR <5% target"
RANDOM_STATE = 42  # fixed, for a reproducible real result across reruns
N_ESTIMATORS = 100  # sklearn's own default, no reason found to deviate
# Real test run and REJECTED, 2026-07-29 (Kimi review 08's Test A):
# max_samples=1.0 (every tree sees the full real train fold, instead of
# sklearn's default 256-sample subsample) was tested directly -- it made
# front-end's real fold instability WORSE, not better (mean FPR 9.9% ->
# 14.8%, fold range widened to 9.1-24.5%), confirming subsampling was
# NOT the cause. Reverted to sklearn's real default ('auto'), which both
# performs better AND is the more honest "off-the-shelf baseline"
# framing -- don't re-guess this without new evidence.
MAX_SAMPLES = "auto"


def load_windows(service: str):
    path = os.path.join(IF_DIR, f"{service}_windows.npz")
    data = np.load(path)
    return data["vocab"], data["vectors"]


def fit_and_threshold(train_vectors: np.ndarray):
    """Fits one real IsolationForest on `train_vectors`, then derives a
    threshold directly from the target FPR against the model's OWN
    train-set scores -- same "threshold from the real target, not a
    guessed contamination" discipline as every other calibrated
    detector in this project."""
    model = IsolationForest(n_estimators=N_ESTIMATORS, max_samples=MAX_SAMPLES, random_state=RANDOM_STATE)
    model.fit(train_vectors)
    train_scores = model.decision_function(train_vectors)
    threshold = float(np.percentile(train_scores, TARGET_FPR * 100))
    return model, threshold


def cross_validate_service(service: str, vocab: np.ndarray, vectors: np.ndarray):
    tscv = TimeSeriesSplit(n_splits=N_SPLITS)
    fold_results = []
    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(vectors), start=1):
        train_vectors, test_vectors = vectors[train_idx], vectors[test_idx]
        model, threshold = fit_and_threshold(train_vectors)

        test_scores = model.decision_function(test_vectors)
        flagged = int((test_scores < threshold).sum())
        fpr = flagged / len(test_scores)
        fold_results.append(fpr)
        print(f"    fold {fold_idx}: train={len(train_vectors)}, test={len(test_vectors)}, "
              f"threshold={threshold:.4f}, real held-out FPR={fpr * 100:.1f}% "
              f"({flagged}/{len(test_scores)} windows flagged)")
    return fold_results


def synthetic_corrupted_test(vocab: np.ndarray, vectors: np.ndarray):
    """Real anomaly check: fits on the FULL real dataset (this is the
    "does it notice a genuine anomaly at all" check, not a held-out
    generalization measurement -- same role as Track A's corrupted-
    sequence test), then forces a window entirely composed of whichever
    real template is LEAST frequent across all real training data --
    the frequency-vector-space equivalent of "force the model's own
    least-likely next event", not an arbitrary corruption."""
    model, threshold = fit_and_threshold(vectors)

    real_totals = vectors.sum(axis=0)
    rarest_idx = int(np.argmin(real_totals))
    window_size = int(vectors.sum(axis=1)[0])  # every real window sums to the same fixed window size

    corrupted = np.zeros((1, len(vocab)), dtype=np.int64)
    corrupted[0, rarest_idx] = window_size

    corrupted_score = model.decision_function(corrupted)[0]
    flagged = corrupted_score < threshold
    return corrupted_score, threshold, flagged, int(vocab[rarest_idx])


# Real, fairer anomaly check added 2026-07-29 (Kimi review 08, point 3):
# the 100%-single-template corrupted test is a strawman -- a real fault
# rarely makes every request in a window hit one rare template. A more
# realistic frequency-space anomaly is a MIXTURE SHIFT: one operation's
# share collapses while another's rises, still a real, plausible
# distribution shape, not an extreme simplex corner.
MIXTURE_SHIFT_FRACTION = 0.30  # real fraction of window mass redistributed, per Kimi's suggestion


def mixture_shift_test(vocab: np.ndarray, vectors: np.ndarray):
    """Perturbs one REAL, actual normal window (not a synthetic
    from-scratch construction) by redistributing MIXTURE_SHIFT_FRACTION
    of the window's real mass from its most common real template to its
    rarest real template, keeping the total at the fixed window size --
    a real, plausible "one operation collapses, another spikes" shape,
    harder and more representative than 100% single-template
    domination."""
    model, threshold = fit_and_threshold(vectors)
    window_size = int(vectors.sum(axis=1)[0])
    real_totals = vectors.sum(axis=0)
    common_idx = int(np.argmax(real_totals))
    rare_idx = int(np.argmin(real_totals))

    base_window = vectors[len(vectors) // 2].copy()  # one real, ordinary window, not cherry-picked
    shift = min(int(round(MIXTURE_SHIFT_FRACTION * window_size)), int(base_window[common_idx]))
    shifted_window = base_window.copy()
    shifted_window[common_idx] -= shift
    shifted_window[rare_idx] += shift

    base_score = model.decision_function(base_window.reshape(1, -1))[0]
    shifted_score = model.decision_function(shifted_window.reshape(1, -1))[0]
    base_flagged = base_score < threshold
    shifted_flagged = shifted_score < threshold
    return (base_score, base_flagged, shifted_score, shifted_flagged,
            threshold, shift, int(vocab[common_idx]), int(vocab[rare_idx]))


# Real, protocol-symmetric comparison added 2026-07-29 (Kimi review 08,
# point 4): DeepLog uses a single chronological 70/15/15 split
# (track_a_deeplog_sequences.py); the 5-fold CV above uses a genuinely
# different real methodology. 0.85/0.15 here matches DeepLog's combined
# train+val (0.70+0.15) vs. its held-out test (0.15) -- both models then
# get scored against the exact same real LAST-15%-of-time slice of the
# underlying event stream, closing the "the difference might be
# evaluation protocol, not model quality" gap Kimi raised.
SAME_HOLDOUT_TRAIN_FRACTION = 0.85


def same_holdout_comparison(vectors: np.ndarray):
    n = len(vectors)
    split_idx = int(n * SAME_HOLDOUT_TRAIN_FRACTION)
    train_vectors, test_vectors = vectors[:split_idx], vectors[split_idx:]
    model, threshold = fit_and_threshold(train_vectors)
    test_scores = model.decision_function(test_vectors)
    flagged = int((test_scores < threshold).sum())
    fpr = flagged / len(test_vectors) if len(test_vectors) else float("nan")
    return fpr, len(train_vectors), len(test_vectors)


def score_service(service: str):
    print(f"[{service}]")
    vocab, vectors = load_windows(service)
    print(f"  {len(vectors)} real {int(vectors.sum(axis=1)[0])}-event windows, "
          f"{len(vocab)} real templates")

    fold_fprs = cross_validate_service(service, vocab, vectors)
    mean_fpr = float(np.mean(fold_fprs))
    print(f"  real mean held-out FPR across {N_SPLITS} folds: {mean_fpr * 100:.1f}% "
          f"(target <{TARGET_FPR * 100:.0f}%)")

    corrupted_score, threshold, flagged, rarest_template_id = synthetic_corrupted_test(vocab, vectors)
    print(f"  synthetic corrupted-window check (window forced entirely to real rarest "
          f"template id={rarest_template_id}): score={corrupted_score:.4f} vs "
          f"threshold={threshold:.4f} -> "
          f"{'FLAGGED (correct)' if flagged else 'NOT flagged (unexpected)'}")

    (base_score, base_flagged, shifted_score, shifted_flagged, mix_threshold,
     shift, common_id, rare_id) = mixture_shift_test(vocab, vectors)
    print(f"  mixture-shift check ({MIXTURE_SHIFT_FRACTION * 100:.0f}% of window mass moved "
          f"from real common template id={common_id} to real rare template id={rare_id}, "
          f"shift={shift}/{int(vectors.sum(axis=1)[0])} events):")
    print(f"    base real window:    score={base_score:.4f} -> "
          f"{'flagged' if base_flagged else 'not flagged'} (expected: not flagged)")
    print(f"    shifted real window: score={shifted_score:.4f} vs threshold={mix_threshold:.4f} -> "
          f"{'FLAGGED (correct)' if shifted_flagged else 'NOT flagged (unexpected)'}")

    same_holdout_fpr, train_n, test_n = same_holdout_comparison(vectors)
    print(f"  same-held-out-set check (train={train_n}, test=last {test_n} windows, "
          f"{SAME_HOLDOUT_TRAIN_FRACTION * 100:.0f}/{100 - SAME_HOLDOUT_TRAIN_FRACTION * 100:.0f} "
          f"split, matching DeepLog's own train+val/test proportions): "
          f"real FPR on this exact test slice = {same_holdout_fpr * 100:.1f}%")
    print()
    return mean_fpr, same_holdout_fpr


def main():
    print(f"Isolation Forest baseline: {N_SPLITS}-fold time-series CV, "
          f"target FPR <{TARGET_FPR * 100:.0f}%\n")
    results = {}
    for service in DEEPLOG_TIER_1:
        results[service] = score_service(service)

    print("-" * 60)
    print("Summary -- 5-fold CV mean FPR vs. same-held-out-set FPR "
          "(compare the latter directly against Track A's real held-out "
          "FPRs: front-end 0.2%, orders 0.0%, user 0.0%):")
    for service, (mean_fpr, same_holdout_fpr) in results.items():
        print(f"  {service:<12} 5-fold CV mean: {mean_fpr * 100:>5.1f}%   "
              f"same-held-out-set: {same_holdout_fpr * 100:>5.1f}%")


if __name__ == "__main__":
    main()
