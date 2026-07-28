"""
Real-numbers check before building DeepLog: how much normal log data does
Loki actually have right now, per Sock Shop service, and roughly how many
distinct log templates does that traffic produce.

Run from WSL2, with a Loki port-forward active in another terminal:
    kubectl port-forward -n monitoring svc/loki 3100:3100

Usage:
    python3 check_loki_log_volume.py

Optional: pip install drain3   (adds a rough distinct-template count per
service; script runs fine without it, just skips that part).

Rewritten 2026-07-28, twice, after real crashes -- both fixes needed, not
just one. First finding: the original version (paginating through EVERY
raw log line, including carts' 5.4M+) crashed Loki (OOMKilled, restart
count climbing, readiness-probe failures) -- 400Mi was too tight for the
in-memory result set. First fix attempt (switching to a single
count_over_time aggregate query) was WRONG on its own: it crashed Loki
again just as fast, proving the real cost is server-side chunk
scanning/decompression across the full 48h range, independent of query
shape -- an aggregate still has to read all the same underlying chunk
data, it just doesn't return raw lines to the client. Real fix needed
BOTH: (1) Loki's memory limit raised 400Mi -> 1Gi in loki-values.yaml
(real headroom checked first, not guessed -- see that file's own
comment), and (2) bounding how much history any ONE request scans, via
CHUNK_HOURS-sized chunks summed client-side instead of one 48h-wide
query. Template mining separately uses a small, capped raw-line sample
(SAMPLE_MAX_LINES, most recent SAMPLE_LOOKBACK_MINUTES only) -- it only
needs real line content for vocabulary shape, never the full history.
"""

import json
import time
import urllib.parse
import urllib.request

LOKI_URL = "http://localhost:3100"
NAMESPACE = "sock-shop"

# Real Sock Shop service names (app label values), not guessed --
# matches the target names already used throughout this project's
# injector.py/agent.py.
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

WINDOW_HOURS = 48
# Query the window in chunks, not one giant request -- confirmed 2026-07-28
# that even a single-bucket count_over_time over the full 48h crashed Loki
# (OOMKilled) just as badly as the original raw-line pagination did. The
# real cost is server-side chunk scanning, independent of query shape, so
# the fix is bounding how much history any ONE request has to scan.
CHUNK_HOURS = 2
CHUNK_SLEEP_S = 2  # brief pause between chunk queries, don't hammer Loki back-to-back
CHUNK_TIMEOUT_S = 60  # generous -- carts' real dense periods need real time to scan
SERVICE_SLEEP_S = 1  # brief pause between services, same reasoning
# Hard cap on how many raw lines are ever pulled for template mining --
# deliberately small. This is a SAMPLE for vocabulary shape, not a full
# census; the count/rate numbers above already come from the lightweight
# aggregate query, not this sample.
SAMPLE_MAX_LINES = 3000
SAMPLE_LOOKBACK_MINUTES = 30

try:
    from drain3 import TemplateMiner
    from drain3.masking import MaskingInstruction
    from drain3.template_miner_config import TemplateMinerConfig

    # Real masking rules, derived from actually sampling this project's logs
    # (sample_loki_logs.py, 2026-07-28) -- not drain3's generic defaults.
    # Without these, drain3 has NO masking active at all (config.load() was
    # never called), so any line with a varying timestamp/duration/number
    # gets treated as a brand-new template every time -- this is what
    # exploded `user`'s template count to ~lines (every Health-check line,
    # e.g. "took=255.771us", was unique).
    MASKING_INSTRUCTIONS = [
        # ISO8601 timestamps with fractional seconds, e.g.
        # "2026-07-27T20:46:05.350321676Z" (user's `ts=` field).
        MaskingInstruction(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", "TIMESTAMP"
        ),
        # UUIDs -- catalogue item IDs embedded in front-end's URLs/JSON,
        # e.g. "808a2de1-1aaa-4c25-a9b9-6612e8f29a38".
        MaskingInstruction(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "UUID",
        ),
        # Mongo ObjectIds -- 24-char hex, e.g. cart IDs in front-end's
        # "POST to carts: http://carts/carts/<objectid>/items" lines.
        MaskingInstruction(r"\b[0-9a-f]{24}\b", "OBJID"),
        # Duration values with a unit suffix directly attached, e.g.
        # "255.771us"/"300.718ms" (user's `took=` field) -- these don't
        # tokenize as pure numbers, so drain3's own numeric detection
        # never catches them without an explicit rule.
        MaskingInstruction(r"\d+(\.\d+)?(µs|us|ms|ns|s)\b", "DURATION"),
        # Same, but with a space before the unit, e.g. front-end's access
        # log "3.030 ms" / "15.662 ms" -- a different literal format from
        # the attached-unit case above, needs its own rule.
        MaskingInstruction(r"\d+\.\d+ (µs|us|ms|ns|s)\b", "DURATION"),
        # Generic decimal numbers not already caught above (e.g. prices
        # like "17.32" inside embedded JSON payloads).
        MaskingInstruction(r"\b\d+\.\d+\b", "NUM"),
        # Generic bare integers (status/result codes, counts, IDs) --
        # catches everything else that would otherwise fragment templates
        # across other services too, not just user/front-end.
        MaskingInstruction(r"\b\d+\b", "NUM"),
    ]

    HAVE_DRAIN3 = True
except ImportError:
    HAVE_DRAIN3 = False


