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
import time
from pathlib import Path

import jwt
import requests
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).parent))

import sqlite3  # noqa: E402

import accounts  # noqa: E402
from auth import create_token, decode_token  # noqa: E402
from trust_engine import (  # noqa: E402
    CAN_ACT,
    DB_PATH,
    DEMOTED,
    PROMOTION_STREAK,
    ensure_trust_tables,
    get_trust_state,
    manual_set_state,
)

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

IMPLEMENTED_CLASSES = {"crash-loop"}  # injector.py only knows how to inject this so far
SAFE_DEMO_CLASSES = {"crash-loop"}  # curated subset, per wardence_context.md
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
EPISODE_IN_FLIGHT_MAX_AGE_MINUTES = 10


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


@app.post("/trigger")
def trigger(
    fault_class: str,
    request: Request,
    payload: dict = Depends(require_role("admin", "demo-trigger")),
):
    role = payload["role"]
    conn = _conn()
    ip = request.client.host

    if fault_class not in IMPLEMENTED_CLASSES:
        _audit(conn, role, "/trigger", f"rejected: '{fault_class}' not implemented", ip)
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
        _audit(conn, role, "/trigger", "rejected: episode already in flight", ip)
        conn.close()
        raise HTTPException(429, "an episode is already in flight, try again shortly")

    if role == "demo-trigger":
        if fault_class not in SAFE_DEMO_CLASSES:
            _audit(conn, role, "/trigger", f"rejected: '{fault_class}' not in safe subset", ip)
            conn.close()
            raise HTTPException(403, f"demo-trigger may only trigger {SAFE_DEMO_CLASSES}")

        if _global_triggers_today(conn) >= GLOBAL_DAILY_CAP:
            _audit(conn, role, "/trigger", "rejected: global daily cap reached", ip)
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
                _audit(conn, role, "/trigger", "rejected: cooldown", ip)
                conn.close()
                raise HTTPException(429, f"cooldown active, wait {COOLDOWN_S - elapsed:.0f}s")

        today_count = conn.execute(
            "SELECT COUNT(*) FROM demo_trigger_log WHERE ip = ? AND date(triggered_at) = date('now')",
            (ip,),
        ).fetchone()[0]
        if today_count >= DAILY_CAP:
            _audit(conn, role, "/trigger", "rejected: daily cap reached", ip)
            conn.close()
            raise HTTPException(429, f"daily cap of {DAILY_CAP} reached for this IP")

        conn.execute("INSERT INTO demo_trigger_log (ip) VALUES (?)", (ip,))
        conn.commit()

    _audit(conn, role, "/trigger", f"fault_class={fault_class}", ip)
    conn.close()

    result = subprocess.run(
        [sys.executable, str(INJECTOR_PATH), "--class", fault_class],
        cwd=str(INJECTOR_CWD),
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise HTTPException(500, f"injector failed: {result.stderr}")

    # injector.py writes ground truth straight to SQLite -- read the real
    # episode_id back from there rather than scraping it out of stdout text.
    conn = _conn()
    row = conn.execute(
        "SELECT episode_id FROM episodes WHERE fault_class = ? ORDER BY t0 DESC LIMIT 1",
        (fault_class,),
    ).fetchone()
    conn.close()
    episode_id = row[0] if row else None

    # Close the loop for real: injector.py only creates the episode with
    # ground truth -- nothing scored it until now, which is why a
    # triggered episode used to sit "in flight" forever (up to the 10-min
    # staleness bound) with no diagnosis, action, or verdict ever recorded.
    # p3_agent.py (the real agent, port 8001) must already be running for
    # this to succeed -- p3_scorer.py calls it directly.
    time.sleep(SETTLE_SECONDS)

    scorer_result = subprocess.run(
        [sys.executable, str(SCORER_PATH)],
        cwd=str(SCORER_CWD),
        capture_output=True,
        text=True,
        timeout=SCORER_TIMEOUT_S,
    )
    if scorer_result.returncode != 0:
        # The episode itself is real and already recorded -- surface the
        # scorer failure but don't pretend the whole trigger failed.
        return {
            "status": "triggered_but_unscored",
            "episode_id": episode_id,
            "scorer_error": scorer_result.stderr,
        }

    conn = _conn()
    score_row = conn.execute(
        "SELECT predicted_class, correct, action_taken, action_applied, durability_verdict "
        "FROM scores WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    conn.close()

    return {
        "status": "scored",
        "episode_id": episode_id,
        "predicted_class": score_row[0] if score_row else None,
        "correct": bool(score_row[1]) if score_row else None,
        "action_taken": score_row[2] if score_row else None,
        "action_applied": bool(score_row[3]) if score_row and score_row[3] is not None else None,
        "durability_verdict": score_row[4] if score_row else None,
    }


@app.post("/promote")
def promote(fault_class: str, request: Request, payload: dict = Depends(require_role("admin"))):
    role = payload["role"]
    if fault_class not in PROMOTION_STREAK:
        raise HTTPException(400, f"'{fault_class}' has no promotion policy")
    conn = _conn()
    manual_set_state(conn, fault_class, CAN_ACT, streak=PROMOTION_STREAK[fault_class])
    _audit(conn, role, "/promote", f"fault_class={fault_class}", request.client.host)
    conn.close()
    return {"fault_class": fault_class, "state": CAN_ACT}


@app.post("/demote")
def demote(fault_class: str, request: Request, payload: dict = Depends(require_role("admin"))):
    role = payload["role"]
    conn = _conn()
    manual_set_state(conn, fault_class, DEMOTED, streak=0)
    _audit(conn, role, "/demote", f"fault_class={fault_class}", request.client.host)
    conn.close()
    return {"fault_class": fault_class, "state": DEMOTED}


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
