"""
P3 Operator API: JWT-gated, three roles (admin / demo-trigger / viewer).

- admin: full access. Trigger any implemented fault class, manually
  promote/demote, read everything, manage accounts. Real username +
  password + TOTP code (2026-07-22) via POST /login -- see
  create_admin_account.py to bootstrap the first admin account. TOTP is
  required specifically because this login endpoint is reachable from
  the public-facing frontend (unlike the old mint_token.py flow, which
  never touched the network) -- see wardence_frontend.md's Auth Model
  section for the full reasoning.
- demo-trigger: real username + password (admin creates these via
  POST /accounts -- one permanent account for the project owner's own
  testing, others time-limited). Can only /trigger, and only the curated
  safe subset (SAFE_DEMO_CLASSES). Rate-limited: cooldown + daily cap per
  IP, and only one episode allowed in-flight system-wide at a time.
  Per-IP limiting is a deterrent, not identity verification -- a VPN/
  different network bypasses it. Softened by the global one-at-a-time
  rule.
- viewer: read-only, /trust only. No account needed -- anonymous IS
  viewer per the locked frontend design (see wardence_frontend.md).

Every request is audit-logged regardless of role/outcome.

Usage:
    python3 p3_trust_action/create_admin_account.py <username>   # once
    uvicorn operator_api:app --reload --app-dir p3_trust_action --port 8002
    Then POST /login with username/password(/totp_code) to get a session token.
"""

import datetime
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jwt
import requests
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "p2_readonly_loop"))

import sqlite3  # noqa: E402

import accounts  # noqa: E402
import publish_to_r2  # noqa: E402
from auth import create_token, decode_token  # noqa: E402
from trust_engine import (  # noqa: E402
    CAN_ACT,
    DB_PATH,
    PROMOTION_STREAK,
    REPORT_ONLY,
    ensure_trust_tables,
    get_trust_state,
    manual_set_state,
)
# Real manual safety-net endpoint (Kimi review 34 finding #8) -- see
# /admin/reset-catalogue-baseline below. Same direct-import pattern
# run_batch_plan.py's own BASELINE_CHECKS already uses, not a subprocess
# call -- these are plain kubectl-wrapping functions.
#
# The 4 evidence-check helpers + 1 constant below (added for Phase 1's
# async wrapper, Kimi review 33/36) are the SAME real production checks
# injector.py's own verification already uses for crash-loop/
# cpu-throttling -- reused directly rather than reimplemented, so the
# early-exit unlock condition can never quietly drift from what the
# injector itself considers "confirmed."
from injector import (  # noqa: E402
    CPU_THROTTLE_MIN_PERIODS_INCREASE,
    FAULT_CONFIG,
    _cfs_throttled_periods,
    _crash_loop_backoff_now,
    _ensure_catalogue_replica_baseline,
    _ensure_oom_baseline,
    _restart_count,
)
import carts_rotation  # noqa: E402


def _republish_to_r2() -> bool:
    """Refresh the public R2 snapshot right after a manual trust-state
    change (2026-07-24 fix). Without this, admin's /promote or /demote
    changes the LIVE DB instantly but the public Trust Ladder page (which
    reads the R2 snapshot, not the live DB -- see wardence_context.md Zone
    2) wouldn't show it until the next manual publish_to_r2.py run,
    making the override look like it silently failed. Best-effort: a
    publish failure (e.g. R2 credentials/network issue) must NOT fail the
    underlying trust-state change, which already succeeded in the DB --
    it just means the public snapshot stays stale until the next run,
    same as today, not a regression.

    Returns True/False (real success signal, added Phase 2 item 2) so a
    caller with an episode row to attach it to (currently only
    _attempt_resolve) can record a real republished_at timestamp -- the
    completion-signal half of the "so the frontend never shows stale
    R2-sourced data on an immediate tab-switch" gap flagged in
    _attempt_resolve's own docstring. Every existing caller ignores the
    return value, unaffected.
    """
    try:
        publish_to_r2.main()
        return True
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"WARNING: R2 republish after manual override failed: {e}")
        return False

app = FastAPI()

# The p4_frontend dev server needs cross-origin access to this API -- same
# CORS requirement as R2 (see wardence_frontend.md). Tighten allow_origins
# to the real Vercel domain once deployed; localhost:5173 covers local dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Session tokens issued at login never outlive a demo-trigger account's
# own expiry (a 24h-limited account shouldn't get a session that outlasts
# it) -- capped at this default for permanent accounts and admin, since
# an unbounded session token would be its own leaked-credential risk
# regardless of the account itself being revocable.
DEFAULT_SESSION_HOURS = 24

# Real 12-class v1 roster (wardence_context.md), expanded 2026-08-06 --
# was hardcoded to the original 3-class Phase B set, blocking live-trigger
# coverage for the 9 classes added since (C1/C2 taxonomy expansion).
IMPLEMENTED_CLASSES = {
    "crash-loop", "oom", "disk-full", "cpu-throttling",
    "under-provisioned-replicas", "bad-rollout",
    "network-latency", "memory-leak", "connection-pool-exhaustion",
    "network-partition", "init-failure", "session-cart-failure",
}
# RE-OPENED to the full 12-class roster, 2026-08-1x -- the original
# 2026-08-06 admin-only restriction on report-only classes (reasoning:
# "no bounded auto-fix to clean up after, unlike the auto-fix set") was
# re-examined and found weaker than it sounded at the time: all 6
# report-only injector functions are confirmed self-timed and
# self-reverting by construction (a `finally` block cleans up
# unconditionally once duration_s elapses -- extensively validated
# across this project's own Operator design saga, no exception ever
# found), so there's no scenario where a report-only class "gets stuck"
# the way the original reasoning implied. If anything, the 6 auto-fix
# classes are the ones that mutate the live cluster (patch_memory_limit,
# scale_deployment, real kubectl patches) -- report-only classes never
# dispatch anything at all. Real, expected demo-trigger volume is low
# enough that the one narrower legitimate concern left (connection-pool-
# exhaustion's MySQL flood and memory-leak's elevated memory both apply
# real shared-resource pressure during their hold, unlike the
# single-pod-contained classes) isn't worth gating on for this project's
# actual scale.
SAFE_DEMO_CLASSES = set(IMPLEMENTED_CLASSES)

# Real fix, same session: AUTO_FIX_CLASSES used to be implicit --
# _run_live_episode_inner's report-only-vs-auto-fix branch reused
# SAFE_DEMO_CLASSES as a stand-in for "has a real fix action," which
# only worked by coincidence while SAFE_DEMO_CLASSES happened to equal
# exactly the 6 auto-fix classes. The moment SAFE_DEMO_CLASSES was
# widened above (to unlock all 12 for demo-trigger), that coincidence
# broke silently -- confirmed via a real live test the same session:
# connection-pool-exhaustion (report-only, no fix) sat in awaiting_fix
# for 296s (nearly the full 5-minute abandonment ceiling) instead of
# auto-chaining to resolved in ~0s, because the branch now read it as
# an auto-fix class. A real, separate constant closes this for good --
# these two concepts (which classes demo-trigger may use vs. which
# classes have an actual fix to dispatch) are independent and must
# never be conflated again.
AUTO_FIX_CLASSES = {
    "crash-loop", "oom", "disk-full",
    "cpu-throttling", "under-provisioned-replicas", "bad-rollout",
}
# Per-class injection subprocess timeout, same shape/precedent as
# run_episodes.py's TARGET_RECENCY_WINDOW_S dict -- a single flat constant
# tried and failed here first (see history below), for the identical
# reason a flat TARGET_RECENCY_WINDOW_S failed: different classes have
# genuinely different real injector.py wall-clock cost, not just different
# duration_s values.
#
# History: originally a flat 400s, sized (2026-08-06/07) against an
# ASSUMED oom ceiling-hit cost of 200s/attempt. RECALIBRATED 2026-08-11
# to 450s (still flat) after two real live tests
# (p2_readonly_loop/test_oom_ceiling_worstcase.py,
# test_oom_real_live_window.py) found the real per-attempt ceiling-hit
# cost is ~260-266s, not 200s -- the poll loop's own `elapsed` counter in
# _inject_and_verify_oom only tracks OOM_VERIFY_POLL_S (3s) added per
# iteration, never the real ~1s of kubectl round-trip latency each
# iteration's two API calls also cost (confirmed: 3/3 forced ceiling-miss
# attempts landed at 260.1s/266.3s/261.9s). Real typical (non-ceiling-miss)
# oom kill time, measured across 5 live production-stressor runs:
# 6.6s/18.5s/31.1s/57.8s/91.3s.
#
# Converted to per-class SAME SESSION, 2026-08-11, once live-testing the
# 5 remaining report-only classes at their own real 180s max surfaced a
# real, DIFFERENT cost outlier: network-latency's real injector mechanism
# polls _probe_orders_latency_ms every 10s throughout the ENTIRE hold (not
# just once at the end) -- each probe spins a real throwaway pod
# (kubectl run --rm), already documented elsewhere in this codebase as
# ~28s for a clean idle round trip (LATENCY_PROBE_TIMEOUT_S=50s exists
# specifically because of this). At 180s that's 19 real probe calls
# (1 baseline + 18 in-loop); observed real single-attempt cost was 306.7s
# -- already close to the flat 450s ceiling on ONE successful attempt, no
# retry needed. A single flat constant covering both oom's ceiling-miss
# shape and network-latency's per-probe-overhead shape would have to be
# padded for the worse of the two on EVERY class, which is exactly the
# TARGET_RECENCY_WINDOW_S mistake repeating itself.
#
# Real per-class values below, each derived from its own real live-test
# result (2026-08-11) + margin, using the same design intent throughout:
# cover the real observed cost with real margin, not the full theoretical
# worst case (e.g. every probe hitting its own 50s internal timeout) --
# a genuinely pathological run should still trip this timeout and surface
# as a clean 504, not be silently padded for.
INJECT_SUBPROCESS_TIMEOUT_S = {
    # Report-only classes, all real-tested at the locked 180s max
    # (2026-08-11). Six of seven cost ~181-190s real (clean, minimal
    # per-probe overhead) -> 260s gives ~70-80s real margin.
    "network-partition": 260,           # real: 181.0s
    "init-failure": 260,                # real: 181.0s
    "session-cart-failure": 260,        # real: 190.4s
    "connection-pool-exhaustion": 260,  # real: 184.2s
    # network-latency: real outlier, see the class docstring above --
    # every-10s real throwaway-pod probing throughout the hold. Real:
    # 306.7s on ONE clean attempt. 500s covers real probe-overhead
    # variance (average ~6.67s/probe this run, room for it to run
    # notably slower under real cluster load) without being padded for
    # every probe hitting its own internal 50s cap.
    "network-latency": 500,
    # Auto-fix classes extended under completion-gating (2026-08-11 live
    # tests). crash-loop: real 188.3s, single attempt, clean -> 260s.
    "crash-loop": 260,
    # oom: NOT extended the same way (no duration_s hold -- exits on
    # confirmed kill or the real ~260-266s ceiling). Real math: one
    # ceiling-miss (266s, rounded to 270s) + one typical success (91.3s
    # observed max, rounded to 100s) = 370s.
    #
    # RECALIBRATED 450 -> 500, 2026-08-1x (Kimi review 34 finding #8,
    # confirmed via direct code read then a real live timing test,
    # test_oom_baseline_reset_timing.py): the 450s figure excluded a real,
    # occasional cost -- _ensure_oom_baseline (called inside injector.py's
    # main(), the SAME subprocess this timeout bounds) can run a genuine
    # kubectl rollout restart + up to a 300s rollout-status wait right
    # before injection, if a prior real fix left catalogue's memory limit
    # raised. Real measured cost of that reset alone: 181.6s (close to,
    # and driven by the same root cause as, oom's own ~185s post-kill
    # recovery number -- catalogue's readinessProbe.initialDelaySeconds=
    # 180). 500s = 370s (ceiling-miss + typical-success math above) +
    # margin, treating "both a fresh baseline-reset AND a ceiling-miss on
    # the same trigger" as the one genuinely rare compound case acceptable
    # to a clean 504, not padded for on every single call. The real,
    # PRIMARY fix for this cost is p3_scorer.py's new automatic
    # post-episode baseline-reset (moves the reset out of this hot path
    # entirely, into the end of the PRECEDING episode's lifecycle) -- this
    # number just covers the rare case that automatic reset didn't run
    # (e.g. a crashed scorer process) and injector.py's own lazy check has
    # to do it here instead.
    "oom": 500,
    # Remaining 3 auto-fix classes: NOT live-tested at an extended
    # duration this session (disk-full is a confirmed hard ceiling, never
    # extended; bad-rollout is a standing config change with a short
    # verification burst, not a "hold longer" mechanism; cpu-throttling's
    # real resource-safety was already tested up to 300s in an earlier
    # session, 2026-08-01, but its own injector.py wall-clock cost wasn't
    # specifically measured the way the other 8 classes were today).
    # Derived from each class's real production duration_s + generous
    # margin, not measured -- revisit with a real live test the same way
    # as the other 8 if these are ever extended for live-visibility too.
    "disk-full": 220,             # duration_s=60, natural hard ceiling, no extension
    "bad-rollout": 200,           # duration_s=60, standing config change
    # RECALIBRATED 350 -> 900, 2026-08-19, after the real probe-loosening
    # demo-visibility fix landed (see injector.py's CPU_THROTTLE_LIVE_
    # TRIGGER_*/_inject_and_verify_cpu_throttling_live_trigger). Real,
    # NOT YET LIVE-MEASURED reasoning, sized generously on purpose: the
    # live-trigger path now brackets the 300s hold with TWO real
    # Deployment rollouts (loosen before, restore after), and each
    # fresh replacement pod still has to wait out user's real
    # readinessProbe.initialDelaySeconds=180s before `kubectl rollout
    # status` reports success (only periodSeconds/timeoutSeconds/
    # failureThreshold are patched, initialDelaySeconds is never
    # touched -- confirmed live, every probe dump this session still
    # showed the real 180s value). 900 = 300s hold + 2x240s (each
    # rollout's own internal timeout, itself already padded past the
    # real 180s floor) + margin. Revisit with a real live timing test
    # (same discipline as oom's/UPR's own recalibration entries above)
    # before trusting this number under real pressure -- it is a safe
    # upper bound, not a measured one.
    "cpu-throttling": 900,
    # under-provisioned-replicas: RECALIBRATED 150 -> 300, 2026-08-1x
    # (Kimi review 34 finding #8's scope extended past the review itself,
    # to UPR -- it shares the same catalogue target/baseline-reset cost as
    # oom, via _ensure_oom_baseline(FAULT_CONFIG["oom"]) called inside its
    # OWN injector function). Real live test (test_upr_worst_case_timing.py,
    # simulating a prior real oom fix AND a prior real UPR fix both landed
    # on catalogue first): 209.1s real, genuine overage against the old
    # 150s budget. 300s = real 209.1s + ~90s margin. Same primary-fix
    # framing as oom above -- this covers the rare case the new automatic
    # post-episode reset didn't run.
    "under-provisioned-replicas": 300,
    # memory-leak: RECALIBRATED 2026-08-21 for the real production
    # mechanism (JVM-attach LeakAgent against shipping), replacing the OLD
    # StressChaos entry above (260s, measured against the now-removed
    # 100s-duration container-stressor mechanism -- no longer a valid
    # comparison). Real locked config: 180s hold + 35s settle (real
    # floor-capture wait before the agent starts ramping) + real overhead
    # margin (ramp confirmation, synthetic-load-burst launch, RELEASE/
    # reap in the finally block) = 300s. No clone-only warmup phase
    # needed in production, per the established "production shipping is
    # always warm from real organic traffic" finding -- unlike the clone
    # harness, which needed its own warmup window before a clean measurement.
    "memory-leak": 300,
}
DEFAULT_INJECT_SUBPROCESS_TIMEOUT_S = 300  # any class not yet in the dict above
# The subprocess call below is wrapped in try/except TimeoutExpired, so
# even a genuine miss fails with a clean error, not a crash.
COOLDOWN_S = 60
DAILY_CAP = 3  # per-IP cap -- a fairness layer, NOT the real budget protection

