"""
Real anomaly-scoring function for Track B's fitted HMMs (catalogue,
queue-master) -- see fit_track_b_hmm.py for the fit itself.

Design: score a SLIDING WINDOW of recent events as mean log-likelihood
per event under the fitted model, not single events -- with only 2-3
real symbols, a single observation's likelihood is too noisy to be a
useful signal on its own (per Kimi's review: rate/mix over a window is
the real signal, not a per-token surprise the way DeepLog's larger
vocabulary supports). The anomaly threshold is derived from the REAL
held-out data's own windowed-score distribution (mean - K*std), not an
arbitrary guessed cutoff -- reuses the exact same chronological
train/held-out split as fit_track_b_hmm.py so the threshold is
calibrated against genuinely unseen-by-training data.

A template_id that never appeared in training is an automatic anomaly
flag on its own -- the HMM has no way to score a symbol it has no
emission probability for, and a brand-new template appearing in a
service the fault classes target is itself real signal, not noise.

Usage:
    python3 hmm_anomaly_scorer.py
"""

import json
import os

import numpy as np
from hmmlearn import hmm

from deeplog_service_config import DEEPLOG_TIER_2_THIN

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_B_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_b")

HELD_OUT_FRACTION = 0.2  # must match fit_track_b_hmm.py's split, for a fair threshold calibration
WINDOW_SIZE = 20         # events per scored window
WINDOW_STRIDE = 5        # step between windows when scanning a sequence
ANOMALY_PERCENTILE = 1   # real, percentile-based threshold on the held-out min-score
                         # distribution -- NOT mean-3*std. Confirmed necessary,
                         # 2026-07-29: the per-window MINIMUM log-prob is a heavy-
                         # tailed/extreme-value statistic, not Gaussian (queue-master's
                         # real held-out std=13.96 was nearly as large as its mean=-14.06,
                         # a few genuinely-rare-but-legitimate single steps in normal
                         # traffic blew the std out and made mean-3*std uselessly loose,
                         # looser than even a deliberately corrupted sequence). A low
                         # percentile of the real distribution is distribution-agnostic
                         # and doesn't inherit that assumption.


def load_model(service: str):
    path = os.path.join(TRACK_B_DIR, f"{service}_hmm_model.npz")
    data = np.load(path)
    template_ids = list(data["template_ids"])
    n_states = len(template_ids)

    model = hmm.CategoricalHMM(n_components=n_states, n_features=n_states)
    model.startprob_ = data["startprob"]
    model.transmat_ = data["transmat"]
    model.emissionprob_ = data["emissionprob"]

    id_to_index = {tid: i for i, tid in enumerate(template_ids)}
    return model, id_to_index


def load_sequence(service: str):
    path = os.path.join(TRACK_B_DIR, f"{service}_hmm_sequence.json")
    with open(path) as f:
        data = json.load(f)
    return data["sequence"]


def to_symbols(sequence: list, id_to_index: dict):
    """Returns (symbol_array, saw_unseen_template). Any template_id not
    in the trained vocabulary is a real, automatic anomaly signal --
    represented here as None so the caller can short-circuit scoring
    for a window containing one."""
    symbols = []
    saw_unseen = False
    for tid in sequence:
        if tid in id_to_index:
            symbols.append(id_to_index[tid])
        else:
            symbols.append(None)
            saw_unseen = True
    return symbols, saw_unseen


def per_step_log_probs(model, symbols: list):
    """Real per-step conditional log-probabilities within a window, via
    the chain rule: log P(x_1..x_n) = sum_i log P(x_i | x_1..x_{i-1}),
    so score(prefix of length i) - score(prefix of length i-1) equals
    the exact log P(x_i | x_1..x_{i-1}) the forward algorithm computed
    -- not an approximation, the real per-step decomposition of the
    same joint likelihood model.score() already returns for the whole
    sequence."""
    prefix_score = model.score(np.array(symbols[:1]).reshape(-1, 1))
    steps = [prefix_score]
    for i in range(1, len(symbols)):
        full_score = model.score(np.array(symbols[:i + 1]).reshape(-1, 1))
        steps.append(full_score - prefix_score)
        prefix_score = full_score
    return steps


def windowed_scores(model, symbols: list, window_size: int, stride: int):
    """Yields the MINIMUM per-step log-probability within each window --
    not the mean. Real fix, 2026-07-29: mean-over-window averages out
    any single rare transition against the rest of a mostly-normal
    window, which made this insensitive for low-state-count services
    like `catalogue` (3 states -- even the model's own worst-case
    sequence only pulled the mean down ~1.5 std, short of the 3-std
    threshold). Taking the minimum surfaces the single worst step
    directly, which is the more natural fit for "did something rare
    just happen" rather than "has the overall pattern drifted." A
    window containing an unseen symbol (None) can't be scored at all --
    yielded as -inf, an unambiguous, maximally-anomalous score rather
    than silently skipped."""
    for start in range(0, len(symbols) - window_size + 1, stride):
        window = symbols[start:start + window_size]
        if None in window:
            yield -np.inf, True  # (score, contains_unseen_template)
            continue
        steps = per_step_log_probs(model, window)
        yield min(steps), False


