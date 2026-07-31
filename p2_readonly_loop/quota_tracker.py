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
DeepInfra (Nemotron) is dollar-credit-metered with no clean per-call
cost signal in its response, so it's still call-count-only here.

CORRECTION, 2026-07-31 (later the same day): Kimi review 17 claimed
Cloudflare Neuron-tracking "would require estimating Neurons-per-token,
which is fragile" -- checked directly against a real live response and
found this was WRONG. Cloudflare's OWN response reports the real,
exact Neuron cost per call directly (`usage.neurons` in the body, also
mirrored in the `cf-ai-neurons` response header) -- no estimation
needed at all. CLOUDFLARE_DAILY_NEURON_BUDGET/
CLOUDFLARE_NEURON_SAFETY_THRESHOLD below implement a real, exact,
pre-call check using this real field, pooled correctly across BOTH
Cloudflare models (gemma primary + kimi fallback share one real
account-wide daily allocation, confirmed in the locked provider-chain
design) -- summing per-model rows would under-count real usage against
the shared pool.
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

# Real Cloudflare AI free-tier daily allocation (confirmed via their own
# docs/dashboard).
CLOUDFLARE_DAILY_NEURON_BUDGET = 10000
# 2026-08-01: the original 1500 was a guess made before any real per-
# episode Neuron data existed. Real measured data since: a full gemma-only
# under-provisioned-replicas episode (the heaviest real class measured so
# far -- most tools, most turns) costs ~90-122 Neurons end-to-end
# (diagnosis loop + action-proposal). 400 is a deliberate, real safety
# margin over that observed max for tonight's first real overnight batch,
# not yet a tightly-derived per-class number -- llm_token_usage.json
# (built this same session) will have real per-class-per-provider min/
# max/avg data after this run completes, letting this be re-derived
# properly (and potentially made per-class) rather than one flat guess.
CLOUDFLARE_NEURON_SAFETY_THRESHOLD = 400


def ensure_quota_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_quota_usage (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            day TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, model, day)
        )
        """
    )
    # Idempotent add for a pre-existing DB from before the tokens column
    # existed (2026-07-31 addition) -- same PRAGMA-based pattern this
    # project already uses elsewhere (episode_snapshots' gate_substitution
    # column, the scores table).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(provider_quota_usage)")}
    if "tokens" not in cols:
        conn.execute("ALTER TABLE provider_quota_usage ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
    if "neurons" not in cols:
        conn.execute("ALTER TABLE provider_quota_usage ADD COLUMN neurons REAL NOT NULL DEFAULT 0")
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


def ensure_call_log_table(conn: sqlite3.Connection):
    """Real per-CALL granularity, 2026-07-31 addition -- provider_quota_usage
    only ever aggregated to (provider, model, day), which can't answer "how
    many tokens did THIS episode cost" once more than one call happens on
    the same day (the normal case). episode_id is nullable/best-effort --
    not every real caller of record_call() has episode context to pass
    (e.g. a raw calibration call_one() test), so this degrades gracefully
    to timestamp-window queries rather than requiring a bigger plumbing
    change through model_backend.py/react_agent.py right now."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            tokens INTEGER NOT NULL DEFAULT 0,
            neurons REAL NOT NULL DEFAULT 0,
            episode_id TEXT,
            called_at TEXT NOT NULL
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(provider_call_log)")}
    if "neurons" not in cols:
        conn.execute("ALTER TABLE provider_call_log ADD COLUMN neurons REAL NOT NULL DEFAULT 0")
    conn.commit()


def record_call(provider: str, model: str, tokens: int = 0, neurons: float = 0, episode_id: str | None = None):
    """Call exactly once per REAL request actually sent to a provider --
    never for a call short-circuited by check_quota() before sending,
    since that never consumed real quota. `tokens`/`neurons` are the real
    per-call cost, extracted by the caller from the provider's own
    response (model_backend.py's _extract_total_tokens/_extract_neurons)
    -- 0 if the provider's response didn't include the field, never
    guessed/estimated. `neurons` is only ever real for Cloudflare (the
    only provider that reports it); every other provider always passes 0.
    Writes to BOTH the existing daily aggregate (provider_quota_usage,
    unchanged, still what check_quota() reads) and the per-call log
    (provider_call_log) -- the aggregate stays the cheap/fast path for
    the real-time quota check every call already does; the log exists
    purely for after-the-fact exact-attribution queries."""
    conn = sqlite3.connect(DB_PATH)
    ensure_quota_table(conn)
    ensure_call_log_table(conn)
    today = _today()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO provider_call_log (provider, model, tokens, neurons, episode_id, called_at) VALUES (?, ?, ?, ?, ?, ?)",
        (provider, model, tokens, neurons, episode_id, now),
    )
    conn.execute(
        """
        INSERT INTO provider_quota_usage (provider, model, day, calls, tokens, neurons) VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(provider, model, day) DO UPDATE SET
            calls = calls + 1, tokens = tokens + excluded.tokens, neurons = neurons + excluded.neurons
        """,
        (provider, model, today, tokens, neurons),
    )
    conn.commit()
    conn.close()


def get_today_neuron_usage(provider: str) -> float:
    """Real Neuron usage summed across ALL models for this provider today
    -- deliberately NOT scoped to one model, since Cloudflare's gemma and
    kimi share one real account-wide daily Neuron allocation (confirmed
    in the locked provider-chain design). Scoping per-model would
    under-count real usage against the shared pool."""
    conn = sqlite3.connect(DB_PATH)
    ensure_quota_table(conn)
    row = conn.execute(
        "SELECT COALESCE(SUM(neurons), 0) FROM provider_quota_usage WHERE provider = ? AND day = ?",
        (provider, _today()),
    ).fetchone()
    conn.close()
    return row[0] if row else 0.0


