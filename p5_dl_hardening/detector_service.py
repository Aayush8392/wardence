"""
Real POST /detect service -- the locked Zone 1 "DL detector service"
spec from wardence_context.md, wiring Track A (DeepLog) and Track B
(HMM/SPC) into one live, per-service endpoint the agent can call.

Deliberately excludes Isolation Forest -- it lost the closed benchmark
against DeepLog on every Tier-1 service (see wardence_buildlog.md's
"Isolation Forest baseline benchmark -- CLOSED" section), so it stays a
benchmark artifact only, never part of the live signal the agent acts on.

Scope: only the 7 fault classes with real, confirmed log-based coverage
route through this service (see deeplog_service_config.py's tier lists
and wardence_context.md's fault-class coverage mapping). The other 5
classes have zero real log signal on any service (confirmed, not just
unbuilt) -- callers should not ask this service about those services at
all; it will refuse rather than return a meaningless answer.

Per-service routing (locked, see deeplog_service_config.py):
  front-end, orders, user   -> Track A (DeepLog LSTM)
  queue-master              -> Track B, HMM primary
  catalogue                 -> Track B, SPC primary (EWMA/CUSUM)

Each request pulls a fresh, small live window directly from Loki (never
reuses the training/calibration streams in pipeline_state/streams/ --
those are historical, this is real-time). Drain3 mining reuses each
service's persisted snapshot READ-ONLY: the miner loads existing cluster
state to map live lines to the same template IDs the models were
trained on, but this service never calls save_state(), so a live request
can never mutate the real training snapshot.

Honest simplifications vs. the full offline scorers, stated plainly
rather than silently narrowed:
  - Track A: scores the most recent SCORING_WINDOW real (context, next)
    transitions pulled live, using the SAME Binomial threshold computed
    once at startup from the offline held-out test set. Identical
    method to the offline scorer, just fed a live window instead of a
    saved one.
  - Track B/HMM (queue-master): continuous_per_step_log_probs is run
    fresh over just the live pulled window each request (not chained
    across requests), so the very first live event pays the same
    cold-start cost every request -- a real, deliberate simplification
    (statelessness over exact continuity), not a bug. Diluted across a
    WINDOW_SIZE-length window rather than concentrated onto every
    non-overlapping calibration window the way the pre-fix bug was, so
    this does not reintroduce the 2026-07-29 phase-alignment bug, but a
    single live check's very first step is still real-but-slightly-
    pessimistic. Documented, not hidden.
  - Track B/SPC (catalogue): a single-point Shewhart-style check against
    the same baseline mean/std + EWMA control limits already computed
    offline (not a full running EWMA/CUSUM state machine across
    requests, since there's no persistent per-request session here).
    A real, simpler check than the offline monitor, not the full
    sequential one -- documented so nobody mistakes this for the same
    CUSUM accumulation guarantee the offline script provides.

Requires: pip install fastapi uvicorn torch hmmlearn (already installed
for the offline Track A/B scripts this reuses).

Usage:
    uvicorn detector_service:app --host 0.0.0.0 --port 8010
"""

import os
import time

import numpy as np
import torch
from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig
from fastapi import FastAPI, HTTPException
from hmmlearn import hmm
from pydantic import BaseModel
from scipy.stats import binom

from deeplog_anomaly_scorer import (
    DeepLogLSTM,
    SCORING_WINDOW as DEEPLOG_SCORING_WINDOW,
    TOP_K,
    calibrate_threshold as deeplog_calibrate_threshold,
    predict_hits as deeplog_predict_hits,
)
from deeplog_service_config import (
    DEEPLOG_TIER_1,
    MASKING_INSTRUCTIONS,
    TRACK_B_PRIMARY_DETECTOR,
    get_depth,
    is_noise_template,
)
from hmm_anomaly_scorer import (
    WINDOW_SIZE as HMM_WINDOW_SIZE,
    calibrate_threshold as hmm_calibrate_threshold,
    continuous_per_step_log_probs,
    to_symbols as hmm_to_symbols,
)
from log_sequence_pipeline import LOKI_URL, NAMESPACE, _fetch_range_no_gaps
from spc_ewma_cusum_monitor import (
    EWMA_LAMBDA,
    EWMA_L,
    extract_channels as spc_extract_channels,
    load_spc_rows,
)
from track_a_deeplog_sequences import WINDOW_SIZE as DEEPLOG_CONTEXT_WINDOW

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRACK_A_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_a")
TRACK_B_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "track_b")
SNAPSHOT_DIR = os.path.join(SCRIPT_DIR, "pipeline_state", "drain3_snapshots")

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Real per-service routing, locked -- see this file's module docstring.
DEEPLOG_SERVICES = set(DEEPLOG_TIER_1)
HMM_SERVICES = {s for s, primary in TRACK_B_PRIMARY_DETECTOR.items() if primary == "hmm"}
SPC_SERVICES = {s for s, primary in TRACK_B_PRIMARY_DETECTOR.items() if primary == "spc"}
SUPPORTED_SERVICES = DEEPLOG_SERVICES | HMM_SERVICES | SPC_SERVICES