# The REAL budget protection, per wardence_context.md's abuse-prevention
# design -- caps TOTAL demo-trigger episodes across ALL visitors/IPs per
# day, regardless of how many accounts/IPs an abuser could spread across.
# Decided 2026-07-22: 10/day. Separate from, and much smaller than, the
# ~150-episode figure elsewhere in the docs -- that number is a one-time
# total-project planning estimate (how much testing was expected during
# BUILD, used only to confirm free-tier LLM quota would cover it), not a
# lifetime cap on anything -- the system has no memory of "episodes ever
# run." This cap resets daily, same as the free-tier API quotas it exists
# to protect.
GLOBAL_DAILY_CAP = 10

INJECTOR_PATH = Path(__file__).parent.parent / "p2_readonly_loop" / "injector.py"
INJECTOR_CWD = Path(__file__).parent.parent / "p2_readonly_loop"
SCORER_PATH = Path(__file__).parent / "p3_scorer.py"
SCORER_CWD = Path(__file__).parent

PROMETHEUS_URL = "http://localhost:9090"
STATUS_NAMESPACE = "sock-shop"

# Matches p2_readonly_loop/run_episodes.py's own SETTLE_SECONDS -- same
# documented race (kube-state-metrics scrapes every 30s; scoring before a
# full cycle has passed can read stale state and misdiagnose a genuine
# fault as "no anomaly"). Never skip this, even here.
SETTLE_SECONDS = 35

# Real bug found and fixed 2026-08-1x (episode a0e27f54, network-partition):
# snapshot_at = t0 + SETTLE_SECONDS is computed from INJECTION START, not
# fault-end -- fine for batch mode (duration_s=60s, diagnosis naturally
# happens at fault-end+35s=~95s past t0) but wrong for a live-triggered
# holding episode, where snapshot_at fires mid-fault regardless of how
# long the real hold (duration override) runs. Most classes are immune
# (max_over_time-for-a-spike signals register correctly whenever queried
# mid-fault; min_over_time-for-a-drop signals like session-cart-failure's
# scale-to-0 manifest instantly). network-partition is the one exception:
# its min_over_time-for-a-drop signal depends on the underlying iptables
# block itself, which injector.py's own docstring confirms takes ~30-40s
# of real propagation before traffic is reliably near-zero -- the fixed
# 35s settle lands right at that boundary, sometimes before it's clean.
# This override gives network-partition a wider margin so snapshot_at
# reliably lands past the documented worst case; every other class keeps
# the original 35s via .get()'s default.
SETTLE_SECONDS_OVERRIDE = {
    "network-partition": 60,
}

# Extra margin added on top of whatever's left of SETTLE_SECONDS when
# /trigger/resolve is called before the full settle window has naturally
# elapsed (2026-07-24, two-phase trigger flow). Not a new race-condition
# fix -- SETTLE_SECONDS is already the proven-sufficient number (see
# disk-full's five-root-cause saga in wardence_buildlog.md) -- this is
# just a small safety pad for the user-controlled variant, where "elapsed
# since t0" is measured server-side at click time rather than via a fixed
# sleep started right after injection.
RESOLVE_SAFETY_BUFFER_S = 5

# Real bug found during Phase B testing (2026-07-24): a user who took
# several minutes between clicking "Trigger Injection" and "Diagnose &
# Fix" (e.g. mid-discussion, mid-distraction) got back a hollow "scored"
# response with every field null -- p3_scorer.py's OWN staleness guard
# (MAX_EPISODE_AGE_MINUTES=10, meant for a totally different scenario --
# an abandoned leftover row from an old session) silently refused to
# score the episode, exited 0 anyway, and operator_api.py had no idea
# nothing had actually happened.
#
# This constant is the REAL fix, not a bandage on that symptom: every
# diagnosis query in agent.py has a genuine, bounded PromQL lookback
# window (confirmed by reading the file, not assumed) -- [3m] for
# restarts/OOM/eviction/connection-pool, [2m] for network-latency.
# memory-leak's own window is [60s] as of the 2026-08-21 production
# mechanism swap (was [3m], tied to the now-removed StressChaos
# mechanism) -- narrower than every window this constant was originally
# sized against, so the "just above the longest real query window"
# reasoning below is now conservative for memory-leak specifically, not
# invalidated by it. Wait too long past injection and the agent's own
# queries will correctly see nothing, because the real evidence has
# aged out of the window it checks -- producing a FALSE "wrong" that
# reflects nothing about the system's real accuracy, only that the user
# waited too long. That would silently corrupt the real published trust/
# calibration stats with illegitimate data (the same contamination risk
# flagged in the 2026-07-24 audit).
#
# 180s (3 minutes) sits just above the longest real query window (3m)
# with a small buffer, comfortably covering every current live class's
# own duration_s (max 60s) plus SETTLE_SECONDS. Past this, /trigger/
# resolve hard-refuses rather than silently scoring a fault result that
# isn't a fair reflection of the agent -- the episode is simply never
# scored (matches this project's standing "refuse rather than corrupt"
# principle, same as injector.py's own total-failure handling).
#
# NOTE, Phase 1 async redesign: this constant is currently unused by the
# real trigger flow below (report-only classes' own extended hold
# durations already exceed it, and auto-fix classes now settle-wait +
# score via _attempt_resolve's own SETTLE_SECONDS handling regardless of
# elapsed time). Left in place, not deleted -- still documents the real
# reasoning behind SETTLE_SECONDS/RESOLVE_SAFETY_BUFFER_S, and may be
# reintroduced as an explicit guard if a future session decides one is
# still warranted for a genuinely abandoned-then-revived episode.
RESOLVE_WINDOW_MAX_S = 180

# Real concurrency guard for the row-CREATION race only (Kimi review 36
# finding 12 -- narrowed scope from the old design's broader in-memory
# busy flag, now that episode_state IS the real source of truth for
# "something's in flight" once a row exists). Two near-simultaneous
# /trigger/inject calls could both query _episode_in_flight, both see
# nothing non-terminal, and both proceed to insert a row and start an
# injector.py subprocess before either write lands -- this lock's ONLY
# job is closing that millisecond-scale window between "check the DB"
# and "the new row exists." Once the row exists, _episode_in_flight
# (querying episode_state) is the real guard for everything after that,
# same as the DB-backed "already scored" check already guards
# double-resolving. Not used anywhere else -- resolving/holding/
# abandon-flag state all live in the DB, not behind this lock.
_TRIGGER_LOCK = threading.Lock()

# Tracks the live 5-minute abandonment Timer for whichever episode is
# currently awaiting_fix, keyed by episode_id (Kimi review 37 finding 7)
# -- without this, a Timer that lost the Gate 2 CAS to a manual resolve
# just sits alive for the remainder of its 5 minutes before firing and
# silently no-op'ing, a real (if harmless) leak on a 24/7 process. Only
# ever holds one real entry at a time given the single-episode-in-flight
# invariant, but keyed by episode_id rather than a single global
# variable so a stale reference can never be popped/cancelled against
# the wrong episode.
_ABANDON_TIMERS: dict[str, threading.Timer] = {}

# p3_scorer.py's own agent request timeout is already 180s (durability
# windows run up to 3 min for oom -- see p3_scorer.py's docstring); give
# the subprocess itself real margin beyond that, not a tight guess.
SCORER_TIMEOUT_S = 400

# Where the wrapper thread's DB-to-file bridge writes the early-exit
# signal injector.py's crash-loop/cpu-throttling hold loops check
# (Kimi review 36 finding 2/7 -- a file, not a DB poll, since injector.py
# has no DB connection). One file per episode so a stale leftover file
# from a previous episode can never fire on the wrong one.
STOP_FILE_DIR = Path("/tmp") if Path("/tmp").exists() else Path.home() / "wardence_stop_files"
STOP_FILE_DIR.mkdir(parents=True, exist_ok=True)

# Real per-episode injector.py output, for live debugging (see
# _run_live_episode_inner's Popen call for why this replaced an
# earlier DEVNULL/PIPE attempt). Not auto-cleaned -- small, human-
# readable text files, left for manual inspection/cleanup same as the
# stop-file convention.
LIVE_TRIGGER_LOG_DIR = Path("/tmp") if Path("/tmp").exists() else Path.home() / "wardence_trigger_logs"
LIVE_TRIGGER_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Same real dir p3_agent.py's REASONING_STREAM_DIR resolves to (both
# processes run on the same machine) -- kept as a literal duplicate of
# that same fallback logic (not imported, to avoid a cross-service
# import) rather than a shared constants module, matching this project's
# existing "duplicated by hand, kept in sync" convention for the handful
# of things genuinely shared between agent-side and API-side code
# (FAULT_CLASSES, FIELD_GUIDANCE, DETERMINISTIC_ACTION_MAP).
REASONING_STREAM_DIR = Path("/tmp") if Path("/tmp").exists() else Path.home() / "wardence_reasoning_streams"
# Real polling cadence for tailing the reasoning-events file below --
# fast enough to feel live (token-by-token gemma reasoning arrives in
# a burst of small chunks), cheap enough not to matter (a few hundred
# stat+read calls over a diagnosis call's real few-second lifetime).
REASONING_STREAM_POLL_S = 0.15
# Real safety ceiling -- if a "done" event never arrives (e.g. the
# diagnosis subprocess itself crashed before writing one), the SSE
# connection self-closes rather than holding a browser connection open
# forever. Real, deliberately generous number: the frontend now opens
# this connection right after inject (episodeId set), NOT when it first
# observes episode_state=="resolving" -- fixed 2026-08-1x after a real,
# live-confirmed race (a 4s live-status poll interval routinely missed
# a diagnosis call that finished in 1-5s, so the browser connected to an
# already-finished stream and burst-read everything at once instead of
# watching it live). Opening early means this ceiling must now cover the
# full real worst case BEFORE resolving even starts, not just the
# diagnosis call itself: holding's own up-to-300s extended window +
# awaiting_fix's 5-minute (300s) abandonment ceiling + a real diagnosis
# call (well under 60s in practice). 700s gives real margin over that
# ~660s worst case. Cheap to hold open this long -- the tail loop below
# only polls for the file's existence/new lines every
# REASONING_STREAM_POLL_S, negligible cost while nothing's been written.
REASONING_STREAM_MAX_S = 700


