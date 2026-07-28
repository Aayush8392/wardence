"""
Real EWMA + CUSUM control-chart monitor for Track B's SPC-primary
services (currently just `catalogue` -- see deeplog_service_config.py's
TRACK_B_PRIMARY_DETECTOR, locked 2026-07-29 after confirming via Kimi
review 07 that catalogue's low transition-determinism makes it
genuinely unsuitable for HMM-primary detection: min-per-step scoring
gave a real 25.8% false-positive rate there).

Three real channels per BUCKET_SECONDS time bucket, all already
produced by track_b_hmm_spc_features.py's SPC vectors:
  1. Per-template counts -- the real "mix" signal (e.g. Health spiking
     while List vanishes, per Kimi's original recommendation).
  2. Total event count -- real throughput.
  3. mean_gap_s -- real cadence signal (a memory leak/CPU throttle
     often stretches the gap between events before the mix itself
     changes, per Kimi's review).

Standard, un-tuned SPC control-chart parameters (textbook defaults, not
cherry-picked against this data):
  - EWMA: lambda=0.2, control limit L=3 (asymptotic control limits).
  - CUSUM: k=0.5*sigma (tuned to detect a 1-sigma mean shift, the
    standard convention), H=5*sigma decision interval.

Baseline mean/sigma per channel computed from a REAL chronological
train split (same HELD_OUT_FRACTION as the HMM track, for consistency)
-- monitored on the real held-out portion, never the training data
itself.

Usage:
    python3 spc_ewma_cusum_monitor.py
"""

import json
import os

import numpy as np

from deeplog_service_config import TRACK_B_PRIMARY_DETECTOR

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_B_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_b")

HELD_OUT_FRACTION = 0.2  # matches fit_track_b_hmm.py / hmm_anomaly_scorer.py's convention

EWMA_LAMBDA = 0.2
EWMA_L = 3.0
CUSUM_K_SIGMAS = 0.5
CUSUM_H_SIGMAS = 5.0


def load_spc_rows(service: str):
    path = os.path.join(TRACK_B_DIR, f"{service}_spc_vectors.jsonl")
    rows = []
    with open(path) as f:
        for line in f:
            rows.append(json.loads(line))
    return rows


def extract_channels(rows: list):
    """Real channel extraction: one array per real template's count,
    plus total_events, plus mean_gap_s (with None -- a bucket with 0-1
    events, no real gap to measure -- forward-filled from the last real
    value, a standard, conservative SPC convention for missing data
    rather than dropping the bucket or treating it as zero)."""
    template_ids = sorted(rows[0]["counts"].keys())
    channels = {f"count[{tid}]": np.array([r["counts"][tid] for r in rows], dtype=float)
                for tid in template_ids}
    channels["total_events"] = np.array([r["total_events"] for r in rows], dtype=float)

    gaps = [r["mean_gap_s"] for r in rows]
    last_real = 0.0
    filled_gaps = []
    for g in gaps:
        if g is not None:
            last_real = g
        filled_gaps.append(last_real)
    channels["mean_gap_s"] = np.array(filled_gaps, dtype=float)

    return channels


def ewma_series(x: np.ndarray, baseline_mean: float, baseline_std: float):
    """Real EWMA statistic + asymptotic control limits (standard
    formula: mean +/- L*sigma*sqrt(lambda/(2-lambda)))."""
    z = np.zeros(len(x))
    z_prev = baseline_mean
    for i, xi in enumerate(x):
        z_prev = EWMA_LAMBDA * xi + (1 - EWMA_LAMBDA) * z_prev
        z[i] = z_prev
    limit = EWMA_L * baseline_std * np.sqrt(EWMA_LAMBDA / (2 - EWMA_LAMBDA))
    upper = baseline_mean + limit
    lower = baseline_mean - limit
    flagged = (z > upper) | (z < lower)
    return z, upper, lower, flagged


def cusum_series(x: np.ndarray, baseline_mean: float, baseline_std: float):
    """Real two-sided CUSUM: detects a sustained shift, not a single
    spike -- accumulates deviations beyond an allowance k, resets on
    return to baseline, flags once the accumulated evidence crosses H."""
    if baseline_std == 0:
        return np.zeros(len(x)), np.zeros(len(x)), 0.0, np.zeros(len(x), dtype=bool)
    k = CUSUM_K_SIGMAS * baseline_std
    h = CUSUM_H_SIGMAS * baseline_std
    c_plus = np.zeros(len(x))
    c_minus = np.zeros(len(x))
    for i, xi in enumerate(x):
        prev_plus = c_plus[i - 1] if i > 0 else 0.0
        prev_minus = c_minus[i - 1] if i > 0 else 0.0
        c_plus[i] = max(0.0, prev_plus + (xi - baseline_mean - k))
        c_minus[i] = max(0.0, prev_minus + (baseline_mean - xi - k))
    flagged = (c_plus > h) | (c_minus > h)
    return c_plus, c_minus, h, flagged


