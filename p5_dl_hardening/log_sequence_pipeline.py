"""
Shared, incremental log-sequence pipeline for both the DeepLog track
(front-end/orders/user) and the HMM/rate-based-SPC track (catalogue/
queue-master) -- see reviews/07_deeplog_coverage_kimi_review.md and
wardence_context.md's "DeepLog real per-service scope LOCKED" section
for the full real-measurement background this is built on.

Three shared stages, run every time this script is invoked:
  1. Pull new raw log lines from Loki per service, since each service's
     own saved watermark (not from scratch every run -- this is meant
     to be re-run repeatedly as traffic_gen keeps producing normal
     traffic, growing the training set for free over time).
  2. Mine templates via drain3, using each service's locked depth
     (deeplog_service_config.py), with persisted miner state so a
     template's numeric ID means the same thing across every run --
     required for both a Track A window-sequence and a Track B
     frequency-vector to stay valid over time.
  3. Append the real (timestamp, template_id) events to each service's
     canonical JSONL stream file. Everything downstream (Track A's
     windowing, Track B's HMM/SPC feature extraction) reads from these
     files -- neither ever touches Loki directly again.

Resumable by design, matching this project's established pattern
(run_batch_plan.py): a watermark + drain3 snapshot are both persisted
to disk after every successful service, so a crash or Ctrl+C mid-run
only loses the currently-in-progress service, never corrupts already-
committed ones, and the next run picks up cleanly.

Run from WSL2, with a Loki port-forward active in another terminal:
    kubectl port-forward -n monitoring svc/loki 3100:3100

Usage:
    python3 log_sequence_pipeline.py                 # incremental run
    python3 log_sequence_pipeline.py --backfill-hours 4   # first-run seed
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig

from deeplog_service_config import PIPELINE_SERVICES, MASKING_INSTRUCTIONS, get_depth

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
NAMESPACE = "sock-shop"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(SCRIPT_DIR, "pipeline_state")
SNAPSHOT_DIR = os.path.join(STATE_DIR, "drain3_snapshots")
STREAM_DIR = os.path.join(STATE_DIR, "streams")
WATERMARK_FILE = os.path.join(STATE_DIR, "watermarks.json")

DEFAULT_BACKFILL_HOURS = 4  # first-run-only seed window, per service with no watermark yet

# Bounded, short time-slices per Loki request -- same "never ask Loki to
# scan a huge range in one call" discipline as check_loki_log_volume.py,
# which crashed Loki (OOMKilled) before that fix.
CHUNK_MINUTES = 15
MAX_LINES_PER_CHUNK = 5000
CHUNK_SLEEP_S = 1
# If a chunk returns exactly the cap, it's truncated (Loki silently drops
# anything past `limit`, does not error) -- split it and retry rather than
# accept a gap. Floor stops infinite recursion on a genuinely dense burst.
MIN_SPLIT_SECONDS = 5


def _ensure_dirs():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    os.makedirs(STREAM_DIR, exist_ok=True)


def _load_watermarks() -> dict:
    if not os.path.exists(WATERMARK_FILE):
        return {}
    with open(WATERMARK_FILE) as f:
        return json.load(f)


def _save_watermark(service: str, ts_ns: int):
    """Read-modify-write, matching run_batch_plan.py's precedent -- always
    re-read the real current file rather than trusting an in-memory copy,
    so a concurrent/partial run never clobbers another service's entry."""
    watermarks = _load_watermarks()
    watermarks[service] = ts_ns
    tmp_path = WATERMARK_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(watermarks, f, indent=2)
    os.replace(tmp_path, WATERMARK_FILE)  # atomic on both POSIX and Windows/WSL


def _fetch_chunk(logql: str, start_ns: int, end_ns: int, max_lines: int):
    params = {
        "query": logql,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(max_lines),
        "direction": "forward",  # oldest-first within the chunk, matches watermark semantics
    }
    url = f"{LOKI_URL}/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        result = json.loads(resp.read())
    streams = result.get("data", {}).get("result", [])
    lines = []
    for stream in streams:
        for ts_str, line in stream.get("values", []):
            lines.append((int(ts_str), line))
    lines.sort(key=lambda x: x[0])
    return lines


