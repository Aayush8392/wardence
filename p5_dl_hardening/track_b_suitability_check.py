"""
Real, principled upfront test for whether a service belongs on the HMM
track or the SPC track -- per Kimi's review 07 follow-up
(reviews/07_deeplog_coverage_kimi_review.md), confirming the empirical
finding that `catalogue`'s 25.8% HMM false-positive rate wasn't a bug,
but a correct model describing a genuinely low-determinism process.

Computes, from each service's already-fitted HMM (fit_track_b_hmm.py's
output):
  - Determinism ratio D: mean of each state's highest outgoing
    transition probability. D > 0.85 -> HMM-suitable. D < 0.60 ->
    SPC-suitable.
  - Normalized entropy rate H_norm: corroborating signal, same
    direction (< 0.3 HMM-suitable, > 0.7 SPC-suitable).
  - Exposure at the real WINDOW_SIZE used by hmm_anomaly_scorer.py:
    1 - (1 - p_min)^W. If this exceeds the target FPR, min-per-step
    scoring is disqualified outright, before ever calibrating a
    threshold against real data.

Meant to be run BEFORE building a full HMM anomaly scorer for any
future Track-B candidate service -- would have predicted, without any
empirical calibration, exactly what we found the hard way for
`catalogue` vs `queue-master`.

Usage:
    python3 track_b_suitability_check.py
"""

import os

import numpy as np

import json

from deeplog_service_config import DEEPLOG_TIER_2_THIN
from hmm_anomaly_scorer import WINDOW_SIZE, ANOMALY_PERCENTILE, HELD_OUT_FRACTION

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_B_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_b")

TARGET_FPR = ANOMALY_PERCENTILE / 100  # e.g. 1st percentile -> 0.01 target


def load_transmat(service: str):
    path = os.path.join(TRACK_B_DIR, f"{service}_hmm_model.npz")
    data = np.load(path)
    return data["startprob"], data["transmat"]


def real_empirical_p_min(service: str) -> float:
    """Real fix, 2026-07-29: the fitted transmat_'s raw floating-point
    values include near-zero numerical residuals from Baum-Welch EM
    (a fully-connected model asymptotically approaches but rarely hits
    exact zero for a "never observed" transition), which badly deflate
    a naive min-nonzero-entry reading -- confirmed directly: it gave
    catalogue an exposure of 0.0000 against Kimi's real ~0.88 estimate.
    Real fix: count ACTUAL observed transitions in the real training
    sequence (same chronological split fit_track_b_hmm.py used) and
    take the smallest genuinely-observed empirical frequency -- grounded
    in what really happened, not a fitted matrix's numerical tail."""
    path = os.path.join(TRACK_B_DIR, f"{service}_hmm_sequence.json")
    with open(path) as f:
        data = json.load(f)
    template_ids = data["template_ids"]
    sequence = data["sequence"]
    id_to_index = {tid: i for i, tid in enumerate(template_ids)}

    split_idx = int(len(sequence) * (1 - HELD_OUT_FRACTION))
    train_symbols = [id_to_index[tid] for tid in sequence[:split_idx]]

    n = len(template_ids)
    counts = np.zeros((n, n))
    for i in range(len(train_symbols) - 1):
        counts[train_symbols[i], train_symbols[i + 1]] += 1

    row_sums = counts.sum(axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        probs = np.where(row_sums > 0, counts / row_sums, 0)

    nonzero = probs[probs > 0]
    return float(nonzero.min()) if len(nonzero) else 0.0


def determinism_ratio(transmat: np.ndarray) -> float:
    """D = mean over states of that state's highest outgoing
    transition probability. Only counts states with real non-zero
    outgoing mass (a state drain3/the HMM never actually transitions
    out of contributes no real information either way)."""
    row_sums = transmat.sum(axis=1)
    active_rows = transmat[row_sums > 1e-12]
    if len(active_rows) == 0:
        return float("nan")
    return float(np.mean(active_rows.max(axis=1)))


def normalized_entropy_rate(startprob: np.ndarray, transmat: np.ndarray) -> float:
    """H = -sum_i,j pi_i * P_ij * log2(P_ij), using the real stationary
    distribution pi (solved from the transition matrix itself, not
    approximated by startprob -- startprob is the t=0 distribution,
    pi is the real long-run one). H_norm = H / log2(N)."""
    n = transmat.shape[0]
    # Real stationary distribution: left eigenvector of transmat for
    # eigenvalue 1, normalized to sum to 1.
    eigvals, eigvecs = np.linalg.eig(transmat.T)
    idx = np.argmin(np.abs(eigvals - 1))
    pi = np.real(eigvecs[:, idx])
    pi = pi / pi.sum()
    pi = np.clip(pi, 0, None)  # guard tiny negative numerical noise
    pi = pi / pi.sum()

    h = 0.0
    for i in range(n):
        for j in range(n):
            p = transmat[i, j]
            if p > 1e-12:
                h -= pi[i] * p * np.log2(p)
    max_h = np.log2(n) if n > 1 else 1.0
    return float(h / max_h) if max_h > 0 else 0.0


def exposure_at_window(p_min: float, window_size: int) -> float:
    """Real coupon-collector exposure: 1 - (1 - p_min)^W, using the
    smallest REAL EMPIRICAL transition probability (real_empirical_p_min),
    not the fitted matrix's raw floating-point tail."""
    return 1 - (1 - p_min) ** window_size


def recommend(D: float, H_norm: float, exposure: float, target_fpr: float) -> str:
    """Kimi's real decision rule, branching on D and H_norm, with the
    exposure check as an explicit override -- if a service's minimum
    real transition would blow past the target FPR at the real window
    size in use, min-per-step is disqualified regardless of what D/H
    alone would suggest."""
    if exposure > target_fpr and D < 0.85:
        return "SPC primary (exposure check disqualifies min-per-step HMM outright)"
    if D > 0.85 and H_norm < 0.3:
        return "HMM primary (min-per-step or full-sequence)"
    if D > 0.60 and H_norm < 0.70:
        return "SPC primary, HMM secondary (full-sequence only, lenient threshold)"
    return "SPC primary, HMM optional/very weak"


def check_service(service: str):
    print(f"[{service}]")
    startprob, transmat = load_transmat(service)
    D = determinism_ratio(transmat)
    H_norm = normalized_entropy_rate(startprob, transmat)
    p_min = real_empirical_p_min(service)
    exposure = exposure_at_window(p_min, WINDOW_SIZE)

    print(f"  determinism ratio D:        {D:.4f}")
    print(f"  normalized entropy H_norm:  {H_norm:.4f}")
    print(f"  real empirical p_min:       {p_min:.4f}")
    print(f"  exposure at window={WINDOW_SIZE}:     {exposure:.4f} "
          f"(target FPR: {TARGET_FPR:.4f})")
    print(f"  -> {recommend(D, H_norm, exposure, TARGET_FPR)}")
    print()


def main():
    for service in DEEPLOG_TIER_2_THIN:
        check_service(service)


if __name__ == "__main__":
    main()