def monitor_channel(name: str, train: np.ndarray, held_out: np.ndarray):
    baseline_mean = float(np.mean(train))
    baseline_std = float(np.std(train))

    if baseline_std == 0:
        print(f"    [{name}] baseline is constant (std=0) -- skipping, "
              f"nothing for SPC to monitor here.")
        return None

    ewma_z, ewma_upper, ewma_lower, ewma_flags = ewma_series(held_out, baseline_mean, baseline_std)
    cusum_plus, cusum_minus, cusum_h, cusum_flags = cusum_series(held_out, baseline_mean, baseline_std)

    combined_flags = ewma_flags | cusum_flags
    fp_rate = combined_flags.mean()
    print(f"    [{name}] baseline mean={baseline_mean:.3f} std={baseline_std:.3f} | "
          f"held-out FP rate: {fp_rate * 100:.1f}% "
          f"(EWMA: {ewma_flags.mean() * 100:.1f}%, CUSUM: {cusum_flags.mean() * 100:.1f}%)")

    return {
        "baseline_mean": baseline_mean, "baseline_std": baseline_std,
        "ewma_upper": ewma_upper, "ewma_lower": ewma_lower,
        "cusum_h": cusum_h,
    }


def synthetic_shift_test(service: str, channels: dict, split_idx: int):
    """Real self-test: inject a genuine, sustained mean shift into one
    channel's held-out portion (double the count of the most common
    template for a real stretch, matching Kimi's "Get spiking while
    List vanishes" fault scenario), confirm the monitor actually
    catches it -- not just that it stays quiet on real normal data."""
    print(f"\n  synthetic shift test ({service}):")
    template_count_keys = [k for k in channels if k.startswith("count[")]
    if not template_count_keys:
        print("    (no template count channels found, skipping)")
        return
    target = max(template_count_keys, key=lambda k: channels[k][:split_idx].mean())

    held_out = channels[target][split_idx:].copy()
    train = channels[target][:split_idx]
    baseline_mean = float(np.mean(train))
    baseline_std = float(np.std(train))

    shift_start = len(held_out) // 2
    shifted = held_out.copy()
    shifted[shift_start:] = shifted[shift_start:] * 2 + 5  # real, sustained, not subtle

    _, _, _, ewma_flags_before = ewma_series(held_out[:shift_start], baseline_mean, baseline_std)
    _, _, _, ewma_flags_after = ewma_series(shifted[shift_start:], baseline_mean, baseline_std)
    _, _, _, cusum_flags_before = cusum_series(held_out[:shift_start], baseline_mean, baseline_std)
    _, _, _, cusum_flags_after = cusum_series(shifted[shift_start:], baseline_mean, baseline_std)

    print(f"    channel: {target}")
    print(f"    pre-shift (real, unmodified): EWMA flagged {ewma_flags_before.sum()}, "
          f"CUSUM flagged {cusum_flags_before.sum()} (out of {shift_start})")
    print(f"    post-shift (synthetic, doubled+5): EWMA flagged {ewma_flags_after.sum()}, "
          f"CUSUM flagged {cusum_flags_after.sum()} (out of {len(shifted) - shift_start})")
    caught = ewma_flags_after.any() or cusum_flags_after.any()
    print(f"    -> {'CAUGHT (correct)' if caught else 'NOT caught (unexpected)'}")


def process_service(service: str):
    print(f"[{service}]")
    rows = load_spc_rows(service)
    print(f"  {len(rows)} real time buckets loaded.")
    channels = extract_channels(rows)

    split_idx = int(len(rows) * (1 - HELD_OUT_FRACTION))
    print(f"  chronological split: {split_idx} train, {len(rows) - split_idx} held-out buckets")
    print(f"  real per-channel baseline + held-out check:")
    for name, values in channels.items():
        monitor_channel(name, values[:split_idx], values[split_idx:])

    synthetic_shift_test(service, channels, split_idx)
    print()


def main():
    spc_services = [s for s, primary in TRACK_B_PRIMARY_DETECTOR.items() if primary == "spc"]
    for service in spc_services:
        process_service(service)


if __name__ == "__main__":
    main()