# How far back to pull live Loki events per request. Not derived from a
# real measured traffic-rate study the way training-data decisions in
# this project usually are -- a real, flagged gap, not a silent guess:
# tune this per-service if `insufficient_data` responses turn out to be
# common in practice once this is exercised live.
LIVE_PULL_MINUTES = 5

# SPC's baseline (upper/lower control limits) was calibrated per
# BUCKET_SECONDS=60s bucket in track_b_hmm_spc_features.py -- pulling a
# 5-minute window (LIVE_PULL_MINUTES) and comparing its raw event counts
# against a 60s-calibrated baseline is an apples-to-oranges scaling
# mismatch, confirmed as a real bug via the first live test of this file
# (2026-07-29): catalogue's every count channel came back "flagged" on
# ordinary traffic, consistently ~5x over baseline -- exactly what a
# 5-bucket-worth-of-events-read-as-one-bucket mismatch produces, not a
# real anomaly. SPC pulls its own BUCKET_SECONDS-matched window instead.
from track_b_hmm_spc_features import BUCKET_SECONDS  # noqa: E402

SPC_LIVE_PULL_SECONDS = BUCKET_SECONDS

app = FastAPI(title="Wardence Detector Service (Track A/B)")


class DetectRequest(BaseModel):
    service: str


# ---------------------------------------------------------------------
# Startup: load every model + calibrated threshold once, not per request.
# ---------------------------------------------------------------------

_deeplog_models: dict = {}
_hmm_models: dict = {}
_spc_baselines: dict = {}
_miners: dict = {}


def _load_drain3_miner(service: str) -> TemplateMiner:
    """Read-only load of the service's persisted drain3 snapshot -- this
    process never calls save_state(), so scoring a live request can
    never mutate the real training-time miner state."""
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"{service}.bin")
    if not os.path.exists(snapshot_path):
        raise RuntimeError(f"no drain3 snapshot found for {service} at {snapshot_path}")
    persistence = FilePersistence(snapshot_path)
    config = TemplateMinerConfig()
    config.masking_instructions = MASKING_INSTRUCTIONS
    config.drain_depth = get_depth(service)
    return TemplateMiner(persistence_handler=persistence, config=config)


def _load_deeplog(service: str):
    seq_path = os.path.join(TRACK_A_DIR, f"{service}_sequences.npz")
    seq_data = np.load(seq_path)
    vocab = list(seq_data["vocab"])
    id_to_index = {tid: i for i, tid in enumerate(vocab)}

    model_path = os.path.join(TRACK_A_DIR, f"{service}_lstm.pt")
    checkpoint = torch.load(model_path, map_location=DEVICE, weights_only=True)
    model = DeepLogLSTM(checkpoint["vocab_size"], checkpoint["embedding_dim"], checkpoint["hidden_size"])
    model.load_state_dict(checkpoint["state_dict"])
    model.to(DEVICE).eval()

    X_test = np.vectorize(id_to_index.get)(seq_data["X_test"]).astype(np.int64)
    y_test = np.vectorize(id_to_index.get)(seq_data["y_test"]).astype(np.int64)
    hits = deeplog_predict_hits(model, X_test, y_test, TOP_K)
    threshold, _, p_miss, k = deeplog_calibrate_threshold(hits)

    return {
        "model": model,
        "id_to_index": id_to_index,
        "threshold": threshold,
        "p_miss": p_miss,
        "k": k,
    }


def _load_hmm(service: str):
    path = os.path.join(TRACK_B_DIR, f"{service}_hmm_model.npz")
    data = np.load(path)
    template_ids = list(data["template_ids"])
    n_states = len(template_ids)

    model = hmm.CategoricalHMM(n_components=n_states, n_features=n_states)
    model.startprob_ = data["startprob"]
    model.transmat_ = data["transmat"]
    model.emissionprob_ = data["emissionprob"]
    id_to_index = {tid: i for i, tid in enumerate(template_ids)}

    threshold, mean_score, std_score, _ = hmm_calibrate_threshold(model, id_to_index, service)
    return {"model": model, "id_to_index": id_to_index, "threshold": threshold}


