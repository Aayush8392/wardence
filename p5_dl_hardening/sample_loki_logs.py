"""
Follow-up to check_loki_log_volume.py: pulls a handful of RAW log lines
per service, sampled from a few different points across the 48h window,
so we can actually look at what's in there rather than guess from counts.

Run from WSL2, with the same Loki port-forward active:
    kubectl port-forward -n monitoring svc/loki 3100:3100

Usage:
    python3 sample_loki_logs.py
"""

import json
import time
import urllib.parse
import urllib.request

LOKI_URL = "http://localhost:3100"
NAMESPACE = "sock-shop"

# carts: investigating the anomalous 230 lines/sec volume.
# front-end / user: investigating the template-explosion (masking) issue.
SERVICES = ["front-end"]

SAMPLES_PER_POINT = 15
WINDOW_HOURS = 48


def loki_query_range(logql: str, start_ns: int, end_ns: int, limit: int):
    params = {
        "query": logql,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "forward",
    }
    url = f"{LOKI_URL}/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def sample_at(logql: str, point_start_ns: int, point_end_ns: int, n: int):
    """Grabs up to n lines from a narrow slice of the window."""
    result = loki_query_range(logql, point_start_ns, point_end_ns, limit=n)
    streams = result.get("data", {}).get("result", [])
    lines = []
    for stream in streams:
        for ts_str, line in stream.get("values", []):
            lines.append((int(ts_str), line))
    lines.sort(key=lambda x: x[0])
    return lines[:n]


def main():
    end_ns = int(time.time() * 1e9)
    start_ns = end_ns - int(WINDOW_HOURS * 3600 * 1e9)
    span_ns = end_ns - start_ns

    # Three points: start of window, middle, most recent.
    points = [
        ("window start (oldest)", start_ns, start_ns + int(5 * 60 * 1e9)),
        ("window middle", start_ns + span_ns // 2, start_ns + span_ns // 2 + int(5 * 60 * 1e9)),
        ("most recent", end_ns - int(5 * 60 * 1e9), end_ns),
    ]

    for svc in SERVICES:
        print("=" * 80)
        print(f"SERVICE: {svc}")
        print("=" * 80)
        logql = f'{{namespace="{NAMESPACE}", app="{svc}"}}'
        for label, p_start, p_end in points:
            print(f"\n--- {label} ---")
            try:
                lines = sample_at(logql, p_start, p_end, SAMPLES_PER_POINT)
            except Exception as e:
                print(f"  ERROR: {e}")
                continue
            if not lines:
                print("  (no lines in this 5-minute slice)")
                continue
            for ts_ns, line in lines:
                ts_s = ts_ns / 1e9
                readable = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_s))
                # Truncate very long lines for readability.
                shown = line if len(line) <= 300 else line[:300] + "...(truncated)"
                print(f"  [{readable}] {shown}")
        print()


if __name__ == "__main__":
    main()
