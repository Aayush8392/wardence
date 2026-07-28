"""
Fits a small Categorical HMM (Baum-Welch) per Track-B service (catalogue,
queue-master), on the real, noise-filtered template_id sequences
produced by track_b_hmm_spc_features.py -- see wardence_context.md's
"DeepLog real per-service scope LOCKED" section and
reviews/07_deeplog_coverage_kimi_review.md for why an HMM (not an LSTM)
is the right tool at these services' small real vocabulary size (3 and
2 templates after noise-filtering).

n_states defaults to n_templates -- a small state count, per Kimi's
"3-state/5-state HMM" recommendation, not a deep model. Real chronological
train/held-out split (never shuffled -- this is time-series, shuffling
would leak future patterns into training, same discipline as Track A's
window split). The real check that matters: does the fitted model
generalize, or did it just memorize noise? Compares mean per-event
log-likelihood on train vs. held-out -- a real model should be close on
both; a large held-out drop signals overfitting to a state count that's
too high for the real data volume.

Requires: pip install hmmlearn

Usage:
    python3 fit_track_b_hmm.py
"""

import json
import os

import numpy as np
from hmmlearn import hmm

from deeplog_service_config import DEEPLOG_TIER_2_THIN

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_B_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_b")

HELD_OUT_FRACTION = 0.2  # last 20% of the real sequence, chronologically -- never shuffled
N_HMM_FIT_ATTEMPTS = 5   # hmmlearn's EM can land in a local optimum -- keep the best of several real fits


def load_sequence(service: str):
    path = os.path.join(TRACK_B_DIR, f"{service}_hmm_sequence.json")
    with open(path) as f:
        data = json.load(f)
    return data["template_ids"], data["sequence"]


def to_symbol_index(sequence: list, template_ids: list):
    """hmmlearn's CategoricalHMM needs 0-indexed contiguous symbols, not
    our real drain3 cluster IDs (which can be any integer) -- real,
    necessary remapping, not arbitrary."""
    id_to_index = {tid: i for i, tid in enumerate(template_ids)}
    return np.array([[id_to_index[s]] for s in sequence])


def fit_and_evaluate(service: str):
    print(f"[{service}]")
    template_ids, sequence = load_sequence(service)
    n_states = len(template_ids)
    print(f"  {len(sequence)} real events, {n_states} real templates -> "
          f"n_states={n_states}")

    symbols = to_symbol_index(sequence, template_ids)
    split_idx = int(len(symbols) * (1 - HELD_OUT_FRACTION))
    train, held_out = symbols[:split_idx], symbols[split_idx:]
    print(f"  chronological split: {len(train)} train, {len(held_out)} held-out "
          f"(last {HELD_OUT_FRACTION * 100:.0f}%, not shuffled)")

    if len(held_out) < 10:
        print(f"  WARNING: held-out set too small ({len(held_out)} events) to "
              f"trust the generalization check -- accumulate more real data "
              f"before relying on this fit.")

    best_model = None
    best_train_score = -np.inf
    for attempt in range(N_HMM_FIT_ATTEMPTS):
        model = hmm.CategoricalHMM(
            n_components=n_states,
            n_iter=200,
            random_state=attempt,
            n_features=n_states,
        )
        model.fit(train)
        score = model.score(train)
        if score > best_train_score:
            best_train_score = score
            best_model = model

    train_mean_ll = best_train_score / len(train)
    held_out_mean_ll = best_model.score(held_out) / len(held_out) if len(held_out) else None

    print(f"  train mean log-likelihood/event:     {train_mean_ll:.4f}")
    if held_out_mean_ll is not None:
        print(f"  held-out mean log-likelihood/event:  {held_out_mean_ll:.4f}")
        gap = train_mean_ll - held_out_mean_ll
        print(f"  gap: {gap:.4f} "
              f"({'looks fine' if abs(gap) < 0.5 else 'WARNING: large gap, possible overfit'})")

    model_path = os.path.join(TRACK_B_DIR, f"{service}_hmm_model.npz")
    np.savez(
        model_path,
        startprob=best_model.startprob_,
        transmat=best_model.transmat_,
        emissionprob=best_model.emissionprob_,
        template_ids=np.array(template_ids),
    )
    print(f"  wrote {model_path}")
    print()


def main():
    for service in DEEPLOG_TIER_2_THIN:
        fit_and_evaluate(service)


if __name__ == "__main__":
    main()
