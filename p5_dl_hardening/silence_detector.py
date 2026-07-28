"""
Silence detection -- the cheap classical signal from the locked
architecture ("service stopped emitting logs"). Built 2026-07-28.

Real design point: a single fixed "no logs for Ns = silent" threshold
doesn't work across this project's services, because their real log
rates differ by 3+ orders of magnitude (carts ~30/s vs catalogue-db
~0.006/s, per check_loki_log_volume.py's real measured numbers). A fixed
threshold would either false-positive constantly on quiet services or
never catch a real silence on a chatty one.

So each service gets its own threshold, derived from its own real
recent inter-log-line gap distribution, not guessed or hardcoded --
same "measure real numbers before building" discipline as the rest of
this project. SAFETY_MULTIPLE and MIN_THRESHOLD_S are the only tunables,
not per-service magic numbers.

Run from WSL2, with a Loki port-forward active:
    kubectl port-forward -n monitoring svc/loki 3100:3100

Usage:
    python3 silence_detector.py
"""

import json
import time
import urllib.parse
import urllib.request

LOKI_URL = "http://localhost:3100"
NAMESPACE = "sock-shop"

SERVICES = [
    "front-end",
    "orders",
    "carts",
    "catalogue",
    "catalogue-db",
    "payment",
    "shipping",
    "user",
    "session-db",
    "queue-master",
]

# How far back to sample for establishing each service's real normal gap
# pattern. Bounded and cheap -- same discipline as check_loki_log_volume.py's
# SAMPLE_MAX_LINES/SAMPLE_LOOKBACK_MINUTES, never a full-history pull.
BASELINE_SAMPLE_MAX_LINES = 500
BASELINE_LOOKBACK_MINUTES = 30

# A real current silence must exceed the largest gap actually observed in
# the baseline sample by this multiple before being flagged -- generous on
# purpose, since normal jitter (traffic_gen bursts, GC pauses, etc.) can
# already produce real gaps larger than the "typical" one.
SAFETY_MULTIPLE = 5
# Floor so a service with an extremely tight, low-jitter baseline (e.g.
# one real burst every ~2s) doesn't get a silence threshold of a few
# seconds, which real jitter alone could trip.
MIN_THRESHOLD_S = 60.0
# Ceiling so a genuinely very low-volume service (catalogue-db,
# session-db) doesn't get a threshold so large a real silence would never
# be caught within any practical episode/durability window.
MAX_THRESHOLD_S = 1800.0


def fetch_sample_timestamps(logql: str, end_ns: int, max_lines: int, lookback_minutes: int):
    start_ns = end_ns - int(lookback_minutes * 60 * 1e9)
    params = {
        "query": logql,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(max_lines),
        "direction": "backward",
    }
    url = f"{LOKI_URL}/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        result = json.loads(resp.read())
    streams = result.get("data", {}).get("result", [])
    timestamps = []
    for stream in streams:
        for ts_str, _ in stream.get("values", []):
            timestamps.append(int(ts_str))
    timestamps.sort()
    return timestamps


def compute_threshold(timestamps_ns: list) -> tuple:
    """Returns (threshold_s, max_observed_gap_s, sample_size). Real gaps
    between consecutive real log lines in the baseline sample -- the max
    observed gap, scaled by SAFETY_MULTIPLE, clamped to [MIN, MAX]."""
    if len(timestamps_ns) < 2:
        # Not enough real data to derive a threshold -- fall back to the
        # floor rather than guessing, and flag it as such to the caller.
        return MIN_THRESHOLD_S, None, len(timestamps_ns)

    gaps_s = [
        (timestamps_ns[i] - timestamps_ns[i - 1]) / 1e9
        for i in range(1, len(timestamps_ns))
    ]
    max_gap_s = max(gaps_s)
    threshold = max(MIN_THRESHOLD_S, min(MAX_THRESHOLD_S, max_gap_s * SAFETY_MULTIPLE))
    return threshold, max_gap_s, len(timestamps_ns)


def main():
    end_ns = int(time.time() * 1e9)

    print(f"Silence check at {LOKI_URL}, namespace={NAMESPACE!r}")
    print(f"Baseline: last {BASELINE_LOOKBACK_MINUTES}min, up to {BASELINE_SAMPLE_MAX_LINES} lines/service.\n")
    print(f"{'service':<16} {'baseline_n':>10} {'max_gap_s':>10} {'threshold_s':>12} "
          f"{'current_gap_s':>14} {'status':>8}")
    print("-" * 76)

    for svc in SERVICES:
        logql = f'{{namespace="{NAMESPACE}", app="{svc}"}}'
        try:
            timestamps = fetch_sample_timestamps(
                logql, end_ns, BASELINE_SAMPLE_MAX_LINES, BASELINE_LOOKBACK_MINUTES
            )
        except Exception as e:
            print(f"{svc:<16} ERROR: {e}")
            continue

        threshold_s, max_gap_s, n = compute_threshold(timestamps)

        if not timestamps:
            print(f"{svc:<16} {n:>10} {'-':>10} {threshold_s:>12.1f} {'no data':>14} {'UNKNOWN':>8}")
            continue

        current_gap_s = (end_ns - timestamps[-1]) / 1e9
        status = "SILENT" if current_gap_s > threshold_s else "ok"
        max_gap_str = f"{max_gap_s:.1f}" if max_gap_s is not None else "-"

        print(f"{svc:<16} {n:>10} {max_gap_str:>10} {threshold_s:>12.1f} "
              f"{current_gap_s:>14.1f} {status:>8}")


if __name__ == "__main__":
    main()