def _fetch_range_no_gaps(logql: str, service: str, start_ns: int, end_ns: int, depth=0):
    """Real fix for silent truncation: Loki's query_range `limit` just
    drops anything past the cap, it does not error or paginate. If a
    request comes back AT the cap, that's a signal (not proof, but the
    safe assumption) that real lines were dropped -- split the range in
    half and retry both halves recursively until every leaf request
    returns under the cap, or the range is too small to split further
    (MIN_SPLIT_SECONDS floor, to bound recursion on a genuinely dense
    burst rather than looping forever)."""
    lines = _fetch_chunk(logql, start_ns, end_ns, MAX_LINES_PER_CHUNK)
    span_s = (end_ns - start_ns) / 1e9
    if len(lines) < MAX_LINES_PER_CHUNK or span_s <= MIN_SPLIT_SECONDS:
        if len(lines) >= MAX_LINES_PER_CHUNK:
            print(f"    WARNING: {service} range still at the cap after "
                  f"splitting down to {span_s:.1f}s -- genuinely denser than "
                  f"{MAX_LINES_PER_CHUNK} lines/{MIN_SPLIT_SECONDS}s, accepting "
                  f"possible truncation here (floor reached).")
        return lines
    mid_ns = start_ns + (end_ns - start_ns) // 2
    left = _fetch_range_no_gaps(logql, service, start_ns, mid_ns, depth + 1)
    time.sleep(CHUNK_SLEEP_S)
    right = _fetch_range_no_gaps(logql, service, mid_ns, end_ns, depth + 1)
    return left + right


def fetch_new_lines(service: str, since_ns: int, until_ns: int):
    """Pulls every new line in (since_ns, until_ns], in bounded chunks,
    each recursively split if it comes back truncated. Yields (ts_ns,
    line) tuples in strictly increasing time order."""
    logql = f'{{namespace="{NAMESPACE}", app="{service}"}}'
    chunk_ns = CHUNK_MINUTES * 60 * int(1e9)
    cursor = since_ns
    first = True
    total_lines = 0
    while cursor < until_ns:
        chunk_end = min(cursor + chunk_ns, until_ns)
        if not first:
            time.sleep(CHUNK_SLEEP_S)
        first = False
        lines = _fetch_range_no_gaps(logql, service, cursor, chunk_end)
        lines.sort(key=lambda x: x[0])
        for ts_ns, line in lines:
            total_lines += 1
            yield ts_ns, line
        cursor = chunk_end
    if total_lines:
        print(f"    {total_lines} new lines pulled for {service}.")


def build_miner(service: str) -> TemplateMiner:
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"{service}.bin")
    persistence = FilePersistence(snapshot_path)
    config = TemplateMinerConfig()
    config.masking_instructions = MASKING_INSTRUCTIONS
    config.drain_depth = get_depth(service)
    return TemplateMiner(persistence_handler=persistence, config=config)


def process_service(service: str, backfill_hours: float, now_ns: int):
    watermarks = _load_watermarks()
    since_ns = watermarks.get(service)
    if since_ns is None:
        since_ns = now_ns - int(backfill_hours * 3600 * 1e9)
        print(f"  {service}: no watermark yet -- seeding with a {backfill_hours}h backfill.")
    else:
        age_s = (now_ns - since_ns) / 1e9
        print(f"  {service}: resuming from watermark ({age_s:.0f}s ago).")

    miner = build_miner(service)
    stream_path = os.path.join(STREAM_DIR, f"{service}.jsonl")

    last_ts_ns = since_ns
    new_events = 0
    with open(stream_path, "a") as out:
        for ts_ns, line in fetch_new_lines(service, since_ns, now_ns):
            result = miner.add_log_message(line)
            event = {
                "ts_ns": ts_ns,
                "template_id": result["cluster_id"],
                "template": result["template_mined"],
            }
            out.write(json.dumps(event) + "\n")
            last_ts_ns = ts_ns
            new_events += 1

    if new_events:
        miner.save_state("periodic pipeline run")
        # Advance the watermark one nanosecond past the last real event
        # actually processed -- start/end in Loki's query_range are both
        # inclusive-ish at the boundary, and we don't want to risk
        # re-pulling the exact same last line as a duplicate next run.
        _save_watermark(service, last_ts_ns + 1)
        print(f"    {new_events} events mined, watermark advanced, "
              f"{len(miner.drain.clusters)} real templates known so far.")
    else:
        # Still advance the watermark to `now_ns` even with zero new lines --
        # otherwise a genuinely quiet service (e.g. catalogue overnight)
        # would keep re-scanning the same empty range forever.
        _save_watermark(service, now_ns)
        print(f"    no new lines since last run.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill-hours", type=float, default=DEFAULT_BACKFILL_HOURS)
    args = parser.parse_args()

    _ensure_dirs()
    now_ns = int(time.time() * 1e9)

    print(f"Log-sequence pipeline run, {len(PIPELINE_SERVICES)} services, "
          f"now={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_ns / 1e9))}\n")

    for service in PIPELINE_SERVICES:
        print(f"[{service}]")
        try:
            process_service(service, args.backfill_hours, now_ns)
        except Exception as e:
            print(f"    ERROR processing {service}: {e} -- watermark NOT advanced, "
                  f"will retry this range on next run.")
        print()


if __name__ == "__main__":
    main()