def _load_spc(service: str):
    rows = load_spc_rows(service)
    channels = spc_extract_channels(rows)
    split_idx = int(len(rows) * 0.8)  # matches spc_ewma_cusum_monitor.py's HELD_OUT_FRACTION=0.2

    baselines = {}
    for name, values in channels.items():
        train = values[:split_idx]
        mean = float(np.mean(train))
        std = float(np.std(train))
        if std == 0:
            continue  # matches monitor_channel's own "nothing to monitor" skip
        # float(...) here matters, not cosmetic: np.sqrt returns numpy.float64,
        # which silently propagates into "upper"/"lower" even though mean/std
        # are already plain floats -- comparing against a numpy.float64 bound
        # later produces numpy.bool_, which FastAPI's jsonable_encoder cannot
        # serialize at all (confirmed: a real 500 with an empty body, first
        # live test of this file, 2026-07-29).
        limit = float(EWMA_L * std * np.sqrt(EWMA_LAMBDA / (2 - EWMA_LAMBDA)))
        baselines[name] = {"mean": mean, "std": std, "upper": mean + limit, "lower": mean - limit}
    return baselines


@app.on_event("startup")
def load_all_models():
    for service in DEEPLOG_SERVICES:
        _deeplog_models[service] = _load_deeplog(service)
        print(f"[detector_service] loaded DeepLog model for {service}")
    for service in HMM_SERVICES:
        _hmm_models[service] = _load_hmm(service)
        print(f"[detector_service] loaded HMM model for {service}")
    for service in SPC_SERVICES:
        _spc_baselines[service] = _load_spc(service)
        print(f"[detector_service] loaded SPC baseline for {service}")
    for service in SUPPORTED_SERVICES:
        _miners[service] = _load_drain3_miner(service)
    print(f"[detector_service] ready -- supported services: {sorted(SUPPORTED_SERVICES)}")


# ---------------------------------------------------------------------
# Live event pull + template mining (shared across all three tracks)
# ---------------------------------------------------------------------

def _pull_live_events(service: str, window_seconds: float) -> list:
    """Real, fresh Loki pull for the last `window_seconds`, mined through
    the service's read-only drain3 miner. Returns a chronologically
    ordered list of (ts_ns, template_id, template_text) tuples -- the
    real mined template text is kept (not just the ID) so noise
    filtering (is_noise_template, which matches on real template text,
    not ID) works the same way it does in
    track_b_hmm_spc_features.py's offline pass.

    `window_seconds` is caller-supplied, not a single global constant --
    each track's calibration assumes its own real window size (SPC's
    baseline is per BUCKET_SECONDS bucket; DeepLog/HMM just need "enough
    recent events," not a specific bucket width), so a single shared
    window would silently mismatch whichever track didn't get consulted
    when it was picked (see SPC_LIVE_PULL_SECONDS's history above)."""
    now_ns = int(time.time() * 1e9)
    since_ns = now_ns - int(window_seconds * 1e9)
    logql = f'{{namespace="{NAMESPACE}", app="{service}"}}'
    lines = _fetch_range_no_gaps(logql, service, since_ns, now_ns)

    miner = _miners[service]
    events = []
    for ts_ns, line in lines:
        result = miner.add_log_message(line)
        events.append((ts_ns, result["cluster_id"], result["template_mined"]))
    return events


# ---------------------------------------------------------------------
# Per-track scoring
# ---------------------------------------------------------------------

def _score_deeplog(service: str, events: list) -> dict:
    needed = DEEPLOG_CONTEXT_WINDOW + DEEPLOG_SCORING_WINDOW
    if len(events) < needed:
        return {"status": "insufficient_data", "events_pulled": len(events), "events_required": needed}

    bundle = _deeplog_models[service]
    model, id_to_index, threshold = bundle["model"], bundle["id_to_index"], bundle["threshold"]

    recent = events[-needed:]
    ids = [tid for _, tid, _ in recent]

    contexts, next_ids, is_unseen = [], [], []
    for i in range(len(ids) - DEEPLOG_CONTEXT_WINDOW):
        window = ids[i:i + DEEPLOG_CONTEXT_WINDOW]
        nxt = ids[i + DEEPLOG_CONTEXT_WINDOW]
        if any(t not in id_to_index for t in window) or nxt not in id_to_index:
            is_unseen.append(True)
            contexts.append([0] * DEEPLOG_CONTEXT_WINDOW)  # placeholder, hit forced False below
            next_ids.append(-1)
        else:
            is_unseen.append(False)
            contexts.append([id_to_index[t] for t in window])
            next_ids.append(id_to_index[nxt])

    X = np.array(contexts, dtype=np.int64)
    y = np.array([n if n >= 0 else 0 for n in next_ids], dtype=np.int64)
    hits = deeplog_predict_hits(model, X, y, TOP_K)
    hits = np.array([False if unseen else h for h, unseen in zip(hits, is_unseen)])

    miss_rate = float(1 - hits.mean())
    return {
        "track": "deeplog",
        "anomaly_score": miss_rate,
        "threshold": threshold,
        "is_anomalous": miss_rate >= threshold,
        "events_used": len(recent),
        "saw_unseen_template": any(is_unseen),
    }