def _stop_file_path(episode_id: str) -> Path:
    return STOP_FILE_DIR / f"wardence_stop_{episode_id}"


def _evidence_file_path(episode_id: str) -> Path:
    """Same dir/naming convention as _stop_file_path, opposite
    direction -- report-only classes' injector.py writes THIS file
    (never the wrapper), the wrapper only ever reads it."""
    return STOP_FILE_DIR / f"wardence_evidence_{episode_id}"


# Real episode-state-machine substrate for Operator's live-trigger flow
# (Phase 1 item 4, locked design -- see wardence_frontend.md's Operator
# saga, reviewed via Kimi review 35). ONLY operator_api.py ever writes
# these columns -- injector.py stays fully agnostic (it never references
# them, per Kimi review 35 finding 3's ownership split), so the migration
# only needs to live here, not duplicated into injector.py's own
# connection setup. Additive/nullable, same convention as every other
# migration in this codebase (llm_replay_test.py/quota_tracker.py/
# p3_scorer.py) -- existing rows (all real batch episodes) keep every
# new column NULL/default forever, since this machinery is Operator-only.
#
# Uses try/except OperationalError instead of the usual PRAGMA-table_info
# check-then-add pattern (Kimi review 35 finding 3): PRAGMA-then-add is
# not atomic across processes -- two processes racing to add the same
# missing column on a fresh DB can both pass the check before either
# commits, and the loser crashes with "duplicate column name" instead of
# silently no-op'ing. try/except is safe under that race; PRAGMA-then-add
# is not.
_EPISODE_STATE_COLUMNS = [
    # NULL for every batch-run episode (run_batch_plan.py never touches
    # this) -- one of injecting/holding/awaiting_fix/resolving/resolved/
    # failed for a live-triggered one. 'failed' exists because the async
    # wrapper pre-creates this row before injector.py runs (so there's a
    # DB-backed in-flight signal during injection itself) -- if
    # injector.py then fails, the row needs a terminal state to land in
    # rather than being deleted (which would erase the audit trail of a
    # real attempt that already consumed rate-limit budget and cluster
    # time).
    ("episode_state", "TEXT"),
    # ISO timestamp of the last state transition -- what real
    # startup-reconciliation recomputes elapsed time from after an API
    # restart, instead of losing all in-flight timer state to the crash
    # (this project has hit exactly that shape of bug once already, the
    # uvicorn-crash-left-port-8002-held incident).
    ("state_entered_at", "TEXT"),
    # Gate 1: checked by the crash-loop/cpu-throttling injection loop at
    # its own ~10s tick during 'holding' only. Set by either the user's
    # early "Diagnose & Fix" click or an abandon signal (logout/tab-
    # close) arriving mid-hold -- whichever sets it first wins, the
    # second setter is a harmless no-op.
    ("stop_hold_requested", "INTEGER NOT NULL DEFAULT 0"),
    # Drives Gate 2's auto-fire (the awaiting_fix -> resolving atomic
    # transition) alongside a manual click and the 5-minute abandonment
    # ceiling -- all three attempt the identical CAS, whichever claims
    # it first wins, the rest silently no-op.
    ("abandon_requested", "INTEGER NOT NULL DEFAULT 0"),
    # manual | auto_resolve, set atomically with the resolved transition
    # (never by p3_scorer.py, which stays agnostic same as injector.py --
    # see Kimi review 35 finding 8). Observability only, never affects
    # scoring -- both are equally real episodes.
    ("triggered_by", "TEXT"),
    # NULL for batch episodes, or once the subprocess exits. Lets
    # startup-reconciliation distinguish "subprocess still genuinely
    # running" from "subprocess died without updating state" via
    # os.kill(pid, 0), instead of guessing purely from elapsed time
    # against INJECT_SUBPROCESS_TIMEOUT_S (Kimi review 35 finding 1).
    ("subprocess_pid", "INTEGER"),
    # crash-loop/cpu-throttling only (Kimi review 33's original design,
    # wired for real here). While episode_state='holding', a background
    # poll (folded into the same Popen-wait loop that owns the
    # subprocess, per Kimi review 36 finding 7 -- no separate thread)
    # checks the class's real production evidence field every ~10s
    # (crash-loop: real restartCount delta; cpu-throttling: the same
    # raw CFS-throttle-periods delta injector.py's own verification
    # already uses). Once confirmed, this flips to 1 and the frontend's
    # live-status poll unlocks the "Diagnose & Fix early" button.
    ("evidence_confirmed", "INTEGER NOT NULL DEFAULT 0"),
    # Set at row pre-creation from the triggering request's own JWT
    # payload. Lets POST /logout (Kimi review 36 finding 8: best-effort,
    # never a hard dependency for correctness -- the 5-minute ceiling is
    # the real, reliable auto-resolve mechanism regardless) check
    # whether the account logging out actually owns the one episode
    # currently in flight before setting abandon_requested, rather than
    # any logged-in user's logout abandoning someone else's episode.
    ("triggering_username", "TEXT"),
    # Phase 2 item 2 (real-time R2 republish completion signal): NULL
    # until _attempt_resolve's own _republish_to_r2() call genuinely
    # succeeds for THIS episode. Distinct from episode_state='resolved'
    # -- an episode can be resolved (scorer ran, DB updated) for several
    # real seconds before the R2 publish (a real ~12s cost, see
    # wardence_frontend.md's Operator saga) actually finishes. The
    # not-yet-built Operator frontend's completion-poll should wait for
    # this to be non-NULL, not just episode_state=='resolved', before
    # treating the public Trust Ladder/Replay Viewer R2 snapshot as safe
    # to re-fetch. Stays NULL forever if the publish itself fails
    # (best-effort, per _republish_to_r2's own docstring) -- the frontend
    # should treat "resolved, republished_at still NULL after a
    # reasonable wait" as "stale snapshot, not stuck," not an error.
    ("republished_at", "TEXT"),
]

# Non-terminal episode_state values -- an explicit allow-list, not a
# "!= resolved" exclusion (Kimi review 35 finding 2): SQL's
# `episode_state != 'resolved'` is true for NULL too only by accident of
# how NULL comparisons behave, and silently mis-including a future new
# terminal state (this project already added 'failed' on top of the
# original 5-state list) is exactly the kind of bug an explicit list
# fails loudly on instead. 'abandoned' is deliberately NOT a distinct
# state (Kimi review 36 finding 4) -- a report-only episode abandoned
# mid-injection lands in 'failed' with triggered_by='abandoned', reusing
# the same terminal state and the same observability field rather than
# growing the state enum for a case that's really just one more reason
# an episode didn't reach 'resolved'.
NON_TERMINAL_EPISODE_STATES = {"injecting", "holding", "awaiting_fix", "resolving"}
TERMINAL_EPISODE_STATES = {"resolved", "failed"}

# The 8 classes with a genuinely extendable, early-exit-capable hold --
# crash-loop/cpu-throttling (Kimi review 33's original locked design)
# plus all 6 report-only classes (same session extension, after
# confirming each one's own injector function already has a real
# verification step whose "confirmed" moment can be signaled out).
# Every other auto-fix class (oom/disk-full/under-provisioned-replicas/
# bad-rollout) goes straight from injecting to awaiting_fix once its
# own subprocess call returns -- no extendable hold to interrupt.
HOLDING_CLASSES = {
    "crash-loop", "cpu-throttling",
    "network-latency", "network-partition", "memory-leak",
    "connection-pool-exhaustion", "init-failure", "session-cart-failure",
    # Added 2026-08-15 -- under-provisioned-replicas' own real sustained
    # k6-burst hold (see injector.py's _inject_and_verify_under_provisioned).
    # First AUTO-FIX class to land in the evidence-file group rather than
    # WRAPPER_POLLED_EVIDENCE_CLASSES -- confirmed safe: AUTO_FIX_CLASSES/
    # HOLDING_CLASSES are already independent set-membership checks
    # everywhere this matters (real-dispatch branch, evidence routing),
    # not coupled the way SAFE_DEMO_CLASSES/AUTO_FIX_CLASSES used to be
    # before that was deliberately fixed.
    "under-provisioned-replicas",
}

# Real bug found and fixed the same session: FAULT_CONFIG's own
# duration_s for every one of these 8 classes is its ORIGINAL,
# pre-Operator-extension value (crash-loop 40s, cpu-throttling/
# network-latency/network-partition/init-failure/session-cart-failure
# 60s -- memory-leak's own original value was 100s, since corrected --
# see the note below) -- the real 180s/300s durations locked across
# several earlier design sessions (live-tested, safety-verified) were
# only ever applied via --duration-override in one-off test scripts,
# never wired into the actual live-trigger wrapper. Confirmed live,
# same session: a real connection-pool-exhaustion trigger's own log
# showed "holding for the full 60s window", not 180s -- every holding-
# class test run tonight before this fix used the wrong, short
# duration. Passed explicitly here rather than editing FAULT_CONFIG
# itself, matching the existing --duration-override convention (a
# fresh per-run cfg copy, FAULT_CONFIG never mutated) -- batch runs are
# UNAFFECTED, this dict is only ever consulted for live triggers.
#
# memory-leak's own entry below is now a genuine no-op, not stale data
# left uncorrected: FAULT_CONFIG["memory-leak"]["duration_s"] was
# directly fixed to 180 earlier in the 2026-08-21 session (the real
# production mechanism's own locked hold, replacing the old 100s
# StressChaos-era value) -- both this override and the base config now
# agree. Kept here anyway, not removed, for the same explicit-symmetry
# reasoning every other class's entry already follows in this dict.
LIVE_TRIGGER_DURATION_OVERRIDE_S = {
    "crash-loop": 180,
    "cpu-throttling": 300,
    "network-latency": 180,
    "network-partition": 180,
    "connection-pool-exhaustion": 180,
    "session-cart-failure": 180,
    "memory-leak": 180,
    "init-failure": 180,
    # Real, live-measured 2026-08-15 (95/100/105 VUS all clean across full
    # 180s sustained runs, see injector.py's UNDER_PROVISIONED_LIVE_TRIGGER_VUS
    # docstring for the real data) -- batch runs are unaffected, this dict
    # is only ever consulted for live triggers.
    "under-provisioned-replicas": 180,
}

# Two different real evidence SOURCES for the 8 holding classes, not
# one -- crash-loop/cpu-throttling's evidence is a cheap Prometheus
# read the wrapper can safely re-run on its own every tick (see
# _evidence_confirmed_now). The 6 report-only classes' own evidence
# checks are active, real-cost probes (a throwaway pod, an actual mysql
# connection attempt) -- re-running those from the wrapper in parallel
# would double real load and risk skewing the very signal being
# measured, so instead injector.py itself writes an evidence-file the
# moment ITS OWN real verification first confirms, and the wrapper just
# polls for that file's existence.
WRAPPER_POLLED_EVIDENCE_CLASSES = {"crash-loop", "cpu-throttling"}
EVIDENCE_FILE_CLASSES = HOLDING_CLASSES - WRAPPER_POLLED_EVIDENCE_CLASSES

# 5-minute abandonment ceiling (Kimi review 33/36, matches the
# durability verifier's own existing upper bound for
# memory-leak/cascading-dependency-failure -- not an arbitrary new
# number). Fires the awaiting_fix -> resolving CAS automatically if
# nobody clicks "Diagnose & Fix" first.
ABANDON_CEILING_S = 300


def _ensure_episode_state_columns(conn) -> None:
    for col, decl in _EPISODE_STATE_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE episodes ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
    conn.commit()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    accounts.ensure_accounts_tables(conn)
    _ensure_episode_state_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT,
            endpoint TEXT NOT NULL,
            detail TEXT,
            ip TEXT,
            recorded_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS demo_trigger_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            triggered_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def _audit(conn, role: str, endpoint: str, detail: str, ip: str):
    conn.execute(
        "INSERT INTO operator_audit (role, endpoint, detail, ip) VALUES (?, ?, ?, ?)",
        (role, endpoint, detail, ip),
    )
    conn.commit()


def require_role(*allowed_roles: str):
    """
    Returns the full decoded payload (role + username), not just the role
    string -- account-management endpoints need to know WHO is calling
    (e.g. to block an admin changing its own password through this panel),
    not just what role they hold.
    """

    def dependency(authorization: str = Header(default=None)) -> dict:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing or malformed Authorization header")
        token = authorization.removeprefix("Bearer ")
        try:
            payload = decode_token(token)
        except jwt.PyJWTError as e:
            raise HTTPException(401, f"invalid token: {e}")
        role = payload["role"]
        if role not in allowed_roles:
            raise HTTPException(403, f"role '{role}' not permitted for this endpoint")
        return payload

    return dependency


