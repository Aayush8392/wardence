"""
Phase H provider-backend abstraction (step 1 of the locked 6-step build).

Locked policy this file implements (wardence_context.md's Model Strategy
section, Kimi review 09):
- episode-scoped provider lock: no mid-episode switching. call_chain()
  returns a typed failure per provider, never a half-answer -- the caller
  (the ReAct loop, step 3) is responsible for retrying the WHOLE episode
  against the next provider, since only the loop knows what "from step 0"
  means.
- intra-provider fallback before crossing providers (consecutive Groq
  entries below are tried before ever reaching OpenRouter).
- temperature=0 for every trust-ladder-scored call.
- provider-aware confidence extraction, tagged with its source
  ("logprob" for Gemini's real avgLogprobs, "self_reported" for
  Groq/OpenRouter) -- never mixed in the same calibration bin without
  the tag.
- real model IDs verified live against provider accounts 2026-07-29, not
  guessed from stale docs. Re-verify before trusting these again if this
  file goes untouched for a long stretch (see the Gemini preview-model
  deprecation history in wardence_context.md -- ~4.5-8 months of real
  life, only 2 weeks' minimum shutdown notice).

NOT in scope here (later Phase H steps, do not assume done):
- retry-the-whole-episode orchestration (step 3, needs episode/loop context)
- the semantic tool-call validator in front of the RBAC cage (step 2)
- model-tier gating enforcement for auto-fix promotion streaks (step 4 --
  `tier` is exposed on every result here for step 4 to consume, not
  enforced in this file)
- token/quota budget tracking + the Gemini staleness guard (step 5)
"""
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TEMPERATURE = 0  # locked: determinism for every trust-ladder-scored call



@dataclass
class LLMResult:
    text: str
    parsed: Optional[dict]
    provider: str
    model: str
    tier: str  # "primary" | "fallback" -- consumed by step 4, not enforced here
    confidence: Optional[float]
    confidence_source: Optional[str]  # "logprob" | "self_reported"
    raw: dict


@dataclass
class LLMFailure:
    provider: str
    model: str
    failure_type: str  # "timeout" | "rate_limited" | "bad_response" | "parse_failure"
    detail: str