def _score_hmm(service: str, events: list) -> dict:
    needed = HMM_WINDOW_SIZE
    filtered = [(ts, tid, tpl) for ts, tid, tpl in events if not is_noise_template(service, tpl)]
    if len(filtered) < needed:
        return {"status": "insufficient_data", "events_pulled": len(filtered), "events_required": needed}

    bundle = _hmm_models[service]
    model, id_to_index, threshold = bundle["model"], bundle["id_to_index"], bundle["threshold"]

    recent_ids = [tid for _, tid, _ in filtered[-needed:]]
    symbols, saw_unseen = hmm_to_symbols(recent_ids, id_to_index)
    if saw_unseen:
        return {
            "track": "hmm", "anomaly_score": float("-inf"), "threshold": threshold,
            "is_anomalous": True, "events_used": len(recent_ids),
            "detail": "unseen template_id in live window -- automatic anomaly",
        }

    log_probs, _ = continuous_per_step_log_probs(model, symbols)
    min_score = float(min(log_probs))
    return {
        "track": "hmm",
        "anomaly_score": min_score,
        "threshold": threshold,
        "is_anomalous": min_score <= threshold,
        "events_used": len(recent_ids),
    }


def _score_spc(service: str, events: list) -> dict:
    events = [(ts, tid, tpl) for ts, tid, tpl in events if not is_noise_template(service, tpl)]
    if not events:
        return {"status": "insufficient_data", "events_pulled": 0, "events_required": 1}

    baselines = _spc_baselines[service]
    ids = [tid for _, tid, _ in events]
    counts: dict = {}
    for tid in ids:
        counts[str(tid)] = counts.get(str(tid), 0) + 1

    channel_results = {}
    any_anomalous = False
    for name, base in baselines.items():
        if name == "total_events":
            value = float(len(ids))
        elif name == "mean_gap_s":
            if len(events) < 2:
                continue
            gaps = [(events[i][0] - events[i - 1][0]) / 1e9 for i in range(1, len(events))]
            value = float(np.mean(gaps))
        elif name.startswith("count["):
            tid = name[len("count["):-1]
            value = float(counts.get(tid, 0))
        else:
            continue

        flagged = value > base["upper"] or value < base["lower"]
        any_anomalous = any_anomalous or flagged
        channel_results[name] = {"value": value, "upper": base["upper"], "lower": base["lower"], "flagged": flagged}

    return {
        "track": "spc",
        "is_anomalous": any_anomalous,
        "events_used": len(events),
        "channels": channel_results,
    }


# ---------------------------------------------------------------------
# API
# ---------------------------------------------------------------------

@app.post("/detect")
def detect(req: DetectRequest):
    service = req.service
    if service not in SUPPORTED_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{service}' has no real log-based detection coverage -- "
                f"supported services: {sorted(SUPPORTED_SERVICES)}. "
                "See wardence_context.md's DeepLog coverage mapping."
            ),
        )

    if service in DEEPLOG_SERVICES:
        events = _pull_live_events(service, LIVE_PULL_MINUTES * 60)
        result = _score_deeplog(service, events)
    elif service in HMM_SERVICES:
        events = _pull_live_events(service, LIVE_PULL_MINUTES * 60)
        result = _score_hmm(service, events)
    else:
        events = _pull_live_events(service, SPC_LIVE_PULL_SECONDS)
        result = _score_spc(service, events)

    return {"service": service, **result}


@app.get("/detect/services")
def list_services():
    return {
        "deeplog": sorted(DEEPLOG_SERVICES),
        "hmm": sorted(HMM_SERVICES),
        "spc": sorted(SPC_SERVICES),
    }
