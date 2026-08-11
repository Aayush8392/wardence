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
import subprocess
import sys
import threading
import time
from pathlib import Path

import jwt
import requests
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

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
from injector import (  # noqa: E402
    FAULT_CONFIG,
    _ensure_catalogue_replica_baseline,
    _ensure_oom_baseline,
)


def _republish_to_r2() -> None:
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
    """
    try:
        publish_to_r2.main()
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"WARNING: R2 republish after manual override failed: {e}")

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
# The 6 auto-fix classes -- all ops-level, RBAC-caged, reversible actions,
# a consistent blast-radius bound. Report-only classes stay admin-only:
# several involve real resource pressure (e.g. connection-pool-exhaustion's
# DB flood) with no bounded auto-fix to clean up after, unlike the auto-fix
# set. Decided 2026-08-06, expanding the original 2026-07-24 3-class set
# (crash-loop/oom/disk-full) to the full auto-fix roster.
SAFE_DEMO_CLASSES = {
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
    "memory-leak": 260,                 # real: 181.2s
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
    "cpu-throttling": 350,        # real-safety-tested to 300s (2026-08-01), injector cost not separately measured
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
# restarts/OOM/eviction/memory-leak/connection-pool, [2m] for
# network-latency. Wait too long past injection and the agent's own
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
RESOLVE_WINDOW_MAX_S = 180

# Real concurrency guard, shared across BOTH /trigger/inject and
# /trigger/resolve (2026-07-24, found during checklist review before the
# two-phase flow was ever tested). The DB-backed checks each endpoint
# already has (_episode_in_flight for inject, the "already scored" query
# for resolve) both have the same blind spot: they can only see a row
# that's already been WRITTEN. Neither can see work that's currently
# running but hasn't produced a row yet --
#   - inject: no episodes row exists until injector.py's subprocess
#     finishes, so a fast double-click (or two different classes clicked
#     back-to-back) can start TWO concurrent injector.py runs against the
#     cluster before either check would catch it.
#   - resolve: no scores row exists until p3_scorer.py's subprocess
#     finishes, so a fast double-click can run the scorer twice,
#     concurrently, against the same episode -- for an auto-fix class
#     that means the real fix action could genuinely fire twice and
#     trust_engine.record_outcome could double-count one real outcome
#     into the streak.
# One shared flag (not two separate per-phase ones) because the real
# invariant is "only one episode in flight, in ANY phase, system-wide" --
# the same invariant _episode_in_flight already enforces for the window
# AFTER a row exists. Checked-and-set atomically under the lock so two
# near-simultaneous requests can't both pass the check before either
# marks itself busy.
_TRIGGER_BUSY: dict | None = None  # {"phase": "injecting" | "resolving", "detail": str} or None while idle
_TRIGGER_LOCK = threading.Lock()


def _try_acquire_trigger_busy(phase: str, detail: str) -> bool:
    global _TRIGGER_BUSY
    with _TRIGGER_LOCK:
        if _TRIGGER_BUSY is not None:
            return False
        _TRIGGER_BUSY = {"phase": phase, "detail": detail}
        return True


def _release_trigger_busy() -> None:
    global _TRIGGER_BUSY
    with _TRIGGER_LOCK:
        _TRIGGER_BUSY = None

# p3_scorer.py's own agent request timeout is already 180s (durability
# windows run up to 3 min for oom -- see p3_scorer.py's docstring); give
# the subprocess itself real margin beyond that, not a tight guess.
SCORER_TIMEOUT_S = 400


def _conn():
    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
    accounts.ensure_accounts_tables(conn)
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


# Mirrors p3_scorer.py's MAX_EPISODE_AGE_MINUTES -- found the SAME class
# of bug here (2026-07-22) that p3_scorer.py already had to fix once
# before: this query originally had no staleness bound at all, so a
# genuinely stale, long-abandoned unscored episode (e.g. a leftover from
# an earlier manual test) would report "in flight" forever, permanently
# blocking demo-trigger with a 429 even though nothing was actually
# running. Anything older than this is treated as abandoned, not in
# flight -- same reasoning as p3_scorer.py's own fix.
#
# Lowered from 10 to 4 minutes (2026-07-24, real bug found during Phase B
# testing): the ORIGINAL 10-minute number was never grounded in real
# diagnosis behavior -- see RESOLVE_WINDOW_MAX_S below, which is the real,
# newly-added hard limit on how long a fault stays genuinely diagnosable
# (~3 minutes, derived from agent.py's actual PromQL lookback windows).
# Leaving this at 10 would have created a real dead zone: an episode
# correctly refused by /trigger/resolve as "too old to score" would still
# report episode_in_flight=True for up to 6 more minutes, blocking a fresh
# inject even after the system had already told the user the old one was
# a lost cause. 4 minutes gives a small buffer above RESOLVE_WINDOW_MAX_S
# (180s) without reintroducing that gap.
EPISODE_IN_FLIGHT_MAX_AGE_MINUTES = 4


def _episode_in_flight(conn) -> bool:
    row = conn.execute(
        """
        SELECT e.episode_id, e.t0 FROM episodes e
        LEFT JOIN scores s ON e.episode_id = s.episode_id
        WHERE s.episode_id IS NULL
        ORDER BY e.t0 DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return False
    _, t0_str = row
    t0 = datetime.datetime.fromisoformat(t0_str)
    age_minutes = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds() / 60
    return age_minutes <= EPISODE_IN_FLIGHT_MAX_AGE_MINUTES


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

    in_flight = _episode_in_flight(conn)
    conn.close()
    return {
        "global_cap": GLOBAL_DAILY_CAP,
        "global_used_today": global_used,
        "global_remaining_today": global_remaining,
        "your_cooldown_remaining_s": round(cooldown_remaining_s),
        "episode_in_flight": in_flight,
    }