def calibrate_threshold(model, id_to_index, service: str):
    """Real threshold, derived from the held-out split's own windowed
    scores -- the same chronological split fit_track_b_hmm.py used, so
    this is calibrated against real data the model never trained on,
    not the training set itself (which would underestimate variance).

    Deliberately uses NON-OVERLAPPING windows (stride == window_size)
    for calibration, not WINDOW_STRIDE -- real fix, 2026-07-29: scanning
    with a small stride (5 vs window_size 20, 75% overlap) produces
    heavily autocorrelated, near-duplicate windows, not independent
    samples. A percentile computed over that many correlated repeats
    isn't a valid empirical percentile -- confirmed directly: it
    produced 28-50% held-out false-positive rates instead of the
    intended ~1%. Non-overlapping windows give genuinely independent
    samples for calibration; WINDOW_STRIDE's smaller stride is still
    fine (even desirable, for responsiveness) once the threshold
    itself is properly calibrated against real independent data."""
    sequence = load_sequence(service)
    split_idx = int(len(sequence) * (1 - HELD_OUT_FRACTION))
    held_out = sequence[split_idx:]
    symbols, _ = to_symbols(held_out, id_to_index)

    scores = [ll for ll, unseen in windowed_scores(model, symbols, WINDOW_SIZE, WINDOW_SIZE)
              if not unseen]
    if len(scores) < 5:
        raise RuntimeError(f"{service}: only {len(scores)} real held-out windows -- "
                            f"not enough to calibrate a threshold, accumulate more data first.")

    mean_score = float(np.mean(scores))
    std_score = float(np.std(scores))
    threshold = float(np.percentile(scores, ANOMALY_PERCENTILE))
    return threshold, mean_score, std_score, scores


def self_test(service: str):
    print(f"[{service}]")
    model, id_to_index = load_model(service)
    threshold, mean_score, std_score, held_out_scores = calibrate_threshold(model, id_to_index, service)
    print(f"  real held-out MIN-per-step-log-prob baseline: mean={mean_score:.4f}, std={std_score:.4f} "
          f"(shown for reference -- NOT used as the threshold, see ANOMALY_PERCENTILE)")
    print(f"  real anomaly threshold ({ANOMALY_PERCENTILE}th percentile of real held-out scores): {threshold:.4f}")

    # Check 1: false-positive rate on the SAME real held-out data the
    # threshold was calibrated from -- should be low (this is real,
    # normal traffic; a high flag rate here means the threshold itself
    # is miscalibrated, not that the data is anomalous). Uses <=, not <
    # -- real fix, 2026-07-29: with only 2-3 discrete states, many
    # windows share the exact same min-log-prob (identical rare
    # subsequence -> identical score), so the low tail is full of exact
    # ties. A strict < silently excludes the tied cluster sitting right
    # at the percentile boundary itself, undercounting real flags.
    false_positives = sum(1 for s in held_out_scores if s <= threshold)
    fp_rate = false_positives / len(held_out_scores)
    print(f"  real held-out self-check: {false_positives}/{len(held_out_scores)} windows "
          f"flagged ({fp_rate * 100:.1f}% false-positive rate on known-normal data)")

    # Check 2: a deliberately corrupted synthetic sequence -- built via
    # a real greedy search directly against model.score() itself, not
    # an indirect proxy. Two earlier attempts were both wrong in
    # instructive ways: (1) "repeat the rare symbol" assumed rarity
    # implies improbability, which failed for `catalogue` (its rare
    # template is a health-check that genuinely bursts in real data);
    # (2) walking model.transmat_'s argmin assumed hidden-state index
    # == observed-symbol index, which isn't guaranteed after EM --
    # transmat_ operates on HIDDEN states, while model.score() computes
    # likelihood via the forward algorithm marginalizing over them, so
    # that walk wasn't actually optimizing against the real scoring
    # function at all. Real fix: at each step, try every possible next
    # observed symbol, keep whichever the model's OWN score() call
    # rates lowest so far -- directly grounded in the exact function
    # the anomaly threshold itself is built from.
    n_states = model.transmat_.shape[0]
    corrupted = [0]
    for _ in range(WINDOW_SIZE - 1):
        best_next, worst_score = None, np.inf
        for candidate in range(n_states):
            trial = np.array(corrupted + [candidate]).reshape(-1, 1)
            s = model.score(trial)
            if s < worst_score:
                worst_score, best_next = s, candidate
        corrupted.append(best_next)
    corrupted_scores = list(windowed_scores(model, corrupted, WINDOW_SIZE, WINDOW_SIZE))
    corrupted_ll, corrupted_unseen = corrupted_scores[0]
    corrupted_flagged = corrupted_unseen or corrupted_ll <= threshold
    print(f"  synthetic corrupted-sequence check (greedy search against the "
          f"model's real score() function): score={corrupted_ll:.4f} -> "
          f"{'FLAGGED (correct)' if corrupted_flagged else 'NOT flagged (unexpected)'}")

    # Check 3: an unseen template_id -- must always be flagged, by
    # construction, regardless of threshold value.
    template_ids = list(id_to_index.keys())
    fake_new_template_id = max(template_ids) + 999
    unseen_seq = [fake_new_template_id] * WINDOW_SIZE
    unseen_symbols, saw_unseen = to_symbols(unseen_seq, id_to_index)
    assert saw_unseen, "unseen-template detection itself is broken"
    print(f"  unseen-template check: correctly detected as unseen -> always flagged")
    print()


def main():
    for service in DEEPLOG_TIER_2_THIN:
        self_test(service)


if __name__ == "__main__":
    main()