def get_calls_since(since_iso: str) -> list[dict]:
    """Real per-call log rows since a given UTC ISO timestamp -- the
    live-diff tool for isolating one specific episode/test's real usage
    (call this with a timestamp captured right before the episode starts).
    Provider/model breakdown not pre-aggregated here on purpose -- small
    result sets are expected (one calibration episode at a time), let the
    caller sum/group as needed rather than guessing the right grouping."""
    conn = sqlite3.connect(DB_PATH)
    ensure_call_log_table(conn)
    rows = conn.execute(
        "SELECT provider, model, tokens, neurons, episode_id, called_at FROM provider_call_log "
        "WHERE called_at >= ? ORDER BY called_at",
        (since_iso,),
    ).fetchall()
    conn.close()
    return [
        {"provider": r[0], "model": r[1], "tokens": r[2], "neurons": r[3], "episode_id": r[4], "called_at": r[5]}
        for r in rows
    ]


def get_episode_usage(episode_id: str) -> list[dict]:
    """Real per-call rows for ONE specific episode, direct by episode_id --
    2026-08-01 addition, now that call_one()/record_call() actually
    receive and store it (threaded through react_agent.py/
    action_proposer.py/p3_agent.py/p3_scorer.py). This is the real
    replacement for the timestamp-window get_calls_since() diffing this
    project relied on before real episode_id attribution existed --
    exact, not approximate, and works for ANY past episode after the
    fact, not just one captured live with a timestamp beforehand."""
    conn = sqlite3.connect(DB_PATH)
    ensure_call_log_table(conn)
    rows = conn.execute(
        "SELECT provider, model, tokens, neurons, called_at FROM provider_call_log "
        "WHERE episode_id = ? ORDER BY called_at",
        (episode_id,),
    ).fetchall()
    conn.close()
    return [
        {"provider": r[0], "model": r[1], "tokens": r[2], "neurons": r[3], "called_at": r[4]}
        for r in rows
    ]


def get_all_episode_usage_summary() -> list[dict]:
    """Real per-episode rollup across EVERY episode that has real
    attributed calls -- the actual thing this whole feature was built
    for: a single query after a batch run finishes, no more guessing or
    manual timestamp-window reconstruction per episode."""
    conn = sqlite3.connect(DB_PATH)
    ensure_call_log_table(conn)
    rows = conn.execute(
        """
        SELECT episode_id, provider, COUNT(*) as calls, SUM(tokens) as tokens, SUM(neurons) as neurons
        FROM provider_call_log
        WHERE episode_id IS NOT NULL
        GROUP BY episode_id, provider
        ORDER BY MIN(called_at)
        """
    ).fetchall()
    conn.close()
    return [
        {"episode_id": r[0], "provider": r[1], "calls": r[2], "tokens": r[3], "neurons": r[4]}
        for r in rows
    ]


def get_today_usage(provider: str, model: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    ensure_quota_table(conn)
    row = conn.execute(
        "SELECT calls FROM provider_quota_usage WHERE provider = ? AND model = ? AND day = ?",
        (provider, model, _today()),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def get_today_token_usage(provider: str, model: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    ensure_quota_table(conn)
    row = conn.execute(
        "SELECT tokens FROM provider_quota_usage WHERE provider = ? AND model = ? AND day = ?",
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
    "ok" (DeepInfra -- see module docstring), with limit=None -- UNLESS
    it's been explicitly marked exhausted via mark_exhausted() (real
    Cloudflare 4006 event, review 17) and that mark hasn't expired past
    UTC midnight yet, in which case this still reports "exhausted" even
    with no KNOWN_DAILY_LIMITS entry at all.

    Cloudflare gets its own REAL pre-call check (2026-07-31, corrects
    review 17's wrong "can't be done without estimating" claim -- see
    module docstring): if today's real, exact Neuron usage (summed
    across gemma+kimi, the shared pool) is within
    CLOUDFLARE_NEURON_SAFETY_THRESHOLD of the daily budget, report
    "exhausted" BEFORE the call is ever sent -- lets call_chain() fall
    through to the next provider (deepinfra) proactively, and the day
    boundary in get_today_neuron_usage()/_today() means this naturally
    re-opens gemma the instant UTC midnight resets the real pool, with
    zero extra code for that case.
    """
    exhaustion_reason = is_marked_exhausted(provider, model)
    if exhaustion_reason is not None:
        return {"status": "exhausted", "used": None, "limit": None, "reason": exhaustion_reason}

    if provider == "cloudflare":
        neurons_used = get_today_neuron_usage(provider)
        neurons_remaining = CLOUDFLARE_DAILY_NEURON_BUDGET - neurons_used
        if neurons_remaining < CLOUDFLARE_NEURON_SAFETY_THRESHOLD:
            return {
                "status": "exhausted", "used": neurons_used, "limit": CLOUDFLARE_DAILY_NEURON_BUDGET,
                "reason": f"only {neurons_remaining:.1f} Neurons left today "
                          f"(below the {CLOUDFLARE_NEURON_SAFETY_THRESHOLD} safety threshold)",
            }

    limit = KNOWN_DAILY_LIMITS.get((provider, model))
    used = get_today_usage(provider, model)
    if limit is None:
        return {"status": "ok", "used": used, "limit": None}
    if used >= limit:
        return {"status": "exhausted", "used": used, "limit": limit}
    if used >= limit * WARN_FRACTION:
        return {"status": "warn", "used": used, "limit": limit}
    return {"status": "ok", "used": used, "limit": limit}