@app.get("/trust")
def trust(request: Request, payload: dict = Depends(require_role("admin", "demo-trigger", "viewer"))):
    role = payload["role"]
    conn = _conn()
    states = [get_trust_state(conn, fc) for fc in PROMOTION_STREAK]
    _audit(conn, role, "/trust", "read", request.client.host)
    conn.close()
    return {"states": states}


# SUPERSEDED, 2026-08-1x -- was an age-based heuristic on the
# t0-vs-scores-row gap (originally 10 minutes, lowered to 4 in the
# 2026-07-24 Phase B session). Confirmed via real arithmetic (Kimi
# review 35 finding 4) to be genuinely wrong once holding classes exist:
# cpu-throttling's real worst case is up to 300s hold + 35s settle + 300s
# abandonment ceiling = 635s -- almost 3x this heuristic's 240s bound.
# _episode_in_flight below now queries episode_state directly (exact,
# not a guess) -- this constant is kept only as a legacy fallback for
# rows with episode_state IS NULL (pre-migration batch episodes have no
# state at all, and would otherwise be invisible to this check forever;
# in practice batch rows are always already scored by the time anyone
# calls this, so the fallback rarely if ever fires).
LEGACY_EPISODE_IN_FLIGHT_MAX_AGE_MINUTES = 4


