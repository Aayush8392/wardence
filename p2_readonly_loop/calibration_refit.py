"""
Real empirical calibration layer for self-reported LLM confidence
(Groq/OpenRouter -- the two providers in PROVIDER_CHAIN that never get
real logprobs, confirmed in model_backend.py). Locked design: Kimi
review 11 (reviews/11_calibrated_confidence_design_kimi_review.md,
2026-07-30). Never built until now -- was explicitly parked pending
real ReAct-loop data to calibrate against, which now exists.

WHY THIS EXISTS: raw self-reported confidence from an LLM is known to
be poorly calibrated (models are often confidently wrong). Histogram
binning + Laplace smoothing converts "the model said 0.95" into "when
this model said something in the 0.9-1.0 range in the past, it was
actually right X% of the time" -- a real, empirically-grounded number
instead of trusting the model's own self-assessment at face value.

REAL DATA SOURCES (this project's actual schema, not the review's
illustrative pseudocode, which assumed a single generic "episodes"
table):
  - llm_diagnosis_log: the primary ReAct chain's own diagnoses,
    whichever provider actually served a given episode (usually
    Gemma/Nemotron, but a real fallback can reach Groq/OpenRouter too).
    Carries actual_class directly (matches_ground_truth precomputed).
  - comparison_sampling_log: the newer Groq/OpenRouter comparison-only
    calls (2026-08-04/05) -- deliberately blinded from ground truth at
    write time, so this script joins episodes.fault_class to score it,
    same as every other comparison-sampling diagnostic script.
Only confidence_source='self_reported' rows are used from either table
-- logprob-derived rows (Gemma/Nemotron/Gemini/Kimi) don't need this
layer at all and are excluded on purpose.

STATISTICAL METHOD (locked, Kimi review 11 item 1): equal-width
histogram binning, 10 bins (0.0-0.1, ..., 0.9-1.0, last bin closed on
both ends), Laplace smoothing: calibrated = (correct+k)/(total+2k).
NOT isotonic regression or Platt scaling -- both overfit badly at the
small per-bin sample sizes expected here.

GRANULARITY (item 2): per-(provider,model) primary. Per-(model,class)
bins only unlock once that model has 100+ total self-reported episodes
AND that specific class has >=10 of them -- otherwise falls back to
the per-model bin. Kimi's explicit note (item 7): key by (provider,
model), never model alone -- Groq's gpt-oss-120b and OpenRouter's
gpt-oss-20b:free are different inference stacks even when the
underlying weights are related, and get separate curves.

COLD-START (item 3): <5 total episodes for a (provider,model) --
passthrough raw, tag self_reported_uncalibrated. 5-9 -- strong
smoothing (+2/+4), tag self_reported_calibrated_weak. 10+ -- standard
smoothing (+1/+2), tag self_reported_calibrated. The tag is written
into the output for every (provider,model) -- never hide the cold-start
state (Kimi's explicit instruction).

PIPELINE (item 4): offline batch refit, reads a snapshot, writes
calibration_curves.json. Not a live per-call lookup -- an episode that
gets re-scored later needs a clean refit, not an incremental patch.

NOT done by this script (deliberately, first step only): wiring
calibration_curves.json into the live agent's reporting, or into
publish_to_r2.py for the frontend. Kimi review 11 item 6 explicitly
locks calibration OUT of trust ladder promotion/demotion -- this is a
measurement/reporting layer only, never a gate.

Run with (from WSL, same venv as the other pipeline scripts):
    python3 calibration_refit.py [path/to/wardence.db] [output/calibration_curves.json]
Safe to delete/rerun -- refits fresh from the DB every time, writes no
state back to it.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from model_backend import PROVIDER_CHAIN

# Real chain membership, not a hardcoded exclusion list -- self-corrects
# if the roster ever changes again.
REAL_CHAIN_KEYS = {f"{e['provider']}:{e['model']}" for e in PROVIDER_CHAIN}

# Explicit exclusion, separate from chain membership: groq:llama-3.3-70b-versatile
# IS still a real fallback-tier entry in PROVIDER_CHAIN (rare cascading-
# failure fallback), but its known real weakness (repeated oom/crash-loop
# confusion, confirmed across multiple sessions -- see wardence_buildlog.md)
# means its confidence isn't worth calibrating -- it's not a candidate for
# trustworthy routing regardless of what a calibration curve would say
# about it. User's explicit call, 2026-08-1x.
EXPLICITLY_EXCLUDED_KEYS = {"groq:llama-3.3-70b-versatile"}

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "wardence_p2_data" / "wardence.db")
OUTPUT_PATH = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "calibration_curves.json"

NUM_BINS = 10

# Cold-start policy (Kimi review 11 item 3), keyed by total real episodes
# for that (provider, model), regardless of class.
COLD_START_UNCALIBRATED_MAX = 4      # 0-4: raw passthrough
COLD_START_WEAK_MAX = 9               # 5-9: strong smoothing
# 10+: standard smoothing

# Granularity graduation (Kimi review 11 item 2).
PER_CLASS_MODEL_MIN_TOTAL = 100       # model needs 100+ total self-reported episodes...
PER_CLASS_MODEL_MIN_PAIR = 10         # ...AND >=10 for that specific class...
                                       # ...before a per-(model,class) bin is used for that pair.


def _bin_index(confidence: float) -> int:
    return min(int(confidence * NUM_BINS), NUM_BINS - 1)


def _is_correct(predicted: str, actual: str) -> bool:
    if predicted == actual:
        return True
    return predicted in ("none", "no anomaly detected") and actual in ("none", "no anomaly detected")


def load_self_reported_rows(conn: sqlite3.Connection) -> list[dict]:
    """Real self-reported rows from both real sources, unioned into one
    flat list. `correct` is computed here (not trusted from a
    precomputed column) so both sources are scored identically."""
    rows = []

    for provider, model, actual_class, diagnosis, confidence in conn.execute(
        """
        SELECT provider, model, actual_class, llm_diagnosis, llm_confidence
        FROM llm_diagnosis_log
        WHERE llm_confidence_source = 'self_reported'
          AND llm_confidence IS NOT NULL
          AND llm_diagnosis IS NOT NULL
        """
    ).fetchall():
        rows.append({
            "provider": provider, "model": model, "fault_class": actual_class,
            "confidence": confidence, "correct": _is_correct(diagnosis, actual_class),
        })

    for provider, model, fault_class, diagnosis, confidence in conn.execute(
        """
        SELECT c.provider, c.model, e.fault_class, c.diagnosis, c.confidence
        FROM comparison_sampling_log c
        JOIN episodes e ON e.episode_id = c.episode_id
        WHERE c.confidence_source = 'self_reported'
          AND c.confidence IS NOT NULL
          AND c.diagnosis IS NOT NULL
        """
    ).fetchall():
        rows.append({
            "provider": provider, "model": model, "fault_class": fault_class,
            "confidence": confidence, "correct": _is_correct(diagnosis, fault_class),
        })

    return rows


def _fit_bins(rows: list[dict]) -> list[dict]:
    bins = [{"total": 0, "correct": 0} for _ in range(NUM_BINS)]
    for r in rows:
        idx = _bin_index(r["confidence"])
        bins[idx]["total"] += 1
        bins[idx]["correct"] += 1 if r["correct"] else 0
    return bins


def _laplace(correct: int, total: int, smoothing: tuple[int, int]) -> float:
    k_num, k_den = smoothing
    return (correct + k_num) / (total + k_den)


def get_calibrated_confidence(
    raw_confidence: float, provider: str, model: str, fault_class: str, curves: dict
) -> tuple[float, str]:
    """Real lookup helper, for whenever this gets wired into a live
    reporting path (not done by this script). Keyed by (provider,
    model) per Kimi review 11 item 7 -- same model ID under a
    different provider gets its own curve, never shared."""
    key = f"{provider}:{model}"
    curve = curves.get(key)
    if curve is None:
        return raw_confidence, "self_reported_uncalibrated"

    smoothing = tuple(curve["smoothing"])
    pair = curve.get("per_class_pairs", {}).get(fault_class)
    if pair is not None:
        bin_idx = _bin_index(raw_confidence)
        b = pair["bins"][bin_idx]
        return _laplace(b["correct"], b["total"], smoothing), curve["tag"]

    bin_idx = _bin_index(raw_confidence)
    b = curve["per_model_bins"][bin_idx]
    return _laplace(b["correct"], b["total"], smoothing), curve["tag"]


def main():
    conn = sqlite3.connect(DB_PATH)
    rows = load_self_reported_rows(conn)
    conn.close()

    print(f"Real self-reported rows loaded: {len(rows)} "
          f"(llm_diagnosis_log + comparison_sampling_log, unioned)\n")
    if not rows:
        print("No self-reported rows found -- nothing to fit. Aborting without writing output.")
        sys.exit(1)

    by_model: dict[str, list[dict]] = defaultdict(list)
    dropped_stale = 0
    dropped_excluded = 0
    for r in rows:
        key = f"{r['provider']}:{r['model']}"
        if key not in REAL_CHAIN_KEYS:
            dropped_stale += 1
            continue
        if key in EXPLICITLY_EXCLUDED_KEYS:
            dropped_excluded += 1
            continue
        by_model[key].append(r)
    if dropped_stale:
        print(f"Dropped {dropped_stale} row(s) from (provider,model) pairs not in the real "
              f"PROVIDER_CHAIN (leftover smoke-test data, never live production volume).")
    if dropped_excluded:
        print(f"Dropped {dropped_excluded} row(s) from explicitly excluded pairs "
              f"({sorted(EXPLICITLY_EXCLUDED_KEYS)}).")
    if dropped_stale or dropped_excluded:
        print()

    curves = {}
    fitted_at = datetime.now(timezone.utc).isoformat()

    print(f"{'provider:model':<45} {'n':>5} {'tag':<28} {'raw_acc':>8} {'calib_acc':>10}")
    print("-" * 100)

    for key, group in sorted(by_model.items()):
        total_n = len(group)

        if total_n <= COLD_START_UNCALIBRATED_MAX:
            tag = "self_reported_uncalibrated"
            smoothing = None
        elif total_n <= COLD_START_WEAK_MAX:
            tag = "self_reported_calibrated_weak"
            smoothing = (2, 4)
        else:
            tag = "self_reported_calibrated"
            smoothing = (1, 2)

        per_model_bins = _fit_bins(group)

        per_class_pairs = {}
        if smoothing is not None and total_n >= PER_CLASS_MODEL_MIN_TOTAL:
            by_class = defaultdict(list)
            for r in group:
                by_class[r["fault_class"]].append(r)
            for cls, cls_rows in by_class.items():
                if len(cls_rows) >= PER_CLASS_MODEL_MIN_PAIR:
                    per_class_pairs[cls] = {
                        "total": len(cls_rows),
                        "correct": sum(1 for r in cls_rows if r["correct"]),
                        "bins": _fit_bins(cls_rows),
                    }

        curves[key] = {
            "total_episodes": total_n,
            "tag": tag,
            "smoothing": list(smoothing) if smoothing else None,
            "per_model_bins": per_model_bins,
            "per_class_pairs": per_class_pairs,
            "fitted_at": fitted_at,
        }

        # Real raw-vs-calibrated comparison, printed for immediate
        # sanity checking -- this IS the "gap between raw and
        # calibrated" the eventual Calibration page is meant to show.
        raw_acc = sum(1 for r in group if r["correct"]) / total_n
        if smoothing is not None:
            calibrated_vals = [
                _laplace(per_model_bins[_bin_index(r["confidence"])]["correct"],
                         per_model_bins[_bin_index(r["confidence"])]["total"], smoothing)
                for r in group
            ]
            calib_acc_str = f"{sum(calibrated_vals)/len(calibrated_vals):.3f}"
        else:
            calib_acc_str = "n/a (raw)"

        print(f"{key:<45} {total_n:>5} {tag:<28} {raw_acc:>8.3f} {calib_acc_str:>10}")
        if per_class_pairs:
            print(f"    per-(model,class) bins built for: {sorted(per_class_pairs)}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(curves, indent=2) + "\n")
    print(f"\nWritten: {OUTPUT_PATH}")
    print("\nReminder (Kimi review 11 item 6): this is measurement/reporting only --")
    print("never feed confidence_calibrated into trust-ladder promotion/demotion.")


if __name__ == "__main__":
    main()