def _extract_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _call_gemini(model: str, prompt: str, timeout: int, system_prompt: str | None = None) -> Union[LLMResult, LLMFailure]:
    key = os.environ["GEMINI_API_KEY"]
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": TEMPERATURE,
            # confirmed live 2026-07-29: gemini-3-flash-preview
            # honors thinkingBudget:0 (drops to zero thinking
            # tokens); the "-latest" alias currently resolves to
            # a model that REJECTS this with a hard 400 -- never
            # point this chain at the alias.
            "thinkingConfig": {"thinkingBudget": 0},
            # avgLogprobs is NOT returned unless explicitly
            # requested -- without this, the logprob-confidence
            # branch below is dead code and every call silently
            # falls back to self-reported (found live, 2026-07-29).
            "responseLogprobs": True,
            "logprobs": 1,
        },
    }
    # Real Gemini API shape: system instructions are a separate top-level
    # field, not part of `contents` -- added only when the caller passes
    # one (react_agent.py's diagnosis loop never does, unaffected).
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            json=body,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return LLMFailure("gemini", model, "timeout", f"timed out after {timeout}s")
    except requests.exceptions.RequestException as e:
        return LLMFailure("gemini", model, "bad_response", str(e))

    if resp.status_code == 429:
        return LLMFailure("gemini", model, "rate_limited", resp.text[:500])
    if resp.status_code >= 500:
        return LLMFailure("gemini", model, "bad_response", f"HTTP {resp.status_code}: {resp.text[:500]}")
    if resp.status_code != 200:
        return LLMFailure("gemini", model, "bad_response", f"HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        candidate = data["candidates"][0]
        text = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return LLMFailure("gemini", model, "bad_response", json.dumps(data)[:500])

    parsed = _extract_json(text)
    if parsed is None:
        return LLMFailure("gemini", model, "parse_failure", text[:500])

    # Real logprob-derived confidence per locked policy: Gemini exposes
    # avgLogprobs (a log-probability); convert to a 0-1 confidence via
    # exp(avg_logprob). Falls back to the model's own self-reported
    # confidence field only if avgLogprobs isn't present on the response.
    confidence = None
    confidence_source = None
    avg_logprob = candidate.get("avgLogprobs")
    if avg_logprob is not None:
        confidence = math.exp(avg_logprob)
        confidence_source = "logprob"
    elif isinstance(parsed.get("confidence"), (int, float)):
        confidence = parsed["confidence"]
        confidence_source = "self_reported"

    return LLMResult(
        text=text, parsed=parsed, provider="gemini", model=model, tier="primary",
        confidence=confidence, confidence_source=confidence_source, raw=data,
    )


def _call_openai_compat(
    provider: str, base_url: str, model: str, prompt: str, timeout: int, max_tokens: int, tier: str,
    system_prompt: str | None = None,
) -> Union[LLMResult, LLMFailure]:
    key_env = {
        "groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
        "cloudflare": "CLOUDFLARE_API_KEY", "deepinfra": "DEEPINFRA_API_KEY",
    }[provider]
    key = os.environ[key_env]
    # Real logprobs confirmed live 2026-07-30 for Cloudflare/DeepInfra
    # (gemma-4-26b-a4b-it, kimi-k2.6, Nemotron-3-Nano-30B-A3B) -- unlike
    # Groq/OpenRouter, which strip/never return it. Requesting it
    # unconditionally is harmless for providers that ignore it (Groq
    # errors on unknown params instead, so keep it OFF for groq/
    # openrouter specifically -- confirmed today Groq documents logprobs
    # as unsupported).
    request_logprobs = provider in ("cloudflare", "deepinfra")
    # Standard OpenAI-compatible system-role message, prepended only when
    # the caller passes one (react_agent.py/llm_replay_test.py never do,
    # unaffected) -- Kimi review 18 suggestion #6.
    messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [
        {"role": "user", "content": prompt}
    ]
    body = {
        "model": model,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
    }
    if request_logprobs:
        body["logprobs"] = True
        body["top_logprobs"] = 1
    try:
        resp = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return LLMFailure(provider, model, "timeout", f"timed out after {timeout}s")
    except requests.exceptions.RequestException as e:
        return LLMFailure(provider, model, "bad_response", str(e))

    if resp.status_code == 429:
        # Review 17 (2026-07-31): Cloudflare's real daily-Neuron-
        # exhaustion 429 is a distinct, pattern-matchable event (error
        # code 4006, "daily free allocation" in the body) -- confirmed
        # via Cloudflare's own community reports, not guessed. Without
        # this, every subsequent real episode today would re-discover
        # the exact same 429 and waste a real round-trip before falling
        # through to the next provider. Mark it exhausted-until-UTC-
        # midnight so quota_tracker.check_quota() skips it proactively
        # from the next call onward. The failure TYPE returned here
        # stays "rate_limited" either way -- this is a side effect, not
        # a reclassification (quota_tracker's own "quota_exhausted" type
        # is what a FUTURE skipped call will see, via call_one() above).
        body_text = resp.text[:500]
        if provider == "cloudflare" and ("4006" in body_text or "daily free allocation" in body_text):
            from quota_tracker import mark_exhausted  # local import, same style as call_one()'s own quota_tracker import
            mark_exhausted(provider, model, reason="Cloudflare 4006: daily free Neuron allocation exhausted")
        return LLMFailure(provider, model, "rate_limited", body_text)
    if resp.status_code >= 500:
        return LLMFailure(provider, model, "bad_response", f"HTTP {resp.status_code}: {resp.text[:500]}")
    if resp.status_code != 200:
        return LLMFailure(provider, model, "bad_response", f"HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        choice = data["choices"][0]
        message = choice["message"]
        text = message.get("content")
    except (KeyError, IndexError):
        return LLMFailure(provider, model, "bad_response", json.dumps(data)[:500])

    if not text:
        # Confirmed real failure mode (live-tested 2026-07-29): gpt-oss
        # models can consume the ENTIRE max_tokens budget on hidden
        # reasoning, leaving content null with no visible answer at all.
        reasoning_tokens = (
            data.get("usage", {}).get("completion_tokens_details", {}).get("reasoning_tokens")
        )
        return LLMFailure(
            provider, model, "bad_response",
            f"empty content (reasoning_tokens={reasoning_tokens}, raw={json.dumps(data)[:300]})",
        )

    parsed = _extract_json(text)
    if parsed is None:
        return LLMFailure(provider, model, "parse_failure", text[:500])

    # Real avgLogprobs-derived confidence for Cloudflare/DeepInfra (same
    # derivation as Gemini's -- confidence = exp(avg per-token logprob)),
    # confirmed live 2026-07-30. Groq/OpenRouter never get real logprobs
    # (request_logprobs is False for them), so they always fall back to
    # the model's own self-reported confidence field.
    confidence = None
    confidence_source = None
    logprobs_content = (choice.get("logprobs") or {}).get("content") if request_logprobs else None
    if logprobs_content:
        avg_logprob = sum(t["logprob"] for t in logprobs_content) / len(logprobs_content)
        confidence = math.exp(avg_logprob)
        confidence_source = "logprob"
    elif isinstance(parsed.get("confidence"), (int, float)):
        confidence = parsed["confidence"]
        confidence_source = "self_reported"

    return LLMResult(
        text=text, parsed=parsed, provider=provider, model=model, tier=tier,
        confidence=confidence, confidence_source=confidence_source, raw=data,
    )


# Real, FINAL provider chain -- locked 2026-07-30 after an extensive
# real search across ~25 candidate providers (see wardence_context.md's
# Model Strategy section for the full accounting: what was tested,
# real accuracy numbers, real quota/cost, and what was ruled out and
# why). Order here reflects real cost-efficiency, NOT the `tier` tag:
#
# 1. gemma-4-26b-a4b-it (Cloudflare, FREE, ~90 real episodes/day) --
#    tried first specifically because it's free and ongoing, preserving
#    both the free daily pool and Nemotron's finite paid credit for
#    when actually needed.
# 2. Nemotron-3-Nano-30B-A3B (DeepInfra, PAID, real overflow) -- a
#    one-time ~$5 credit real-measured at ~$0.000674/episode
#    (~7,400 episodes total, does NOT reset daily unlike #1). Real
#    accuracy 6/6, matching gemma's.
# 3. gemini-3-flash-preview (Gemini, FREE but thin, ~20 req/day) --
#    real logprobs, but a small preview-model quota, likely already
#    thin from testing.
# 4. kimi-k2.6 (Cloudflare, FREE, SAME pool as #1) -- last, since it
#    shares gemma's exact daily budget (if gemma failed on quota
#    exhaustion, kimi would too); real accuracy 6/6 but a real 20%
#    500-error rate seen in burst testing, so tier=fallback despite
#    equal accuracy.
#
# `tier` (primary/fallback, consumed by step 4's promotion-streak gate,
# not enforced here) is a SEPARATE dimension from list order: gemma,
# Nemotron, and gemini are all tier="primary" (all confirmed strong,
# real confidence signals), kimi is tier="fallback" specifically for
# its real reliability flakiness, not its accuracy.
#
# Real, previously-considered-then-ruled-out models NOT in this chain,
# logged here so they aren't silently re-added later without re-reading
# why: reka-edge (3/6 accuracy), Cloudflare's llama-3.2-3b-instruct
# (3/6) and gemma-2b-it-lora (1/6, systematically biased toward
# "crash-loop"), DeepInfra's Meta-Llama-3.1-8B-Instruct-Turbo (4/6),
# NVIDIA's llama-3.1-8b-instruct (4/6, also a finite one-time quota).
# Groq/OpenRouter models below are real, high-volume, SELF-REPORTED-
# confidence contributors -- they still carry the bulk of real episode
# volume despite the logprobs roster above, per the locked calibration-
# layer design (Kimi review 11) that makes their confidence honest.
PROVIDER_CHAIN = [
    {
        # 2026-07-31: real live failure confirmed (oom action-proposal call,
        # episode d57c4801...) -- gemma's own reasoning_content spent the
        # entire 2000-token budget before ever reaching the real answer,
        # coming back with content="" and finish_reason="length" (same
        # failure shape as the 2026-07-29 Groq gpt-oss finding, just a
        # different model). Originally bumped to 6000 (a guess), then
        # RE-CALIBRATED 2026-08-01 from real data after the prompt fix
        # (review 18): 5 real successful calls across 3 classes
        # (diagnosis + action-proposal, this value is shared by both)
        # topped out at 2192 tokens -- 3500 keeps a real ~1.6x margin
        # over that observed max while still acting as a real circuit
        # breaker (a future spiral fails fast into retry/escalation
        # instead of accommodating thousands of wasted tokens).
        "provider": "cloudflare", "model": "@cf/google/gemma-4-26b-a4b-it",
        "format": "openai_compat", "tier": "primary",
        "base_url": f"https://api.cloudflare.com/client/v4/accounts/{os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')}/ai/v1/chat/completions",
        "max_tokens": 3500,
    },
    {
        "provider": "deepinfra", "model": "nvidia/Nemotron-3-Nano-30B-A3B",
        "format": "openai_compat", "tier": "primary",
        "base_url": "https://api.deepinfra.com/v1/openai/chat/completions",
        "max_tokens": 2000,
    },
    {
        "provider": "gemini", "model": "gemini-3-flash-preview",
        "format": "gemini_native", "tier": "primary",
    },
    {
        # Same real reasoning-eats-the-budget risk as gemma above -- same
        # provider, same visible reasoning_content behavior -- widened
        # for the same reason, not yet observed failing but no reason to
        # wait for a live failure to fix an identical known risk. 3500
        # extrapolated from gemma's real 2026-08-01 calibration data
        # (same provider family) -- kimi itself was never actually
        # exercised this session (same-pool fallback, never fired), so
        # this specific value is untested for kimi, not independently
        # confirmed the way gemma's is.
        "provider": "cloudflare", "model": "@cf/moonshotai/kimi-k2.6",
        "format": "openai_compat", "tier": "fallback",
        "base_url": f"https://api.cloudflare.com/client/v4/accounts/{os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')}/ai/v1/chat/completions",
        "max_tokens": 3500,
    },
    {
        "provider": "groq", "model": "llama-3.3-70b-versatile",
        "format": "openai_compat", "tier": "fallback",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "max_tokens": 500,
    },
    {
        # gpt-oss models reason internally; verified live that a small
        # max_tokens can let reasoning eat the whole budget before any
        # visible answer is written -- kept generous here on purpose.
        "provider": "groq", "model": "openai/gpt-oss-120b",
        "format": "openai_compat", "tier": "fallback",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "max_tokens": 800,
    },
    {
        "provider": "openrouter", "model": "openai/gpt-oss-20b:free",
        "format": "openai_compat", "tier": "fallback",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "max_tokens": 800,
    },
]


def _extract_total_tokens(result: Union["LLMResult", "LLMFailure"]) -> int:
    """Real total-token count from the provider's own response, never
    estimated. openai_compat providers (cloudflare, deepinfra, groq,
    openrouter) share one response shape (`usage.total_tokens`, standard
    OpenAI-compatible contract); gemini uses its own
    `usageMetadata.totalTokenCount`. Defensive .get() chains throughout --
    a provider whose response happens to omit the field just contributes
    0, never crashes the call. LLMFailure has no `raw` attribute at all
    (a failed call, e.g. a timeout, may never have gotten a response body
    to read tokens from), so this only ever applies to LLMResult.
    """
    raw = getattr(result, "raw", None)
    if not raw:
        return 0
    if result.provider == "gemini":
        return raw.get("usageMetadata", {}).get("totalTokenCount") or 0
    return raw.get("usage", {}).get("total_tokens") or 0


def _extract_neurons(result: Union["LLMResult", "LLMFailure"]) -> float:
    """Real Cloudflare Neuron cost for this specific call, straight from
    their own response (`usage.neurons`, confirmed live 2026-07-31 --
    also mirrored in the `cf-ai-neurons` response header, body used here
    since call_one() only ever sees the parsed JSON, not raw headers).
    Only Cloudflare reports this; every other provider's raw response has
    no such field, so this safely returns 0 for them."""
    raw = getattr(result, "raw", None)
    if not raw or getattr(result, "provider", None) != "cloudflare":
        return 0
    return raw.get("usage", {}).get("neurons") or 0


def call_one(
    entry: dict, prompt: str, timeout: int = 30, system_prompt: str | None = None,
    episode_id: str | None = None,
) -> Union[LLMResult, LLMFailure]:
    """
    Call a single provider-chain entry. Returns LLMResult or LLMFailure --
    never raises on a normal API-level failure.

    Step 5 (quota_tracker.py) wired in here, the one real choke point
    every caller (llm_replay_test.py, react_agent.py) already goes
    through -- checks quota BEFORE sending a real request (short-
    circuits to a "quota_exhausted" failure with zero real request sent
    if already at 100% today, per the locked graceful-degradation
    policy: never silently retry into a provider already known to be
    dead), and records real usage only for a request that actually got
    sent.
    """
    from quota_tracker import check_quota, record_call  # local import: avoids a hard dependency for any caller that only wants call_chain's pure LLM logic, e.g. a future unit test with no DB

    quota = check_quota(entry["provider"], entry["model"])
    if quota["status"] == "exhausted":
        # quota["limit"] is None for a provider marked exhausted via a
        # real Cloudflare-4006-style event (review 17) rather than a
        # known RPD count -- use the real recorded reason in that case
        # instead of printing "None/None".
        detail = (
            f"real daily limit reached ({quota['used']}/{quota['limit']}) -- no request sent"
            if quota.get("limit") is not None
            else f"{quota.get('reason', 'marked exhausted')} -- no request sent"
        )
        return LLMFailure(entry["provider"], entry["model"], "quota_exhausted", detail)

    if entry["format"] == "gemini_native":
        result = _call_gemini(entry["model"], prompt, timeout, system_prompt=system_prompt)
    else:
        result = _call_openai_compat(
            entry["provider"], entry["base_url"], entry["model"], prompt, timeout,
            entry.get("max_tokens", 500), entry["tier"], system_prompt=system_prompt,
        )
    record_call(
        entry["provider"], entry["model"], tokens=_extract_total_tokens(result),
        neurons=_extract_neurons(result), episode_id=episode_id,
    )
    return result


def call_chain(prompt: str, chain: Optional[list] = None, timeout: int = 30):
    """
    Try each entry in the chain in order, stopping at the first success.

    This is ONE diagnosis attempt against whichever provider succeeds --
    it does NOT retry a half-completed reasoning trace across providers.
    There is no reasoning trace to preserve yet (step 1 is single-shot);
    step 3's ReAct loop is where the real episode-scoped-lock behavior
    (abort the WHOLE episode, retry from step 0 against the next
    provider) actually gets implemented, since only the loop knows what
    "from step 0" means.

    Returns (LLMResult, attempts) on success, or (None, attempts) if
    every entry in the chain failed, where `attempts` is the list of
    LLMFailure objects in the order tried. The caller decides what
    "every provider failed" means (report-only fallback, an
    LLM_UNAVAILABLE episode status, etc.) -- not decided in this file.
    """
    chain = chain if chain is not None else PROVIDER_CHAIN
    attempts = []
    for entry in chain:
        result = call_one(entry, prompt, timeout=timeout)
        if isinstance(result, LLMResult):
            return result, attempts
        attempts.append(result)
    return None, attempts