@app.post("/trigger/inject")
def trigger_inject(
    fault_class: str,
    request: Request,
    payload: dict = Depends(require_role("admin", "demo-trigger")),
):
    """
    Phase 1 of the two-phase trigger flow (2026-07-24, superseding the old
    single-call /trigger): injects only, returns immediately with the real
    episode_id + t0. Does NOT settle-wait, diagnose, act, or score -- the
    caller decides when to move to /trigger/resolve, after visually
    confirming on the frontend that the fault actually landed. All the
    same rate-limiting/safety checks the old /trigger had still apply here,
    unchanged -- they gate INJECTION, not resolution.
    """
    role = payload["role"]
    conn = _conn()
    ip = request.client.host

    if fault_class not in IMPLEMENTED_CLASSES:
        _audit(conn, role, "/trigger/inject", f"rejected: '{fault_class}' not implemented", ip)
        conn.close()
        raise HTTPException(400, f"'{fault_class}' has no injector implementation yet")

    # Real concurrency-safety guard, not a fairness/budget one -- applies to
    # EVERY role, including admin. Two genuinely concurrent injector.py runs
    # against the same cluster target is a correctness risk (races on pod
    # selection/baselining, the same class of bug this project already hit
    # repeatedly with disk-full's settle-wait timing), not something an
    # admin should be able to bypass just because the cooldown/cap fairness
    # rules below don't apply to them.
    if _episode_in_flight(conn):
        _audit(conn, role, "/trigger/inject", "rejected: episode already in flight", ip)
        conn.close()
        raise HTTPException(429, "an episode is already in flight, try again shortly")

    # Closes the real gap _episode_in_flight can't see -- see _TRIGGER_BUSY's
    # module-level comment. Deliberately checked BEFORE the cooldown/cap
    # bookkeeping below, so a busy-rejection never costs a demo-trigger
    # caller their cooldown/daily-cap allowance for a request that never
    # actually ran.
    if not _try_acquire_trigger_busy("injecting", fault_class):
        _audit(conn, role, "/trigger/inject", "rejected: another trigger call in progress", ip)
        conn.close()
        raise HTTPException(429, "an episode is already in flight, try again shortly")

    # Everything from here on has already acquired _TRIGGER_BUSY -- every
    # exit path (a rejection below, injector failure, or success) MUST
    # release it, or one rejected/failed call would wedge every future
    # trigger behind a busy flag nothing will ever clear.
    try:
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

            # Cooldown/cap bookkeeping happens on INJECT, not resolve --
            # "an episode was triggered" is the fairness-relevant event,
            # matching the old /trigger's behavior.
            conn.execute("INSERT INTO demo_trigger_log (ip) VALUES (?)", (ip,))
            conn.commit()

        _audit(conn, role, "/trigger/inject", f"fault_class={fault_class}", ip)
        conn.close()

        inject_timeout_s = INJECT_SUBPROCESS_TIMEOUT_S.get(fault_class, DEFAULT_INJECT_SUBPROCESS_TIMEOUT_S)
        try:
            result = subprocess.run(
                [sys.executable, str(INJECTOR_PATH), "--class", fault_class],
                cwd=str(INJECTOR_CWD),
                capture_output=True,
                text=True,
                timeout=inject_timeout_s,
            )
        except subprocess.TimeoutExpired:
            # Real bug, logged 2026-08-03: an uncaught TimeoutExpired used to
            # surface as a bare unhandled 500 and could kill injector.py
            # mid-attempt, risking leaked flood/stressor state its own
            # `finally`-block cleanup never got to run (the exact class of
            # bug fault-injection-cleanup-discipline exists to catch). Still
            # not a clean recovery -- a genuinely stuck injector process is a
            # real infra problem -- but this at least gives the caller an
            # honest, specific error instead of a crash.
            raise HTTPException(
                504,
                f"injector for '{fault_class}' did not finish within "
                f"{inject_timeout_s}s -- likely a genuinely stuck "
                f"cluster/injector process, not a normal retry. Check the "
                f"cluster directly before retrying.",
            )
        if result.returncode != 0:
            raise HTTPException(500, f"injector failed: {result.stderr}")

        # injector.py writes ground truth straight to SQLite -- read the
        # real episode_id + t0 back from there rather than scraping
        # stdout text.
        conn = _conn()
        row = conn.execute(
            "SELECT episode_id, t0 FROM episodes WHERE fault_class = ? ORDER BY t0 DESC LIMIT 1",
            (fault_class,),
        ).fetchone()
        conn.close()
        if row is None:
            raise HTTPException(500, "injector reported success but no episode was recorded")

        return {"status": "injected", "episode_id": row[0], "t0": row[1]}
    finally:
        # Deliberately NOT released here on the success path alone -- an
        # unscored episode row now exists, so _episode_in_flight takes
        # over as the guard for the "awaiting fix" window that follows.
        # This flag only needs to cover the sub-window where NO row
        # exists yet, which ends the instant this function returns
        # (success) or raises (every rejection/failure path above).
        _release_trigger_busy()


