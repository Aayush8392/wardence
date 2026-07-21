"""
P3 Operator API: JWT-gated, three roles (admin / demo-trigger / viewer).

- admin: full access. Trigger any implemented fault class, manually
  promote/demote, read everything. Tokens minted locally via
  mint_token.py -- no login endpoint, admin is never distributed
  (see wardence_context.md).
- demo-trigger: can only /trigger, and only the curated safe subset
  (SAFE_DEMO_CLASSES). Rate-limited: cooldown + daily cap per IP, and
  only one episode allowed in-flight system-wide at a time. Per-IP
  limiting is a deterrent, not identity verification -- a VPN/different
  network bypasses it. Softened by the global one-at-a-time rule.
- viewer: read-only, /trust only.

Every request is audit-logged regardless of role/outcome.

Usage:
    python3 p3_trust_action/mint_token.py admin      # get a token first
    uvicorn operator_api:app --reload --app-dir p3_trust_action --port 8002
"""

import subprocess
import sys
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request

sys.path.insert(0, str(Path(__file__).parent))

import sqlite3  # noqa: E402

from auth import decode_token  # noqa: E402
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

IMPLEMENTED_CLASSES = {"crash-loop"}  # injector.py only knows how to inject this so far
SAFE_DEMO_CLASSES = {"crash-loop"}  # curated subset, per wardence_context.md
COOLDOWN_S = 60
DAILY_CAP = 3

INJECTOR_PATH = Path(__file__).parent.parent / "p2_readonly_loop" / "injector.py"
INJECTOR_CWD = Path(__file__).parent.parent / "p2_readonly_loop"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    ensure_trust_tables(conn)
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
    def dependency(authorization: str = Header(default=None)) -> str:
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
        return role

    return dependency


@app.get("/trust")
def trust(request: Request, role: str = Depends(require_role("admin", "demo-trigger", "viewer"))):
    conn = _conn()
    states = [get_trust_state(conn, fc) for fc in PROMOTION_STREAK]
    _audit(conn, role, "/trust", "read", request.client.host)
    conn.close()
    return {"states": states}


def _episode_in_flight(conn) -> bool:
    row = conn.execute(
        """
        SELECT e.episode_id FROM episodes e
        LEFT JOIN scores s ON e.episode_id = s.episode_id
        WHERE s.episode_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    return row is not None


@app.post("/trigger")
def trigger(
    fault_class: str,
    request: Request,
    role: str = Depends(require_role("admin", "demo-trigger")),
):
    conn = _conn()
    ip = request.client.host

    if fault_class not in IMPLEMENTED_CLASSES:
        _audit(conn, role, "/trigger", f"rejected: '{fault_class}' not implemented", ip)
        conn.close()
        raise HTTPException(400, f"'{fault_class}' has no injector implementation yet")

    if role == "demo-trigger":
        if fault_class not in SAFE_DEMO_CLASSES:
            _audit(conn, role, "/trigger", f"rejected: '{fault_class}' not in safe subset", ip)
            conn.close()
            raise HTTPException(403, f"demo-trigger may only trigger {SAFE_DEMO_CLASSES}")

        if _episode_in_flight(conn):
            _audit(conn, role, "/trigger", "rejected: episode already in flight", ip)
            conn.close()
            raise HTTPException(429, "an episode is already in flight, try again shortly")

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
        [sys.executable, str(INJECTOR_PATH)],
        cwd=str(INJECTOR_CWD),
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise HTTPException(500, f"injector failed: {result.stderr}")
    return {"status": "triggered", "output": result.stdout}


@app.post("/promote")
def promote(fault_class: str, request: Request, role: str = Depends(require_role("admin"))):
    if fault_class not in PROMOTION_STREAK:
        raise HTTPException(400, f"'{fault_class}' has no promotion policy")
    conn = _conn()
    manual_set_state(conn, fault_class, CAN_ACT, streak=PROMOTION_STREAK[fault_class])
    _audit(conn, role, "/promote", f"fault_class={fault_class}", request.client.host)
    conn.close()
    return {"fault_class": fault_class, "state": CAN_ACT}


@app.post("/demote")
def demote(fault_class: str, request: Request, role: str = Depends(require_role("admin"))):
    conn = _conn()
    manual_set_state(conn, fault_class, DEMOTED, streak=0)
    _audit(conn, role, "/demote", f"fault_class={fault_class}", request.client.host)
    conn.close()
    return {"fault_class": fault_class, "state": DEMOTED}
