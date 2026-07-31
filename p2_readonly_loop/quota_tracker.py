"""
Phase H step 5 (part 1): per-provider daily budget tracking.

Locked policy this implements (wardence_context.md's Model Strategy,
Kimi review 09 item 8): "Token/request budget accounting, per provider
per day, with a graceful-degradation path: at 100% of any free-tier
limit, hard-fallback to report_only with a visible 'LLM quota
exhausted' status -- never silently retry into a broken/absent model."

Wired into model_backend.call_one() so every real caller (the replay
script, the ReAct loop) gets this automatically, with zero per-caller
changes needed.

Real, honest limitation: only the RPD-based providers (Gemini, Groq,
OpenRouter) have a clean daily-request-count limit to check against.
Cloudflare (gemma/kimi) and DeepInfra (Nemotron) are Neuron/credit-
metered, not request-count-metered -- a call-count check can't derive
a real "100% exhausted" signal for them (a request could be cheap or
expensive depending on prompt/response length). Their usage is still
recorded here for visibility (so real per-provider call volume is
knowable), but KNOWN_DAILY_LIMITS deliberately has no entry for them --
check_quota() always returns "ok" for a provider/model pair with no
known limit, rather than guessing one.
"""
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "p3_trust_action"))

from trust_engine import DB_PATH  # noqa: E402

# Real, confirmed limits (wardence_context.md's Model Strategy section,
# live-verified 2026-07-30). Re-verify before trusting these again if
# this file goes untouched for a long stretch -- same staleness risk
# every other real-number-bearing file in this project carries.
KNOWN_DAILY_LIMITS = {
    ("gemini", "gemini-3-flash-preview"): 20,
    ("groq", "llama-3.3-70b-versatile"): 1000,
    ("groq", "openai/gpt-oss-120b"): 1000,
    ("openrouter", "openai/gpt-oss-20b:free"): 50,
}

WARN_FRACTION = 0.8


def ensure_quota_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_quota_usage (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            day TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, model, day)
        )
        """
    )
    conn.commit()


def ensure_exhaustion_table(conn: sqlite3.Connection):
    """
    Kimi review 17 (2026-07-31) -- a real, narrow fix for the Cloudflare
    Neuron-exhaustion gap this module's own docstring already documents:
    Cloudflare/DeepInfra have no KNOWN_DAILY_LIMITS entry (can't derive a
    request-count-based cutoff for credit-metered providers), so
    check_quota() always said "ok" for them regardless of real usage.
    Confirmed live (web search, Cloudflare's own community reports): real
    daily Neuron exhaustion returns a distinctive HTTP 429 with error
    code 4006 and "daily free allocation" in the body -- a real,
    pattern-matchable signal, deliberately NOT the same thing as a
    generic transient rate-limit 429. This table records that specific
    event when model_backend.py's 429 handler detects it, so every
    subsequent call THIS SAME DAY skips the provider proactively instead
    of re-discovering the same 429 on every real episode until UTC
    midnight. Deliberately NOT Neuron-count tracking (review 17: "you
    don't know the Neuron count per call, so you can't track
    consumption... adding Neuron-tracking would require estimating
    Neurons-per-token, which is fragile") -- this only ever records the
    exhaustion EVENT, nothing more.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_exhaustion (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            exhausted_until TEXT NOT NULL,
            reason TEXT,
            PRIMARY KEY (provider, model)
        )
        """
    )
    conn.commit()


def _next_utc_midnight() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    tomorrow = (now + datetime.timedelta(days=1)).date()
    return datetime.datetime.combine(
        tomorrow, datetime.time.min, tzinfo=datetime.timezone.utc
    ).isoformat()


def mark_exhausted(provider: str, model: str, reason: str):
    """Called by model_backend.py's 429 handler on a real, confirmed
    Cloudflare 4006 (or equivalent) response -- never speculatively."""
    conn = sqlite3.connect(DB_PATH)
    ensure_exhaustion_table(conn)
    conn.execute(
        """
        INSERT INTO provider_exhaustion (provider, model, exhausted_until, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(provider, model) DO UPDATE SET
            exhausted_until = excluded.exhausted_until, reason = excluded.reason
        """,
        (provider, model, _next_utc_midnight(), reason),
    )
    conn.commit()
    conn.close()


def is_marked_exhausted(provider: str, model: str) -> "str | None":
    """Returns the real reason string if still within the marked
    exhaustion window, else None (expired past UTC midnight or never
    marked)."""
    conn = sqlite3.connect(DB_PATH)
    ensure_exhaustion_table(conn)
    row = conn.execute(
        "SELECT exhausted_until, reason FROM provider_exhaustion WHERE provider = ? AND model = ?",
        (provider, model),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    exhausted_until, reason = row
    if datetime.datetime.now(datetime.timezone.utc).isoformat() < exhausted_until:
        return reason
    return None


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def record_call(provider: str, model: str):
    """Call exactly once per REAL request actually sent to a provider --
    never for a call short-circuited by check_quota() before sending,
    since that never consumed real quota."""
    conn = sqlite3.connect(DB_PATH)
    ensure_quota_table(conn)
    today = _today()
    conn.execute(
        """
        INSERT INTO provider_quota_usage (provider, model, day, calls) VALUES (?, ?, ?, 1)
        ON CONFLICT(provider, model, day) DO UPDATE SET calls = calls + 1
        """,
        (provider, model, today),
    )
    conn.commit()
    conn.close()


def get_today_usage(provider: str, model: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    ensure_quota_table(conn)
    row = conn.execute(
        "SELECT calls FROM provider_quota_usage WHERE provider = ? AND model = ? AND day = ?",
        (provider, model, _today()),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def check_quota(provider: str, model: str) -> dict:
    """
    Returns {"status": "ok" | "warn" | "exhausted", "used": int, "limit": Optional[int]}.
    "exhausted" means the caller MUST NOT send this request -- treat it
    as a graceful-degradation trigger (report_only / try next provider),
    never a silent retry into a provider already known to be dead today.
    A provider/model with no entry in KNOWN_DAILY_LIMITS always reports
    "ok" (Cloudflare/DeepInfra -- see module docstring), with limit=None
    -- UNLESS it's been explicitly marked exhausted via mark_exhausted()
    (real Cloudflare 4006 event, review 17) and that mark hasn't expired
    past UTC midnight yet, in which case this still reports "exhausted"
    even with no KNOWN_DAILY_LIMITS entry at all.
    """
    exhaustion_reason = is_marked_exhausted(provider, model)
    if exhaustion_reason is not None:
        return {"status": "exhausted", "used": None, "limit": None, "reason": exhaustion_reason}

    limit = KNOWN_DAILY_LIMITS.get((provider, model))
    used = get_today_usage(provider, model)
    if limit is None:
        return {"status": "ok", "used": used, "limit": None}
    if used >= limit:
        return {"status": "exhausted", "used": used, "limit": limit}
    if used >= limit * WARN_FRACTION:
        return {"status": "warn", "used": used, "limit": limit}
    return {"status": "ok", "used": used, "limit": limit}