@app.post("/trigger/resolve")
def trigger_resolve(
    episode_id: str,
    request: Request,
    payload: dict = Depends(require_role("admin", "demo-trigger")),
):
    """
    Phase 2 of the two-phase trigger flow (2026-07-24): the user clicked
    "Diagnose & Fix" for a real, already-injected episode. Enforces the
    same SETTLE_SECONDS floor the old atomic /trigger always waited out --
    if the user resolves fast, this silently sleeps out whatever's left
    (+RESOLVE_SAFETY_BUFFER_S) before actually diagnosing; if they waited
    long enough on their own, it proceeds immediately. Deliberately does
    NOT expose which of those two cases happened -- see
    wardence_frontend.md's "Two-Phase Trigger Flow" section for why the
    settle-wait must stay invisible to the end user.
    """
    role = payload["role"]
    conn = _conn()
    ip = request.client.host

    row = conn.execute(
        "SELECT t0 FROM episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        _audit(conn, role, "/trigger/resolve", f"rejected: no such episode '{episode_id}'", ip)
        conn.close()
        raise HTTPException(404, f"no such episode '{episode_id}'")

    already_scored = conn.execute(
        "SELECT 1 FROM scores WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if already_scored is not None:
        _audit(conn, role, "/trigger/resolve", f"rejected: '{episode_id}' already scored", ip)
        conn.close()
        raise HTTPException(409, f"episode '{episode_id}' was already resolved")

    # Hard block, checked BEFORE acquiring the busy flag or sleeping --
    # see RESOLVE_WINDOW_MAX_S's module-level comment. This episode will
    # NEVER be scored past this point; deliberately not attempted at all,
    # rather than run the scorer and let it silently fail/return nulls.
    elapsed_s = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(row[0])
    ).total_seconds()
    if elapsed_s > RESOLVE_WINDOW_MAX_S:
        _audit(conn, role, "/trigger/resolve", f"rejected: '{episode_id}' window expired ({elapsed_s:.0f}s)", ip)
        conn.close()
        raise HTTPException(
            410,
            f"too much time has passed since this fault was injected ({elapsed_s:.0f}s, "
            f"limit {RESOLVE_WINDOW_MAX_S}s) -- its evidence window has likely expired and "
            f"it will not be scored. Inject a fresh fault instead.",
        )

    # Closes the gap the DB-backed already_scored check above can't see --
    # see _TRIGGER_BUSY's module-level comment. Shares the same flag
    # /trigger/inject uses, since the real invariant is one episode in
    # flight system-wide, in ANY phase, not a per-endpoint rule.
    if not _try_acquire_trigger_busy("resolving", episode_id):
        _audit(conn, role, "/trigger/resolve", f"rejected: '{episode_id}' resolve already in progress", ip)
        conn.close()
        raise HTTPException(409, f"diagnosis already in progress for episode '{episode_id}'")

    _audit(conn, role, "/trigger/resolve", f"episode_id={episode_id}", ip)
    conn.close()

    try:
        t0 = datetime.datetime.fromisoformat(row[0])
        elapsed_s = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds()
        remaining_s = SETTLE_SECONDS - elapsed_s
        if remaining_s > 0:
            time.sleep(remaining_s + RESOLVE_SAFETY_BUFFER_S)

        # p3_agent.py (the real agent, port 8001) must already be running
        # for this to succeed -- p3_scorer.py calls it directly.
        # --episode-id (2026-07-24): tells the scorer exactly which
        # episode to score, instead of letting it guess "most recent
        # unscored" -- see get_episode_by_id's docstring in p3_scorer.py
        # for why guessing is a real correctness risk, not just untidy.
        scorer_result = subprocess.run(
            [sys.executable, str(SCORER_PATH), "--episode-id", episode_id],
            cwd=str(SCORER_CWD),
            capture_output=True,
            text=True,
            timeout=SCORER_TIMEOUT_S,
        )
        if scorer_result.returncode != 0:
            # The episode itself is real and already recorded -- surface
            # the scorer failure but don't pretend resolution failed
            # outright.
            return {
                "status": "triggered_but_unscored",
                "episode_id": episode_id,
                "scorer_error": scorer_result.stderr,
            }
    finally:
        # Always release, even on a scorer failure/timeout/exception --
        # otherwise one failed resolve would permanently wedge every
        # future trigger call behind a busy flag nothing will ever clear.
        _release_trigger_busy()

    conn = _conn()
    # target + confidence: target comes from the episodes table (the
    # injector's own record of what it hit), confidence from scores (the
    # diagnoser's self-reported confidence, same field Calibration uses).
    score_row = conn.execute(
        "SELECT s.predicted_class, s.correct, s.action_taken, s.action_applied, s.durability_verdict, "
        "s.confidence, e.target "
        "FROM scores s JOIN episodes e ON e.episode_id = s.episode_id "
        "WHERE s.episode_id = ?",
        (episode_id,),
    ).fetchone()
    conn.close()

    # Refresh the R2 snapshot so "VIEW FULL REPLAY" (which reads episodes.json
    # from R2, not the live DB) actually finds this episode right away --
    # same staleness gap already fixed for /promote and /demote.
    _republish_to_r2()

    return {
        "status": "scored",
        "episode_id": episode_id,
        "predicted_class": score_row[0] if score_row else None,
        "correct": bool(score_row[1]) if score_row else None,
        "action_taken": score_row[2] if score_row else None,
        "action_applied": bool(score_row[3]) if score_row and score_row[3] is not None else None,
        "durability_verdict": score_row[4] if score_row else None,
        "confidence": score_row[5] if score_row else None,
        "target": score_row[6] if score_row else None,
    }


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