def _count_over_time_chunk(logql: str, chunk_end_ns: int, chunk_hours: float) -> int:
    """One bounded count_over_time call, scoped to a single chunk -- never
    asks Loki to scan more than `chunk_hours` of history in one request.

    Uses the INSTANT query endpoint (/loki/api/v1/query), not query_range --
    real bug found and fixed 2026-07-28: query_range evaluates a metric
    query at start, start+step, ..., never necessarily at `end`. With
    step == the full chunk width, the single evaluation point landed at
    `start`, so count_over_time(...[chunk_hours]) looked BACKWARD from
    `start` -- covering [start-chunk_hours, start], not the intended
    [start, start+chunk_hours]. Every chunk was silently querying one
    chunk-width too early (badly undercounting: front-end came back as 9
    lines instead of the real 500k+ measured by the original raw-line
    version). An instant query at time=chunk_end evaluates
    count_over_time looking backward from chunk_end, which is exactly the
    intended [chunk_end-chunk_hours, chunk_end] window."""
    # Whole seconds, not "{chunk_hours}h" -- LogQL/Prometheus durations
    # must be integers (e.g. "7200s" is valid, "2.0h" is NOT), and
    # chunk_hours can be fractional for a partial final chunk.
    duration_s = int(round(chunk_hours * 3600))
    metric_query = f"count_over_time({logql}[{duration_s}s])"
    params = {"query": metric_query, "time": str(chunk_end_ns)}
    url = f"{LOKI_URL}/loki/api/v1/query?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=CHUNK_TIMEOUT_S) as resp:
        result = json.loads(resp.read())
    series = result.get("data", {}).get("result", [])
    if not series:
        return 0
    # Instant query returns a single {"value": [ts, "n"]} per series, not
    # a "values" list -- different response shape from query_range.
    return int(sum(float(s["value"][1]) for s in series))


def fetch_line_count(logql: str, start_ns: int, end_ns: int) -> int:
    """
    Real total line count, built from several bounded chunk queries summed
    client-side, never one request spanning the full window -- see
    CHUNK_HOURS comment for why. A brief sleep between chunks keeps this
    from hammering Loki back-to-back the way the original full-window
    version did.
    """
    total = 0
    chunk_ns = int(CHUNK_HOURS * 3600 * 1e9)
    cursor = start_ns
    first = True
    while cursor < end_ns:
        chunk_end = min(cursor + chunk_ns, end_ns)
        # Real width of THIS chunk in hours -- the last chunk is usually
        # shorter than CHUNK_HOURS (window doesn't divide evenly), and the
        # range vector duration must match the real gap being queried or
        # it'll either miss data or double-count into the previous chunk.
        chunk_hours = (chunk_end - cursor) / 3600e9
        if not first:
            time.sleep(CHUNK_SLEEP_S)
        first = False
        total += _count_over_time_chunk(logql, chunk_end, chunk_hours)
        cursor = chunk_end
    return total


def fetch_sample_lines(logql: str, end_ns: int, max_lines: int, lookback_minutes: int):
    """
    Bounded sample of the most recent raw lines -- for template mining
    only, never for counting. Single call, capped, backward direction
    (most recent first) so it's cheap regardless of how much total
    history exists.
    """
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
    lines = []
    for stream in streams:
        for _, line in stream.get("values", []):
            lines.append(line)
    return lines


def main():
    end_ns = int(time.time() * 1e9)
    # Ask Loki for the full configured retention window (48h per
    # loki-values.yaml) -- if traffic_gen has run longer than that,
    # anything older is already gone regardless.
    start_ns = end_ns - int(WINDOW_HOURS * 3600 * 1e9)

    print(f"Querying Loki at {LOKI_URL} for namespace={NAMESPACE!r}")
    print(f"Window: last {WINDOW_HOURS}h (retention_period cap), ending now.")
    print(f"Counts via count_over_time (aggregate, no raw-line pull). "
          f"Templates from a {SAMPLE_MAX_LINES}-line sample "
          f"(last {SAMPLE_LOOKBACK_MINUTES} min), not the full history.\n")
    print(f"{'service':<16} {'lines':>10} {'lines/sec':>10} {'templates':>10}")
    print("-" * 52)

    total_lines = 0
    for i, svc in enumerate(SERVICES):
        if i > 0:
            time.sleep(SERVICE_SLEEP_S)
        logql = f'{{namespace="{NAMESPACE}", app="{svc}"}}'
        try:
            n = fetch_line_count(logql, start_ns, end_ns)
        except Exception as e:
            print(f"{svc:<16} ERROR (count): {e}")
            continue

        total_lines += n
        rate = n / (WINDOW_HOURS * 3600)

        template_count = "-"
        if HAVE_DRAIN3:
            try:
                sample = fetch_sample_lines(
                    logql, end_ns, SAMPLE_MAX_LINES, SAMPLE_LOOKBACK_MINUTES
                )
            except Exception as e:
                print(f"{svc:<16} ERROR (sample): {e}")
                sample = []
            if sample:
                config = TemplateMinerConfig()  # defaults, no extra ini file
                config.masking_instructions = MASKING_INSTRUCTIONS
                miner = TemplateMiner(config=config)
                for line in sample:
                    miner.add_log_message(line)
                template_count = str(len(miner.drain.clusters))

        print(f"{svc:<16} {n:>10} {rate:>10.3f} {template_count:>10}")

    print("-" * 52)
    print(f"Total lines across all services (last {WINDOW_HOURS}h): {total_lines}")
    if not HAVE_DRAIN3:
        print("\n(drain3 not installed -- template-count column skipped. "
              "pip install drain3 to get it.)")


if __name__ == "__main__":
    main()