def _in_flight_episode_row(conn) -> tuple[str, str] | None:
    """Real (episode_id, fault_class) for whichever episode is currently
    in flight, or None -- added 2026-08-15 so the frontend can rehydrate
    its local activeEpisode state after a page refresh (previously only a
    bare bool was available anywhere, so a mid-episode refresh lost all
    context: the grid/panel had no way to know WHICH class was actually
    still running, just that triggering a new one was blocked). The
    legacy age-based fallback intentionally has no real fault_class to
    report (pre-migration rows never recorded one in a form this can
    trust) -- returns None for the class in that rare path, callers must
    handle it."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_EPISODE_STATES)
    row = conn.execute(
        f"SELECT episode_id, fault_class FROM episodes WHERE episode_state IN ({placeholders}) LIMIT 1",
        tuple(NON_TERMINAL_EPISODE_STATES),
    ).fetchone()
    if row is not None:
        return (row[0], row[1])

    legacy_row = conn.execute(
        """
        SELECT e.episode_id, e.t0 FROM episodes e
        LEFT JOIN scores s ON e.episode_id = s.episode_id
        WHERE s.episode_id IS NULL AND e.episode_state IS NULL
        ORDER BY e.t0 DESC
        LIMIT 1
        """
    ).fetchone()
    if legacy_row is None:
        return None
    episode_id, t0_str = legacy_row
    t0 = datetime.datetime.fromisoformat(t0_str)
    age_minutes = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds() / 60
    if age_minutes <= LEGACY_EPISODE_IN_FLIGHT_MAX_AGE_MINUTES:
        return (episode_id, None)
    return None


def _episode_in_flight(conn) -> bool:
    """Exact, not heuristic (Kimi review 35 finding 4): a live-triggered
    episode is in flight iff its episode_state is one of the real
    non-terminal states -- no age guessing involved. Thin wrapper over
    _in_flight_episode_row so the two never drift out of sync."""
    return _in_flight_episode_row(conn) is not None


def _global_triggers_today(conn) -> int:
    """Total demo-trigger episodes across ALL IPs today -- the real cap,
    unlike DAILY_CAP which is only per-IP and trivially bypassed by
    spreading requests across IPs/accounts."""
    return conn.execute(
        "SELECT COUNT(*) FROM demo_trigger_log WHERE date(triggered_at) = date('now')"
    ).fetchone()[0]


@app.get("/trigger/status")
def trigger_status(request: Request):
    """
    Public, no auth required -- feeds the frontend's 'X of Y daily
    triggers remaining' widget, which is shown to anonymous visitors too
    (it's informational, not an action). Also reports the caller's own
    per-IP cooldown remaining, since that's specific to whoever's asking.
    """
    conn = _conn()
    ip = request.client.host

    global_used = _global_triggers_today(conn)
    global_remaining = max(GLOBAL_DAILY_CAP - global_used, 0)

    cooldown_remaining_s = 0
    last = conn.execute(
        "SELECT triggered_at FROM demo_trigger_log WHERE ip = ? ORDER BY triggered_at DESC LIMIT 1",
        (ip,),
    ).fetchone()
    if last is not None:
        elapsed = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 86400.0", (last[0],)
        ).fetchone()[0]
        cooldown_remaining_s = max(COOLDOWN_S - elapsed, 0)

    in_flight_row = _in_flight_episode_row(conn)
    conn.close()

    # crash-loop warm-standby readiness (Model A, locked -- see
    # wardence_crash_loop_warm_standby_LOCKED_SPEC.md). Derived live
    # from the cluster on every poll, never persisted -- same reasoning
    # as everywhere else this state is checked (the Service selector +
    # a real readiness check ARE the rotation state, a stored flag
    # would just be a second source of truth that can drift from it).
    # Lets the frontend grey out/explain the crash-loop button proactively
    # instead of only finding out via a rejected click.
    crash_loop_active_label = carts_rotation.get_active_label()
    crash_loop_ready = (
        crash_loop_active_label == "carts" and carts_rotation.is_carts_ready()
    )

    return {
        "global_cap": GLOBAL_DAILY_CAP,
        "global_used_today": global_used,
        "global_remaining_today": global_remaining,
        "your_cooldown_remaining_s": round(cooldown_remaining_s),
        "episode_in_flight": in_flight_row is not None,
        # Real (2026-08-15): lets the frontend rehydrate activeEpisode
        # after a page refresh instead of just knowing SOMETHING is
        # blocking new triggers with no way to show what. Both null in
        # the rare legacy-fallback-with-no-fault_class path -- frontend
        # must handle that as "in flight but unknown," never crash on it.
        "in_flight_episode_id": in_flight_row[0] if in_flight_row else None,
        "in_flight_fault_class": in_flight_row[1] if in_flight_row else None,
        "crash_loop_ready": crash_loop_ready,
    }


# Whitelist for _set_episode_state's **extra columns (Kimi review 37
# finding 8) -- every current call site passes a hardcoded literal
# keyword (triggered_by=, subprocess_pid=), never anything derived from
# request input, so this isn't exploitable today. Enforced anyway: the
# column names get interpolated directly into the SQL SET clause below,
# and a whitelist is what keeps that permanently true rather than
# relying on every future caller remembering not to pass anything
# untrusted.
_SET_EPISODE_STATE_ALLOWED_EXTRA_KEYS = {"subprocess_pid", "triggered_by", "evidence_confirmed"}


def _set_episode_state(conn, episode_id: str, state: str, **extra) -> None:
    """Every state transition goes through this one function -- keeps
    state_entered_at updated on EVERY transition (load-bearing: Kimi
    review 36 finding 11 confirms reconciliation's Timer-remaining math
    depends on this being genuinely true), and gives every transition a
    single audit point instead of ad-hoc UPDATEs scattered per call
    site."""
    cols = ["episode_state", "state_entered_at"]
    vals = [state, datetime.datetime.now(datetime.timezone.utc).isoformat()]
    for k, v in extra.items():
        if k not in _SET_EPISODE_STATE_ALLOWED_EXTRA_KEYS:
            raise ValueError(f"_set_episode_state: '{k}' is not an allowed extra column")
        cols.append(k)
        vals.append(v)
    set_clause = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(f"UPDATE episodes SET {set_clause} WHERE episode_id = ?", (*vals, episode_id))
    conn.commit()


def _evidence_confirmed_now(fault_class: str, cfg: dict, baseline_restarts: int, baseline_periods: int) -> bool:
    """Single-poll evidence check for the 2 holding classes -- reuses
    injector.py's own real production checks directly (see the import
    block above), confirmed structurally safe for a single poll (not a
    consecutive-poll guard) by Kimi review 33: crash-loop's restartCount
    is a monotonic past-event latch, cpu-throttling's own injector-side
    verification already uses a raw instant-counter delta, not a
    windowed PromQL query, so neither can false-positive on a transient
    blip the way a windowed check could."""
    if fault_class == "crash-loop":
        target, namespace = cfg["target"], cfg["namespace"]
        return _restart_count(target, namespace) > baseline_restarts or _crash_loop_backoff_now(target, namespace)
    if fault_class == "cpu-throttling":
        current = _cfs_throttled_periods(cfg["target"], cfg["namespace"], cfg["container"])
        return current - baseline_periods >= CPU_THROTTLE_MIN_PERIODS_INCREASE
    return False


def _attempt_resolve(episode_id: str, triggered_by: str) -> bool:
    """Gate 2 (Kimi review 35/36's locked design): the ONE atomic CAS
    every trigger for moving awaiting_fix -> resolving goes through --
    a manual /trigger/resolve click, the 5-minute abandonment Timer
    firing, or (for report-only classes) the wrapper thread itself
    immediately after its own subprocess exits. Whichever call wins the
    UPDATE's rowcount, every other call silently no-ops -- no pairwise
    special-casing needed. Runs the scorer subprocess and the terminal
    transition; always called off the request thread (either already
    inside the background wrapper thread, or spawned into a fresh one
    by /trigger/resolve so a manual click never blocks its own HTTP
    response on the scorer's own up-to-500s runtime)."""
    conn = _conn()
    cur = conn.execute(
        "UPDATE episodes SET episode_state = 'resolving', state_entered_at = ? "
        "WHERE episode_id = ? AND episode_state = 'awaiting_fix'",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(), episode_id),
    )
    conn.commit()
    won = cur.rowcount > 0
    if not won:
        conn.close()
        return False

    # Real fix, Kimi review 37 finding 7: whichever caller actually wins
    # Gate 2's CAS is what should own cancelling the abandonment Timer,
    # since it's the only caller that can be sure the Timer's own future
    # fire would just be a wasted no-op (win == the Timer either already
    # fired and lost, or hasn't fired yet and now definitely will lose).
    # .pop with a default so this is a no-op if no Timer was ever
    # tracked for this episode (report-only classes never start one).
    timer = _ABANDON_TIMERS.pop(episode_id, None)
    if timer is not None:
        timer.cancel()

    t0_row = conn.execute(
        "SELECT t0, fault_class FROM episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    conn.close()
    t0 = datetime.datetime.fromisoformat(t0_row[0])
    settle_seconds = SETTLE_SECONDS_OVERRIDE.get(t0_row[1], SETTLE_SECONDS)
    # Real evidence-freezing timestamp -- the moment evidence was
    # genuinely ready (t0 + settle_seconds), computed here regardless of
    # how much real wall-clock time passes AFTER this point waiting for
    # a manual resolve click or the abandonment ceiling. This is the
    # actual fix for a genuine live-tested bug: a real crash-loop
    # episode's restart evidence aged out of agent.py's own [3m]
    # diagnosis query because the real elapsed time from injection to
    # diagnosis (holding's own duration + up to the full 300s ceiling)
    # can exceed 8 minutes -- the query was always evaluated against
    # live "now", however much later "now" turned out to be. Passed
    # through to p3_scorer.py --snapshot-at -> p3_agent.py /diagnose ->
    # every PromQL query in agent.py's query_prometheus, all of which
    # now evaluate against this fixed point instead of live "now".
    snapshot_at = (t0 + datetime.timedelta(seconds=settle_seconds)).isoformat()
    elapsed_s = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds()
    remaining_s = settle_seconds - elapsed_s
    if remaining_s > 0:
        time.sleep(remaining_s + RESOLVE_SAFETY_BUFFER_S)

    try:
        # --stream unconditional here: _attempt_resolve only ever runs
        # for a live-triggered episode (batch runs call p3_scorer.py
        # directly, never through this function) -- see this endpoint's
        # own real "reasoning stream" file convention in p3_agent.py's
        # REASONING_STREAM_DIR/_make_reasoning_event_writer.
        scorer_result = subprocess.run(
            [sys.executable, str(SCORER_PATH), "--episode-id", episode_id,
             "--snapshot-at", snapshot_at, "--stream"],
            cwd=str(SCORER_CWD),
            capture_output=True,
            text=True,
            timeout=SCORER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        conn = _conn()
        # p3_scorer.py stays agnostic to episode_state (Kimi review 35
        # finding 8, same "the row's own transitions are the wrapper's
        # job, not the tool it calls" split as injector.py) -- the
        # wrapper performs the terminal transition itself, here, after
        # the subprocess call returns/fails.
        _set_episode_state(conn, episode_id, "failed", triggered_by=triggered_by)
        conn.close()
        return True

    conn = _conn()
    if scorer_result.returncode != 0:
        _set_episode_state(conn, episode_id, "failed", triggered_by=triggered_by)
        conn.close()
        return True

    # Terminal transition + triggered_by set together (Kimi review 35
    # finding 8) -- the very next statement after the scorer subprocess
    # returns 0, with nothing else able to interleave on this episode_id
    # (Gate 2 already guarantees only one resolve is ever in flight).
    _set_episode_state(conn, episode_id, "resolved", triggered_by=triggered_by)
    conn.close()

    # Runs from inside this background thread, never the request thread
    # -- the ~12s real cost (measured, see wardence_frontend.md's
    # Operator saga) never blocks an HTTP response now, which is most of
    # what the earlier "fire-and-forget + completion-poll" design was
    # solving for. republished_at (Phase 2 item 2) is the real
    # completion signal a future frontend poll can wait on, closing the
    # gap this comment used to flag as unbuilt.
    if _republish_to_r2():
        conn = _conn()
        conn.execute(
            "UPDATE episodes SET republished_at = ? WHERE episode_id = ?",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(), episode_id),
        )
        conn.commit()
        conn.close()
    return True


def _run_live_episode(episode_id: str, fault_class: str, cfg: dict) -> None:
    """Real top-level exception guard (Kimi review 37 finding 2) around
    the actual logic in _run_live_episode_inner. Without this, ANY
    exception inside the background thread (a DB hiccup, a Prometheus
    blip during evidence polling, a disk-full write to the stop-file)
    kills the thread silently and leaves the row stuck in a
    non-terminal state -- which, since _episode_in_flight checks
    exactly that state set, would deadlock every future trigger until
    someone notices and does manual DB surgery."""
    try:
        _run_live_episode_inner(episode_id, fault_class, cfg)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"FATAL: _run_live_episode crashed for episode {episode_id}: {exc}")
        try:
            conn = _conn()
            _set_episode_state(conn, episode_id, "failed")
            conn.close()
        except Exception as inner_exc:  # noqa: BLE001
            print(f"  additionally failed to mark episode {episode_id} failed: {inner_exc}")


def _run_live_episode_inner(episode_id: str, fault_class: str, cfg: dict) -> None:
    """Everything from spawning injector.py through the terminal state
    transition -- off the request thread (Kimi review 36 findings 2/7:
    Popen, not subprocess.run, so the PID is real and evidence-polling
    folds into the same wait loop instead of a second thread)."""
    stop_file = _stop_file_path(episode_id)
    evidence_file = _evidence_file_path(episode_id)
    holding = fault_class in HOLDING_CLASSES
    wrapper_polled = fault_class in WRAPPER_POLLED_EVIDENCE_CLASSES
    evidence_file_class = fault_class in EVIDENCE_FILE_CLASSES
    cmd = [sys.executable, str(INJECTOR_PATH), "--class", fault_class, "--episode-id", episode_id]
    if fault_class in LIVE_TRIGGER_DURATION_OVERRIDE_S:
        cmd += ["--duration-override", str(LIVE_TRIGGER_DURATION_OVERRIDE_S[fault_class])]
    if holding:
        cmd += ["--stop-file", str(stop_file)]
    if evidence_file_class:
        cmd += ["--evidence-file", str(evidence_file)]

    baseline_restarts = baseline_periods = None
    if fault_class == "crash-loop":
        baseline_restarts = _restart_count(cfg["target"], cfg["namespace"])
    elif fault_class == "cpu-throttling":
        baseline_periods = _cfs_throttled_periods(cfg["target"], cfg["namespace"], cfg.get("container"))

    # A real per-episode LOG FILE, not PIPE and not DEVNULL (refined
    # same session after a real failed episode showed DEVNULL threw
    # away exactly the debugging info needed to understand why). PIPE
    # was the original bug (Kimi review 37 finding 1): nothing in this
    # loop reads the child's stdout/stderr, and once combined output
    # exceeds the OS pipe buffer (~64KiB typical), the child blocks on
    # write() forever, since the parent only calls proc.poll(), never
    # drains it. A file has no such buffer limit -- writing to disk
    # never blocks the child the way an unread pipe does -- so this
    # keeps the real fix (no deadlock risk) while restoring real
    # visibility into injector.py's own prints for live debugging.
    log_path = LIVE_TRIGGER_LOG_DIR / f"episode_{episode_id}.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(cmd, cwd=str(INJECTOR_CWD), stdout=log_file, stderr=subprocess.STDOUT)

    conn = _conn()
    if holding:
        # For these 2 classes, "injection lands" and "the extended hold
        # begins" are the same real-world moment -- the kill loop/
        # stressor starts immediately inside injector.py's own subprocess,
        # there's no separate confirmation step to wait for first.
        _set_episode_state(conn, episode_id, "holding", subprocess_pid=proc.pid)
    else:
        conn.execute("UPDATE episodes SET subprocess_pid = ? WHERE episode_id = ?", (proc.pid, episode_id))
        conn.commit()
    conn.close()

    inject_timeout_s = INJECT_SUBPROCESS_TIMEOUT_S.get(fault_class, DEFAULT_INJECT_SUBPROCESS_TIMEOUT_S)
    deadline = time.time() + inject_timeout_s
    timed_out = False
    while proc.poll() is None:
        if time.time() > deadline:
            # Graceful first (Kimi review 36 finding 3): a bare SIGKILL
            # never lets injector.py's own finally-block chaos-resource
            # cleanup run at all -- terminate() (SIGTERM) gives it the
            # chance; only escalate to kill() if it doesn't take the
            # hint. This is the real fix for the leaked-StressChaos risk
            # a plain subprocess.run(timeout=X) has today.
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            timed_out = True
            break
        if holding:
            conn = _conn()
            row = conn.execute(
                "SELECT evidence_confirmed, stop_hold_requested, abandon_requested FROM episodes "
                "WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            confirmed_already, stop_requested, abandon_flag = row
            if not confirmed_already:
                newly_confirmed = (
                    _evidence_confirmed_now(fault_class, cfg, baseline_restarts or 0, baseline_periods or 0)
                    if wrapper_polled
                    else evidence_file_class and evidence_file.exists()
                )
                if newly_confirmed:
                    conn.execute(
                        "UPDATE episodes SET evidence_confirmed = 1 WHERE episode_id = ?", (episode_id,)
                    )
                    conn.commit()
            # This tick loop is the SOLE writer of the stop-file (Kimi
            # review 36 finding 10, simplified from its dual-write
            # proposal): both a manual /trigger/stop-hold click and an
            # abandon signal (logout/the 5-min ceiling, though the
            # ceiling only ever fires post-hold in practice) just set a
            # DB flag; this loop is what converts either flag into the
            # real file injector.py is watching, on its own next ~10s
            # tick. Structurally cannot produce Kimi's "file exists but
            # DB flag wasn't written yet" crash race, since the file is
            # never written before the DB flag that causes it -- the
            # ordering only ever goes one direction. Trade-off accepted:
            # up to ~10s extra latency vs. writing the file synchronously
            # inside the endpoint handler, judged acceptable for a
            # "stop early" UX, not a real-time control signal.
            if (stop_requested or abandon_flag) and not stop_file.exists():
                stop_file.write_text(str(time.time()))
            conn.close()
        time.sleep(10)

    # stop_file left in place after the subprocess exits -- harmless
    # (episode_id-scoped filename, never reused), cheap to leave for
    # /tmp's own eventual cleanup rather than adding another failure
    # path here to remove it.
    log_file.close()
    print(f"episode {episode_id}: injector.py output saved to {log_path}")

    conn = _conn()
    row = conn.execute(
        "SELECT t0, chaos_resource_name, stop_hold_requested, evidence_confirmed FROM episodes WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    t0_written, chaos_name_written, stop_was_requested, was_evidence_confirmed = row
    # Defensive check (Kimi review 35/36 finding 1), NOT trusting
    # returncode alone even after injector.py's own sys.exit(1) fix --
    # this is what actually defends against a future injector.py
    # regression reintroducing the same bug, not just today's fix.
    succeeded = not timed_out and proc.returncode == 0 and t0_written is not None and chaos_name_written is not None

    if not succeeded:
        # Kimi review 37 finding 9 raised a real question: could an
        # early-stop (holding classes only) win the race against
        # injector.py's own post-hold verification, so the frontend
        # already unlocked "Diagnose & Fix" (evidence_confirmed=1) but
        # injector.py's fresh check somehow disagreed and exited
        # nonzero? Traced through, not assumed: both this wrapper's
        # evidence poll (_evidence_confirmed_now) and injector.py's own
        # verification read the EXACT SAME monotonic, non-resetting
        # counter (_restart_count for crash-loop, _cfs_throttled_periods
        # for cpu-throttling -- injector.py's own docstring confirms the
        # latter is "non-resetting"). Since this wrapper's check runs
        # strictly BEFORE injector.py's own later re-check of that same
        # counter, and the counter can only move forward, injector.py's
        # check cannot see LESS evidence than this wrapper already saw
        # -- the race Kimi described is structurally impossible against
        # this code, not just unlikely. Not silently patched over with a
        # forced-success override (which would be actively wrong here:
        # t0/chaos_resource_name are still NULL in exactly this failure
        # path, so forcing 'awaiting_fix' would crash _attempt_resolve's
        # datetime.fromisoformat(None) a few lines into scoring). Instead:
        # log loudly if the "impossible" case is ever actually observed,
        # so a future change that breaks the monotonic assumption is
        # caught immediately rather than silently mis-scoring episodes.
        if stop_was_requested and was_evidence_confirmed:
            print(
                f"WARNING: episode {episode_id} ({fault_class}) failed injector-side verification "
                f"despite evidence_confirmed=1 and an early stop having been requested -- this was "
                f"traced as structurally impossible given the monotonic counters both checks share; "
                f"if this fires for real, the monotonic assumption has been broken by a code change "
                f"and needs re-investigating, not just re-suppressing."
            )
        _set_episode_state(conn, episode_id, "failed")
        conn.close()
        return

    # Real bug, Kimi review 37 finding 5: this check used to live ONLY
    # inside the report-only branch below, so a user who abandoned mid-
    # HOLD (crash-loop/cpu-throttling -- the stop-file gets written for
    # abandon_requested exactly the same as for a manual early-stop
    # click, see the tick loop above) fell straight through to
    # awaiting_fix + the 5-minute Timer, silently getting scored once
    # the ceiling fired despite their explicit abandon. Checked once
    # here, uniformly, for every class -- covers report-only, holding,
    # and the other 4 auto-fix classes with one code path instead of
    # three.
    abandon_flag = conn.execute(
        "SELECT abandon_requested FROM episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()[0]
    if abandon_flag:
        _set_episode_state(conn, episode_id, "failed", triggered_by="abandoned")
        conn.close()
        return

    if fault_class not in AUTO_FIX_CLASSES:
        # A report-only class -- has no real fix action to dispatch.
        # Passes through awaiting_fix for ~0 real seconds by design (no
        # manual click exists for these at all, matching the locked
        # single "Trigger & Diagnose" button spec).
        _set_episode_state(conn, episode_id, "awaiting_fix")
        conn.close()
        _attempt_resolve(episode_id, triggered_by="auto_resolve")
        return

    _set_episode_state(conn, episode_id, "awaiting_fix")
    conn.close()

    # 5-minute abandonment ceiling (Kimi review 33/36) -- fires the same
    # Gate 2 CAS as a manual click; if the user already clicked (or a
    # different abandon path already won), this attempt just silently
    # loses the CAS, harmless. Tracked in _ABANDON_TIMERS (Kimi review
    # 37 finding 7) so a winning manual resolve can cancel it instead of
    # leaking a live Timer that fires uselessly 5 minutes later.
    timer = threading.Timer(ABANDON_CEILING_S, _attempt_resolve, args=(episode_id, "auto_resolve"))
    timer.daemon = True
    _ABANDON_TIMERS[episode_id] = timer
    timer.start()


@app.post("/trigger/inject")
def trigger_inject(
    fault_class: str,
    request: Request,
    payload: dict = Depends(require_role("admin", "demo-trigger")),
):
    """
    Real async redesign, Phase 1 item 5 (Kimi reviews 35/36) -- returns
    almost immediately (sub-second) with just the new episode_id and its
    starting state; every real cluster mutation (injector.py's
    subprocess, up to holding's extended hold, the eventual scorer call)
    happens in a background thread. The frontend polls
    GET /trigger/live-status for real progress instead of this call
    itself blocking for up to inject_timeout_s. All the same rate-
    limiting/safety checks the old synchronous /trigger/inject had still
    apply here, unchanged -- they gate injection, not resolution.
    """
    role = payload["role"]
    username = payload.get("username")
    conn = _conn()
    ip = request.client.host

    if fault_class not in IMPLEMENTED_CLASSES:
        _audit(conn, role, "/trigger/inject", f"rejected: '{fault_class}' not implemented", ip)
        conn.close()
        raise HTTPException(400, f"'{fault_class}' has no injector implementation yet")

    # crash-loop warm-standby gate (Model A, locked -- see
    # wardence_crash_loop_warm_standby_LOCKED_SPEC.md). Real, honest
    # server-side rejection BEFORE the injector subprocess ever spawns
    # -- injector.py's own _ensure_crash_loop_baseline is still the
    # authoritative, unconditional gate (this endpoint isn't the only
    # trigger path; run_batch_plan.py's subprocess calls don't go
    # through here at all), but checking here too gives a clean,
    # immediate HTTP error instead of letting the request succeed at
    # the API level and only fail deep inside a spawned subprocess a
    # moment later. Applies to EVERY role including admin -- this is a
    # correctness gate (carts genuinely isn't back to steady state
    # yet), not a fairness rule admin should be exempt from, same
    # reasoning as the concurrency guard below.
    if fault_class == "crash-loop":
        active_label = carts_rotation.get_active_label()
        if active_label != "carts" or not carts_rotation.is_carts_ready():
            _audit(conn, role, "/trigger/inject",
                   "rejected: carts still recovering from a prior episode", ip)
            conn.close()
            raise HTTPException(
                409, "carts is still recovering from a prior crash-loop episode -- try again shortly"
            )

    if role == "demo-trigger":
        if fault_class not in SAFE_DEMO_CLASSES:
            _audit(conn, role, "/trigger/inject", f"rejected: '{fault_class}' not in safe subset", ip)
            conn.close()
            raise HTTPException(403, f"demo-trigger may only trigger {SAFE_DEMO_CLASSES}")

        if _global_triggers_today(conn) >= GLOBAL_DAILY_CAP:
            _audit(conn, role, "/trigger/inject", "rejected: global daily cap reached", ip)
            conn.close()
            raise HTTPException(429, f"site-wide daily cap of {GLOBAL_DAILY_CAP} reached, try again tomorrow")

        last = conn.execute(
            "SELECT triggered_at FROM demo_trigger_log WHERE ip = ? ORDER BY triggered_at DESC LIMIT 1",
            (ip,),
        ).fetchone()
        if last is not None:
            elapsed = conn.execute(
                "SELECT (julianday('now') - julianday(?)) * 86400.0", (last[0],)
            ).fetchone()[0]
            if elapsed < COOLDOWN_S:
                _audit(conn, role, "/trigger/inject", "rejected: cooldown", ip)
                conn.close()
                raise HTTPException(429, f"cooldown active, wait {COOLDOWN_S - elapsed:.0f}s")

        today_count = conn.execute(
            "SELECT COUNT(*) FROM demo_trigger_log WHERE ip = ? AND date(triggered_at) = date('now')",
            (ip,),
        ).fetchone()[0]
        if today_count >= DAILY_CAP:
            _audit(conn, role, "/trigger/inject", "rejected: daily cap reached", ip)
            conn.close()
            raise HTTPException(429, f"daily cap of {DAILY_CAP} reached for this IP")

    # Real concurrency-safety guard, applies to EVERY role including
    # admin -- two genuinely concurrent injector.py runs against the
    # same cluster target is a correctness risk (races on pod
    # selection/baselining), not a fairness rule admin should bypass.
    # Narrowed to just this check-and-insert critical section (Kimi
    # review 36 finding 12) -- once the row below exists, episode_state
    # IS the real guard for everything after, not this lock.
    cfg = dict(FAULT_CONFIG[fault_class])
    with _TRIGGER_LOCK:
        if _episode_in_flight(conn):
            _audit(conn, role, "/trigger/inject", "rejected: episode already in flight", ip)
            conn.close()
            raise HTTPException(429, "an episode is already in flight, try again shortly")

        episode_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # t0/chaos_resource_name genuinely unknown yet -- NULL, not a
        # sentinel (Kimi review 35/36 finding 5). injector.py's own
        # UPSERT (record_episode, called via --episode-id) fills in the
        # real values once known.
        conn.execute(
            "INSERT INTO episodes "
            "(episode_id, fault_class, target, namespace, t0, chaos_resource_name, "
            "episode_state, state_entered_at, triggering_username) "
            "VALUES (?, ?, ?, ?, NULL, NULL, 'injecting', ?, ?)",
            (episode_id, fault_class, cfg["target"], cfg["namespace"], now, username),
        )
        conn.commit()

        if role == "demo-trigger":
            # Cooldown/cap bookkeeping happens on INJECT, not resolve --
            # "an episode was triggered" is the fairness-relevant event,
            # matching the old /trigger's behavior.
            conn.execute("INSERT INTO demo_trigger_log (ip) VALUES (?)", (ip,))
            conn.commit()

    _audit(conn, role, "/trigger/inject", f"fault_class={fault_class} episode_id={episode_id}", ip)
    conn.close()

    thread = threading.Thread(target=_run_live_episode, args=(episode_id, fault_class, cfg), daemon=True)
    thread.start()

    return {"status": "injecting", "episode_id": episode_id, "episode_state": "injecting"}


@app.get("/trigger/live-status")
def trigger_live_status(episode_id: str, payload: dict = Depends(require_role("admin", "demo-trigger"))):
    """
    New (Phase 1 item 5) -- what the frontend polls for real progress
    now that /trigger/inject and /trigger/resolve both return almost
    immediately. Reports exactly what's persisted, nothing inferred or
    guessed -- an absent live per-attempt progress field (e.g. "attempt
    2 of 3") is a deliberate, logged scope cut for this step, not an
    oversight: it needs injector.py to write a --progress-file
    (Kimi review 36 finding 9's recommended mechanism), not yet built.
    """
    conn = _conn()
    row = conn.execute(
        "SELECT episode_state, state_entered_at, evidence_confirmed, fault_class, t0, "
        "triggering_username, republished_at "
        "FROM episodes WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, f"no such episode '{episode_id}'")
    state, state_entered_at, evidence_confirmed, fault_class, t0, triggering_username, republished_at = row
    elapsed_s = None
    if state_entered_at is not None:
        entered = datetime.datetime.fromisoformat(state_entered_at)
        elapsed_s = round((datetime.datetime.now(datetime.timezone.utc) - entered).total_seconds())
    return {
        "episode_id": episode_id,
        "episode_state": state,
        "elapsed_in_state_s": elapsed_s,
        "evidence_confirmed": bool(evidence_confirmed),
        "fault_class": fault_class,
        "t0": t0,
        "can_stop_hold_early": state == "holding" and bool(evidence_confirmed),
        # Phase 2 item 2: null until the R2 snapshot genuinely reflects
        # this episode. episode_state=='resolved' alone is NOT enough --
        # the scorer finishing and the ~12s R2 publish finishing are two
        # separate real steps. Frontend should wait for this to be
        # non-null before re-fetching/navigating to Trust Ladder/Replay
        # Viewer's R2-sourced data for this episode.
        "republished_at": republished_at,
        # Real timer constants, added so the frontend never has to
        # hardcode/duplicate them (and risk drifting out of sync with
        # the real values enforced here) -- both are the exact same
        # constants this file itself already enforces, not new numbers.
        # `hold_duration_s` is null for the 4 auto-fix classes with no
        # real "fault-live" countdown concept: oom/disk-full self-heal
        # their active mechanism almost immediately (their real
        # signal is a STICKY k8s field, not a held-open window --
        # see wardence_frontend.md's "Full locked state across all 12
        # classes" table), and under-provisioned-replicas/bad-rollout
        # never self-revert at all (standing until fixed), so neither
        # has a real duration to count down from.
        "hold_duration_s": LIVE_TRIGGER_DURATION_OVERRIDE_S.get(fault_class),
        "abandon_ceiling_s": ABANDON_CEILING_S,
    }


@app.get("/trigger/reasoning-stream/{episode_id}")
def trigger_reasoning_stream(episode_id: str, token: str):
    """
    Real Server-Sent Events feed for the live Operator "Central Thinking
    Hub" widget -- tails the JSONL file p3_agent.py's /diagnose (called
    from inside _attempt_resolve's real scorer subprocess) writes real
    events into as its diagnosis genuinely happens: "provider_attempt"
    (a real chain-entry handoff), "turn_start", "reasoning_chunk" (real
    streamed model reasoning text, gemma/nemotron only -- see
    model_backend.py's STREAMING_CAPABLE_PROVIDERS), and a final "done".

    Auth via `?token=` query param, NOT the standard require_role()
    Authorization-header dependency every other endpoint uses -- real,
    deliberate exception: the browser's native EventSource API (the only
    real way to consume text/event-stream from JS without hand-rolling a
    fetch-based SSE reader) cannot set custom request headers at all, so
    a Bearer header is structurally unavailable here. Same real JWT
    decode/role check as require_role(), just reading the token from a
    different place -- not a weaker check, a differently-carried one.

    Real, not fabricated: every event here is written by the actual
    diagnosis call as it happens, not synthesized or replayed from a
    template. Stale note, corrected: the connection is now opened by the
    frontend right after inject (episodeId set), not when diagnosis
    starts -- see CentralThinkingHub.jsx's own comment for the real race
    this fixed (a polled episode_state could miss a fast "resolving"
    window entirely). This endpoint's own tail loop already handled a
    not-yet-existing file gracefully either way.

    No episode_state/ownership check against `episode_id` beyond the
    role gate -- same real trust boundary as every other admin/demo-
    trigger endpoint; a nonexistent or already-finished episode_id just
    yields an empty stream that self-closes at REASONING_STREAM_MAX_S,
    never a 404 (avoids a real race against the file not existing yet in
    the brief window between /trigger/inject returning and the scorer
    subprocess actually reaching /diagnose).
    """
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid token: {e}")
    if payload["role"] not in ("admin", "demo-trigger"):
        raise HTTPException(403, f"role '{payload['role']}' not permitted for this endpoint")

    path = REASONING_STREAM_DIR / f"reasoning_{episode_id}.jsonl"

    def _tail():
        start = time.monotonic()
        pos = 0
        saw_done = False
        while True:
            if path.exists():
                with path.open("r") as f:
                    f.seek(pos)
                    new_lines = f.readlines()
                    pos = f.tell()
                for line in new_lines:
                    line = line.strip()
                    if not line:
                        continue
                    yield f"data: {line}\n\n"
                    try:
                        if json.loads(line).get("type") == "done":
                            saw_done = True
                    except json.JSONDecodeError:
                        pass
            if saw_done or (time.monotonic() - start) > REASONING_STREAM_MAX_S:
                return
            time.sleep(REASONING_STREAM_POLL_S)

    return StreamingResponse(_tail(), media_type="text/event-stream")


@app.post("/trigger/stop-hold")
def trigger_stop_hold(episode_id: str, payload: dict = Depends(require_role("admin", "demo-trigger"))):
    """
    New (Phase 1 item 5, Kimi review 33's early-exit design wired for
    real) -- the user's "stop and diagnose now" click, valid only once
    the frontend's own live-status poll shows can_stop_hold_early=True.
    Only sets a DB flag; the running episode's own wrapper thread (see
    _run_live_episode's tick loop) is what actually converts this into
    the file injector.py's hold loop is watching, on its own next tick.
    """
    role = payload["role"]
    conn = _conn()
    row = conn.execute(
        "SELECT episode_state, evidence_confirmed, triggering_username FROM episodes WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, f"no such episode '{episode_id}'")
    state, evidence_confirmed, triggering_username = row
    if role == "demo-trigger" and triggering_username != payload.get("username"):
        conn.close()
        raise HTTPException(403, "you may only stop an episode you triggered yourself")
    if state != "holding" or not evidence_confirmed:
        conn.close()
        raise HTTPException(409, f"episode '{episode_id}' is not in an early-stoppable state right now")
    conn.execute("UPDATE episodes SET stop_hold_requested = 1 WHERE episode_id = ?", (episode_id,))
    conn.commit()
    conn.close()
    return {"status": "stop_requested", "episode_id": episode_id}


@app.post("/trigger/resolve")
def trigger_resolve(episode_id: str, payload: dict = Depends(require_role("admin", "demo-trigger"))):
    """
    Real async redesign, Phase 1 item 5 -- the manual "Diagnose & Fix"
    click. Attempts Gate 2 (the same atomic CAS the 5-minute ceiling and
    a report-only class's own auto-resolve also attempt) in a background
    thread and returns immediately; the frontend polls
    GET /trigger/live-status for the real terminal result (episode_state
    'resolved' or 'failed') instead of this call blocking for the
    scorer's own up-to-500s runtime.
    """
    role = payload["role"]
    conn = _conn()
    row = conn.execute("SELECT episode_state FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    if row is None:
        _audit(conn, role, "/trigger/resolve", f"rejected: no such episode '{episode_id}'", None)
        conn.close()
        raise HTTPException(404, f"no such episode '{episode_id}'")
    if row[0] != "awaiting_fix":
        _audit(conn, role, "/trigger/resolve", f"rejected: '{episode_id}' not awaiting_fix (state={row[0]})", None)
        conn.close()
        raise HTTPException(409, f"episode '{episode_id}' is not currently awaiting a manual resolve")
    _audit(conn, role, "/trigger/resolve", f"episode_id={episode_id}", None)
    conn.close()

    thread = threading.Thread(target=_attempt_resolve, args=(episode_id, "manual"), daemon=True)
    thread.start()
    return {"status": "resolving", "episode_id": episode_id}


@app.post("/logout")
def logout(payload: dict = Depends(require_role("admin", "demo-trigger"))):
    """
    New (Phase 1 item 5, Kimi review 36 finding 8) -- best-effort only,
    NEVER a hard dependency for correctness. sendBeacon/tab-close can't
    guarantee this ever fires (browser/OS can kill the tab before the
    request completes), so the 5-minute abandonment ceiling remains the
    one reliable auto-resolve mechanism regardless of whether this ever
    gets called. If the logging-out account happens to be the one that
    triggered the episode currently in flight, this sets abandon_requested
    so it can wind down sooner than the full ceiling -- pure optimization.
    """
    username = payload.get("username")
    conn = _conn()
    conn.execute(
        "UPDATE episodes SET abandon_requested = 1 "
        "WHERE triggering_username = ? AND episode_state IN ({})".format(
            ",".join("?" for _ in NON_TERMINAL_EPISODE_STATES)
        ),
        (username, *NON_TERMINAL_EPISODE_STATES),
    )
    conn.commit()
    conn.close()
    return {"status": "logged_out"}


def _wait_for_orphan(pid: int, episode_id: str) -> None:
    """Spawned by reconciliation for a still-alive orphaned subprocess
    (the API restarted but the real injector.py process is still
    running) -- a plain liveness poll, not a full re-run of
    _run_live_episode (can't reattach a Popen handle across a process
    restart, so this can't resume live evidence-polling for a
    still-holding class). Wrapped in its own try/except (Kimi review 37
    finding 6) -- without this, an exception here (e.g. the scorer
    subprocess called by _attempt_resolve crashing) kills this thread
    silently and leaves the episode stuck forever, since nothing else
    is watching it."""
    try:
        while True:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError, OSError):
                break
            time.sleep(10)
        conn = _conn()
        row = conn.execute(
            "SELECT t0, chaos_resource_name FROM episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row[0] is not None and row[1] is not None:
            _set_episode_state(conn, episode_id, "awaiting_fix")
            conn.close()
            threading.Thread(target=_attempt_resolve, args=(episode_id, "auto_resolve"), daemon=True).start()
        else:
            _set_episode_state(conn, episode_id, "failed")
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"WARNING: _wait_for_orphan failed for episode {episode_id}: {exc}")
        try:
            conn = _conn()
            _set_episode_state(conn, episode_id, "failed")
            conn.close()
        except Exception as inner_exc:  # noqa: BLE001
            print(f"  additionally failed to mark episode {episode_id} failed: {inner_exc}")


def _reconcile_one_row(episode_id: str, fault_class: str, state: str, state_entered_at: str, pid, t0, chaos_name) -> None:
    entered = datetime.datetime.fromisoformat(state_entered_at)
    age_s = (datetime.datetime.now(datetime.timezone.utc) - entered).total_seconds()

    if state in ("injecting", "holding"):
        if pid is None:
            # Real fix, Kimi review 37 finding 4: the original 60s age
            # grace period assumed reconciliation might run again soon
            # and re-check a young row -- it does NOT, reconciliation
            # runs exactly once at startup. A row younger than 60s at
            # that single check point was left NON-terminal with no
            # thread ever watching it again -- a permanent zombie,
            # deadlocking every future trigger via the in-flight guard,
            # on the very first crash that lands between the row INSERT
            # and Popen returning a PID. No PID + no live process to
            # check == an orphan, unconditionally, regardless of age.
            conn = _conn()
            _set_episode_state(conn, episode_id, "failed")
            conn.close()
            return

        alive = True
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            alive = False

        if alive:
            # The real injector.py subprocess is still genuinely running
            # even though the API itself restarted -- subprocess
            # lifetimes aren't tied to the parent Python process by
            # default.
            threading.Thread(target=_wait_for_orphan, args=(pid, episode_id), daemon=True).start()
        else:
            # Process already gone by the time we checked -- classify
            # immediately from the row's own real populated-ness (can't
            # retrieve the real exit code without the original Popen
            # handle, so this is the honest signal available
            # post-restart).
            conn = _conn()
            if t0 is not None and chaos_name is not None:
                _set_episode_state(conn, episode_id, "awaiting_fix")
                conn.close()
                threading.Thread(target=_attempt_resolve, args=(episode_id, "auto_resolve"), daemon=True).start()
            else:
                _set_episode_state(conn, episode_id, "failed")
                conn.close()

    elif state == "awaiting_fix":
        # Real fix, Kimi review 37 finding 3: calling _attempt_resolve
        # directly on the reconciliation (startup) call stack blocks
        # the FastAPI startup handler for SETTLE_SECONDS +
        # RESOLVE_SAFETY_BUFFER_S + up to SCORER_TIMEOUT_S (up to ~7
        # real minutes) before the app accepts its first request, if an
        # episode was genuinely abandoned right before a restart.
        # Backgrounded exactly like every other real trigger of this
        # function.
        remaining_s = ABANDON_CEILING_S - age_s
        if remaining_s <= 0:
            threading.Thread(target=_attempt_resolve, args=(episode_id, "auto_resolve"), daemon=True).start()
        else:
            timer = threading.Timer(remaining_s, _attempt_resolve, args=(episode_id, "auto_resolve"))
            timer.daemon = True
            _ABANDON_TIMERS[episode_id] = timer
            timer.start()

    elif state == "resolving":
        # The scorer subprocess was mid-flight when the API crashed --
        # no PID is tracked for it (only injector.py's own subprocess
        # gets one). Not safely re-runnable blind (a real fix action
        # may have already dispatched once); check whether a real
        # scores row landed before the crash.
        conn = _conn()
        scored = conn.execute("SELECT 1 FROM scores WHERE episode_id = ?", (episode_id,)).fetchone()
        if scored is not None:
            _set_episode_state(conn, episode_id, "resolved", triggered_by="auto_resolve")
        else:
            _set_episode_state(conn, episode_id, "failed", triggered_by="auto_resolve")
        conn.close()


def _reconcile_on_startup() -> None:
    """Real startup-reconciliation (Phase 1 item 10, Kimi review 36
    findings 6/11) -- runs once when the API process starts, before it
    accepts any real traffic. Finds any episode left in a non-terminal
    state by a crash (this project has hit exactly this shape of bug
    once already, the uvicorn-crash-left-port-8002-held incident) and
    resolves it one way or another rather than leaving it stuck forever,
    which -- given the single-episode-in-flight invariant -- would
    otherwise deadlock every future trigger.
    """
    conn = _conn()
    rows = conn.execute(
        "SELECT episode_id, fault_class, episode_state, state_entered_at, subprocess_pid, "
        "t0, chaos_resource_name FROM episodes WHERE episode_state IN ({})".format(
            ",".join("?" for _ in NON_TERMINAL_EPISODE_STATES)
        ),
        tuple(NON_TERMINAL_EPISODE_STATES),
    ).fetchall()
    conn.close()

    for episode_id, fault_class, state, state_entered_at, pid, t0, chaos_name in rows:
        # Real fix, Kimi review 37 finding 6: one bad row (a malformed
        # state_entered_at, a DB hiccup) used to be able to raise and
        # abort the whole loop, silently skipping reconciliation for
        # every row after it. Isolated per-row so one failure can't
        # starve the rest.
        try:
            _reconcile_one_row(episode_id, fault_class, state, state_entered_at, pid, t0, chaos_name)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
            print(f"WARNING: reconciliation failed for episode {episode_id}: {exc}")


@app.on_event("startup")
def _on_startup():
    _reconcile_on_startup()


@app.post("/promote")
def promote(fault_class: str, request: Request, payload: dict = Depends(require_role("admin"))):
    role = payload["role"]
    if fault_class not in PROMOTION_STREAK:
        raise HTTPException(400, f"'{fault_class}' has no promotion policy")
    conn = _conn()
    # Guard added 2026-07-24 (found during frontend testing): without this,
    # force-promoting an ALREADY can_act class silently overwrote its real,
    # earned streak with the fixed PROMOTION_STREAK[fault_class] floor (5) --
    # a real class (disk-full) has genuinely earned streaks past 5 (10, 11)
    # via real correct fixes, and a stray click would have fabricated the
    # published Trust Ladder number. This endpoint is meant only for
    # recovering a class after a KNOWN-BOGUS demotion (see oom's token-expiry
    # and disk-full's settle-wait incidents in wardence_buildlog.md), never
    # for touching a class that's already trusted.
    current = get_trust_state(conn, fault_class)
    if current["state"] == CAN_ACT:
        conn.close()
        raise HTTPException(400, f"'{fault_class}' is already can_act -- nothing to promote")
    manual_set_state(conn, fault_class, CAN_ACT, streak=PROMOTION_STREAK[fault_class])
    _audit(conn, role, "/promote", f"fault_class={fault_class}", request.client.host)
    conn.close()
    _republish_to_r2()
    return {"fault_class": fault_class, "state": CAN_ACT}


@app.post("/demote")
def demote(fault_class: str, request: Request, payload: dict = Depends(require_role("admin"))):
    role = payload["role"]
    conn = _conn()
    # Guard + state fix added 2026-07-24 (found during frontend testing):
    # (1) demoting an already report_only class had nothing real to revoke,
    # and (2) manual demotion previously wrote the literal state "demoted",
    # which the NATURAL scorer pipeline (trust_engine.record_outcome) never
    # produces -- a real automatic demotion lands on REPORT_ONLY instead
    # (see that function's docstring/logic). The two paths wrote different
    # state values for the same real-world meaning, so a manually-forced
    # demotion rendered a different Trust Ladder badge than a real one.
    # Fixed by aligning manual demotion onto the same REPORT_ONLY state the
    # real pipeline uses.
    current = get_trust_state(conn, fault_class)
    if current["state"] == REPORT_ONLY:
        conn.close()
        raise HTTPException(400, f"'{fault_class}' is already report_only -- nothing to demote")
    manual_set_state(conn, fault_class, REPORT_ONLY, streak=0)
    _audit(conn, role, "/demote", f"fault_class={fault_class}", request.client.host)
    conn.close()
    _republish_to_r2()
    return {"fault_class": fault_class, "state": REPORT_ONLY}


@app.post("/admin/reset-catalogue-baseline")
def reset_catalogue_baseline(request: Request, payload: dict = Depends(require_role("admin"))):
    """Manual, admin-only safety net for oom's/under-provisioned-replicas'
    shared catalogue baseline (memory limit + replica count), added
    2026-08-1x alongside Kimi review 34 finding #8's real fix. The
    PRIMARY mechanism is now p3_scorer.py's automatic reset at the end of
    every oom/under-provisioned-replicas episode's lifecycle -- this
    endpoint exists only for the rare case that automatic reset itself
    didn't run (e.g. the scorer process crashed mid-episode before
    reaching it), so the next real trigger of either class doesn't have
    to silently pay for it via its own injection timeout. Idempotent --
    a cheap kubectl-get no-op if catalogue is already at baseline on
    both dimensions."""
    role = payload["role"]
    _ensure_catalogue_replica_baseline(FAULT_CONFIG["under-provisioned-replicas"])
    _ensure_oom_baseline(FAULT_CONFIG["oom"])
    conn = _conn()
    _audit(conn, role, "/admin/reset-catalogue-baseline", "manual reset", request.client.host)
    conn.close()
    return {"status": "reset applied (idempotent no-op on any dimension already at baseline)"}


# --- Accounts (2026-07-22) -------------------------------------------------
# Real username/password login for BOTH demo-trigger and admin (unified,
# per user decision) -- admin requires a TOTP code too, since this login
# endpoint is reachable from the public-facing frontend, unlike the old
# mint_token.py flow which never touched the network. See
# wardence_frontend.md's Auth Model section for the full reasoning.


@app.post("/login")
def login(request: Request, body: dict = Body(...)):
    username = body.get("username")
    password = body.get("password")
    totp_code = body.get("totp_code")
    if not username or not password:
        raise HTTPException(400, "username and password are required")

    conn = _conn()
    ip = request.client.host
    result = accounts.verify_login(conn, username, password, ip=ip)

    if not result["ok"]:
        _audit(conn, "unauthenticated", "/login", f"failed: {result['reason']}", ip)
        conn.close()
        reason = result["reason"]
        if reason == "locked_out":
            raise HTTPException(429, "too many failed attempts, try again later")
        # Deliberately generic for the rest -- doesn't reveal whether the
        # username exists, is inactive, expired, or the password was wrong.
        raise HTTPException(401, "invalid credentials")

    account = result["account"]

    if account["role"] == "admin":
        if not totp_code or not accounts.verify_totp(account["totp_secret"], totp_code):
            _audit(conn, "unauthenticated", "/login", "failed: bad or missing TOTP code", ip)
            conn.close()
            raise HTTPException(401, "invalid credentials")

    remaining_hours = accounts.hours_until_expiry(account)
    session_hours = (
        DEFAULT_SESSION_HOURS if remaining_hours is None else min(DEFAULT_SESSION_HOURS, remaining_hours)
    )
    token = create_token(account["role"], expires_hours=session_hours, username=username)

    _audit(conn, account["role"], "/login", "success", ip)
    conn.close()
    return {"token": token, "role": account["role"], "username": username}


@app.post("/accounts")
def create_account_endpoint(
    request: Request,
    body: dict = Body(...),
    payload: dict = Depends(require_role("admin")),
):
    username = body.get("username")
    password = body.get("password")
    role = body.get("role", "demo-trigger")
    expires_hours = body.get("expires_hours")  # None = permanent

    if not username or not password:
        raise HTTPException(400, "username and password are required")
    if role not in accounts.ROLES:
        raise HTTPException(400, f"role must be one of {accounts.ROLES}")

    conn = _conn()
    if accounts.get_account(conn, username) is not None:
        conn.close()
        raise HTTPException(409, f"account '{username}' already exists")

    totp_secret = None
    provisioning_uri = None
    if role == "admin":
        totp_secret = accounts.generate_totp_secret()
        provisioning_uri = accounts.totp_provisioning_uri(username, totp_secret)

    accounts.create_account(
        conn, username, password, role, expires_hours=expires_hours, totp_secret=totp_secret
    )
    _audit(conn, payload["role"], "/accounts", f"created {username} ({role})", request.client.host)
    conn.close()

    response = {"username": username, "role": role, "expires_hours": expires_hours}
    if provisioning_uri:
        # Shown ONCE, same one-shot-reveal pattern as the R2 secret --
        # never re-derivable from the DB after this response.
        response["totp_provisioning_uri"] = provisioning_uri
        response["totp_secret"] = totp_secret
    return response


@app.get("/accounts")
def list_accounts_endpoint(payload: dict = Depends(require_role("admin"))):
    conn = _conn()
    result = accounts.list_accounts(conn)
    conn.close()
    return {"accounts": result}


@app.delete("/accounts/{username}")
def delete_account_endpoint(
    username: str, request: Request, payload: dict = Depends(require_role("admin"))
):
    conn = _conn()
    accounts.delete_account(conn, username)
    _audit(conn, payload["role"], "/accounts/delete", username, request.client.host)
    conn.close()
    return {"username": username, "deleted": True}


@app.post("/accounts/{username}/revoke")
def revoke_account_endpoint(
    username: str, request: Request, payload: dict = Depends(require_role("admin"))
):
    conn = _conn()
    accounts.set_active(conn, username, False)
    _audit(conn, payload["role"], "/accounts/revoke", username, request.client.host)
    conn.close()
    return {"username": username, "active": False}


@app.post("/accounts/{username}/reactivate")
def reactivate_account_endpoint(
    username: str, request: Request, payload: dict = Depends(require_role("admin"))
):
    conn = _conn()
    accounts.set_active(conn, username, True)
    _audit(conn, payload["role"], "/accounts/reactivate", username, request.client.host)
    conn.close()
    return {"username": username, "active": True}


@app.post("/accounts/{username}/password")
def change_password_endpoint(
    username: str,
    request: Request,
    body: dict = Body(...),
    payload: dict = Depends(require_role("admin")),
):
    # Per the locked design: admin can change any account's password
    # EXCEPT its own through this panel -- self-password-change isn't
    # part of this flow, admin manages its own credentials directly.
    if username == payload.get("username"):
        raise HTTPException(403, "cannot change your own password through this endpoint")

    new_password = body.get("new_password")
    if not new_password:
        raise HTTPException(400, "new_password is required")

    conn = _conn()
    if accounts.get_account(conn, username) is None:
        conn.close()
        raise HTTPException(404, f"no such account '{username}'")

    accounts.change_password(conn, username, new_password)
    _audit(conn, payload["role"], "/accounts/password", username, request.client.host)
    conn.close()
    return {"username": username, "password_changed": True}


# --- Live system-status (2026-07-22) ---------------------------------------
# The "before" state view for the logged-in operator dashboard -- current
# cluster health, independent of whether a fault is actively being
# triggered. Distinct from the Replay Viewer (historical) and the
# live-mode trigger view (one active episode). Gated to demo-trigger +
# admin per the locked frontend design (wardence_frontend.md) -- not
# public, since it's real cluster data.
#
# Metric names confirmed directly against a live Prometheus instance
# before writing this (2026-07-22), not assumed: k6_http_reqs_total
# (counter), k6_http_req_failed_rate (already a 0-1 ratio, not something
# needing a manual failed/total division), kube_pod_status_phase (same
# metric already used elsewhere in this project for pod health).


def _prom_query(query: str):
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]["result"]


@app.get("/system-status")
def system_status(payload: dict = Depends(require_role("admin", "demo-trigger"))):
    request_rate_result = _prom_query("sum(rate(k6_http_reqs_total[1m]))")
    request_rate = float(request_rate_result[0]["value"][1]) if request_rate_result else 0.0

    error_rate_result = _prom_query("avg(k6_http_req_failed_rate)")
    error_rate = float(error_rate_result[0]["value"][1]) if error_rate_result else 0.0

    pod_phase_result = _prom_query(
        f'kube_pod_status_phase{{namespace="{STATUS_NAMESPACE}"}} == 1'
    )
    pods_by_phase: dict[str, int] = {}
    for entry in pod_phase_result:
        phase = entry["metric"].get("phase", "Unknown")
        pods_by_phase[phase] = pods_by_phase.get(phase, 0) + 1

    return {
        "request_rate_per_s": round(request_rate, 3),
        "error_rate": round(error_rate, 4),
        "pods_by_phase": pods_by_phase,
    }


# Live-status readout for the classes with no other Operator-screen
# visibility. Security spec locked via Kimi review 34 finding #9 +
# review 38 (fixed 2 real errors in the first draft: raw Prometheus
# label leakage, and a stale-SAFE_DEMO_CLASSES role-gating premise --
# all of these classes are in SAFE_DEMO_CLASSES as of the 2026-08-1x
# reopen, so they get the same demo-trigger+admin gate as every other
# class's trigger permission, not a split).
#
# init-failure and bad-rollout REMOVED 2026-08-1x -- both were masked
# by the same "old healthy pod keeps serving" RollingUpdate default and
# both were fixed the same real way (rollout-strategy patch forcing the
# old pod down first) -- see faultClasses.js's INVISIBLE_CLASSES
# comment for the full real fix detail, not duplicated here.
#
# Every query below is a hardcoded template, verbatim from agent.py's
# own real diagnosis path for these classes -- target/namespace/pod
# regex all come from FAULT_CONFIG, never from the request. Always
# called WITHOUT snapshot_at (live "now" only, unlike the diagnosis
# path's evidence-freezing mechanism) -- this is a live glance, not a
# scored diagnosis.
_LIVE_STATUS_CLASSES = {"disk-full", "memory-leak"}


def _prom_query_safe(query: str) -> list:
    """Same shape as _prom_query, but never lets a Prometheus failure
    propagate as an uncaught 500 -- Kimi review 38 finding 2. Raises
    HTTPException(503) instead, which the caller can catch per-query
    so one flaky sub-query doesn't fail the whole readout."""
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
        resp.raise_for_status()
        return resp.json()["data"]["result"]
    except requests.RequestException:
        raise HTTPException(503, "prometheus unavailable")


def _disk_full_live_status(namespace: str, target: str) -> dict:
    pod_re = f"{target}-[^-]+-[^-]+$"
    evicted_query = f'kube_pod_status_reason{{namespace="{namespace}", pod=~"{pod_re}", reason="Evicted"}} == 1'
    fresh_query = (
        f'(kube_pod_status_phase{{namespace="{namespace}", pod=~"{pod_re}", phase="Running"}} == 1) '
        f'and on(namespace, pod) ((time() - kube_pod_created{{namespace="{namespace}", pod=~"{pod_re}"}}) < 180)'
    )
    # Run concurrently -- Kimi review 38 finding 5B, sequential 2x10s
    # timeouts would double real worst-case request latency.
    with ThreadPoolExecutor(max_workers=2) as pool:
        evicted_future = pool.submit(_prom_query_safe, evicted_query)
        fresh_future = pool.submit(_prom_query_safe, fresh_query)
        evicted_result = None
        fresh_result = None
        evicted_error = None
        fresh_error = None
        try:
            evicted_result = evicted_future.result()
        except HTTPException as exc:
            evicted_error = exc.detail
        try:
            fresh_result = fresh_future.result()
        except HTTPException as exc:
            fresh_error = exc.detail

    evicted_present = None if evicted_error else len(evicted_result) > 0
    fresh_present = None if fresh_error else len(fresh_result) > 0

    if evicted_present is None or fresh_present is None:
        indication = "unavailable"
    elif evicted_present and fresh_present:
        indication = "evicted_replacement_starting"
    elif evicted_present and not fresh_present:
        indication = "evicted_awaiting_replacement"
    else:
        indication = "no_eviction_detected"

    return {
        "evicted_pod_present": evicted_present,
        "fresh_replacement_present": fresh_present,
        "indication": indication,
        "warning": evicted_error or fresh_error,
    }


def _init_failure_live_status(namespace: str, target: str) -> dict:
    query = (
        f'max_over_time(kube_pod_status_ready{{namespace="{namespace}", pod=~"{target}-[^-]+-[^-]+$", '
        f'condition="false"}}[2m]) == 1'
    )
    try:
        result = _prom_query_safe(query)
    except HTTPException as exc:
        return {"ready_false_present": None, "warning": exc.detail}
    return {"ready_false_present": len(result) > 0, "warning": None}


def _memory_leak_live_status(namespace: str, target: str) -> dict:
    """Real production replacement (2026-08-21 session), REPLACES the old
    container_memory_working_set_bytes reading -- that signal belonged to
    the now-removed StressChaos mechanism and has no real relationship to
    the JVM-attach LeakAgent's own retained heap. `heap_used` is
    shipping's own real, scraped JVM heap metric (KB, same signal
    agent.py's real diagnosis query reads).

    Deliberately reports the RAW current/recent-peak reading, NOT a
    rise-over-baseline like the real scored diagnosis does -- this is a
    live "now" glance with no episode context (no snapshot_at, no
    per-episode baseline_heap_kb the way /diagnose has), so there is no
    real baseline available here to compare against. Fabricating one
    would misrepresent this as more diagnostic than it honestly is;
    every other class's own live-status function here (disk-full/
    init-failure/bad-rollout) reports similarly raw, undiagnosed
    readings, not a full verdict."""
    query = f'max_over_time(heap_used{{namespace="{namespace}", pod=~"{target}-[^-]+-[^-]+$"}}[2m])'
    try:
        result = _prom_query_safe(query)
    except HTTPException as exc:
        return {"heap_used_mib": None, "warning": exc.detail}
    if not result:
        return {"heap_used_mib": None, "warning": None}
    value_mib = round(float(result[0]["value"][1]) / 1024, 1)  # heap_used is KB, not bytes
    return {"heap_used_mib": value_mib, "warning": None}


def _bad_rollout_live_status(namespace: str, target: str) -> dict:
    """Real (2026-08-15): bad-rollout was found to be storefront-invisible
    too, not just disk-full/init-failure/memory-leak -- front-end's own
    rolling-update strategy (real, confirmed maxSurge=25%/maxUnavailable=25%,
    which for a single replica means create-new-before-removing-old) plus
    its existing readinessProbe means the OLD healthy pod keeps serving
    100% of real traffic the entire time the NEW (broken-image) pod sits
    unable to start -- injector.py's own _front_end_image_pull_failing
    docstring already called this "the same old-pod-stays-healthy pattern
    as init-failure," just never reflected in this readout or
    INVISIBLE_CLASSES until now. Mirrors that exact same real Prometheus
    signal, not a new detection mechanism."""
    query = (
        f'kube_pod_container_status_waiting_reason{{namespace="{namespace}", '
        f'pod=~"{target}.*", reason=~"ImagePullBackOff|ErrImagePull"}} == 1'
    )
    try:
        result = _prom_query_safe(query)
    except HTTPException as exc:
        return {"image_pull_failing": None, "warning": exc.detail}
    return {"image_pull_failing": len(result) > 0, "warning": None}


@app.get("/operator/fault-status/{fault_class}")
def operator_fault_status(
    fault_class: str, payload: dict = Depends(require_role("admin", "demo-trigger"))
):
    """Sanitized, read-only live-status readout for the 3 classes with
    no other Operator-screen visibility. Named 'fault-status', not
    'live-status', to avoid any semantic collision with the
    episode-scoped /trigger/live-status above (Kimi review 38 finding
    5C). Every field returned is server-computed (bool/number/string
    only) -- the raw Prometheus response body is never passed through,
    per Kimi review 38's real finding that these metrics' label sets
    (pod, uid, id/container-runtime-id) would otherwise leak."""
    if fault_class not in _LIVE_STATUS_CLASSES:
        raise HTTPException(
            404, f"live-status not available for '{fault_class}' (only {sorted(_LIVE_STATUS_CLASSES)})"
        )
    config = FAULT_CONFIG[fault_class]
    namespace = config["namespace"]
    target = config["target"]

    if fault_class == "disk-full":
        status = _disk_full_live_status(namespace, target)
    elif fault_class == "init-failure":
        status = _init_failure_live_status(namespace, target)
    elif fault_class == "bad-rollout":
        status = _bad_rollout_live_status(namespace, target)
    else:
        status = _memory_leak_live_status(namespace, target)

    return {"fault_class": fault_class, **status}
