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
    # Real per-call backend-version signal, added 2026-08-1x after a live
    # Gemini regression (responseLogprobs unexpectedly started 400ing)
    # surfaced the need for a future model-version-drift check. NOT a
    # detector itself -- just capture, for whenever that's built. Gemini:
    # raw's own `modelVersion` (only informative when calling an alias;
    # echoes the pinned ID back otherwise). openai_compat providers:
    # `system_fingerprint` where the provider returns one (confirmed live
    # for Groq; Cloudflare/OpenRouter unconfirmed, DeepInfra confirmed
    # absent). None when the provider doesn't expose one.
    version_fingerprint: Optional[str] = None


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

    # Real regression found live 2026-08-1x: gemini-3-flash-preview started
    # 400ing on responseLogprobs (message: "Logprobs is not enabled for
    # this model") on the exact call shape that worked in production up to
    # that point -- confirmed via direct isolation test (same call minus
    # responseLogprobs -> real 200, model unaffected otherwise; /models
    # list confirms the model itself still exists, same version string).
    # Real, permanent server-side capability change on Google's end, not
    # a transient error -- retrying the SAME request would fail forever.
    # Graceful degrade: retry once without logprobs, fall back to
    # self-reported confidence, rather than losing this provider's real
    # diagnoses entirely every time the chain reaches it.
    if (
        resp.status_code == 400
        and "Logprobs is not enabled" in resp.text
        and body["generationConfig"].get("responseLogprobs")
    ):
        body["generationConfig"].pop("responseLogprobs", None)
        body["generationConfig"].pop("logprobs", None)
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                json=body,
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            return LLMFailure("gemini", model, "timeout", f"timed out after {timeout}s (logprobs-fallback retry)")
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
        version_fingerprint=data.get("modelVersion"),
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
        version_fingerprint=data.get("system_fingerprint"),
    )


# Real, live-confirmed streaming-capable subset of the chain.
# check_stream_support.py (gitignored) originally confirmed both
# cloudflare/deepinfra return genuine multi-chunk SSE at all -- but
# check_stream_confidence_source.py (gitignored, run 2026-08-1x)
# confirmed a real, disqualifying problem specific to deepinfra: its
# streaming response does NOT carry usable per-chunk logprobs the way
# its non-streaming response does, silently degrading
# confidence_source away from "logprob" to "self_reported" -- and this
# streaming call IS the real production diagnosis call for a live-
# triggered episode (no separate non-streaming call happens
# afterward), so this would have corrupted real logged confidence
# quality, not just the widget display. deepinfra/nemotron was ALSO
# confirmed (same test run) to only ever emit its reasoning as ONE
# complete chunk anyway (1 real chunk seen, never incremental) -- so
# nothing genuinely "live" is lost by excluding it here; it already
# gets the correct "reveal as one block" treatment via the plain
# call_one() fallback path in react_agent.py, which preserves real
# logprobs. Only cloudflare (gemma) is included: confirmed live to
# stream incrementally (326 real chunks in the same test) AND keep its
# real per-chunk logprobs intact (confidence_source == "logprob").
# Groq/OpenRouter/Gemini were never tested for streaming at all, not
# included for that separate reason.
STREAMING_CAPABLE_PROVIDERS = {"cloudflare"}


def _call_openai_compat_streaming(
    provider: str, base_url: str, model: str, prompt: str, timeout: int, max_tokens: int, tier: str,
    system_prompt: str | None, on_reasoning_chunk, on_content_chunk,
) -> Union[LLMResult, LLMFailure]:
    """
    Real token-by-token streaming variant of _call_openai_compat, built
    for the live Operator "Central Thinking Hub" widget ONLY. This is the
    SAME single real API call _call_openai_compat would make for this
    chain entry (same body, same model, same temperature=0) -- never a
    second, duplicate call alongside the real production one. Only ever
    called for entries in STREAMING_CAPABLE_PROVIDERS (currently
    cloudflare only -- see that constant's own comment for why
    deepinfra was tested and excluded). Real reasoning text arrives
    under EITHER `reasoning` or `reasoning_content`
    depending on provider/params (gemma's key is conditional on
    reasoning_effort being set) -- both keys are always checked here,
    whichever is present wins, same fix as the probe script's own
    root-caused bug.

    Real, NOT YET LIVE-VERIFIED caveat, flagged rather than silently
    assumed: whether Cloudflare/DeepInfra's STREAMING responses carry
    real per-chunk `logprobs`/a final `usage` object the same way their
    non-streaming responses do (requested here via the standard OpenAI-
    compatible `stream_options: {"include_usage": true}` + `logprobs:
    true` params) has only been confirmed for the non-streaming shape --
    verify live before trusting this path's confidence/token numbers.
    Defensive: if either is absent, confidence/token-count fall back the
    same way the non-streaming path already does for a provider that
    omits them (self_reported / 0), never crashes.
    """
    key_env = {"cloudflare": "CLOUDFLARE_API_KEY", "deepinfra": "DEEPINFRA_API_KEY"}[provider]
    key = os.environ[key_env]
    messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [
        {"role": "user", "content": prompt}
    ]
    body = {
        "model": model,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "logprobs": True,
        "top_logprobs": 1,
    }
    try:
        resp = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=timeout,
            stream=True,
        )
    except requests.exceptions.Timeout:
        return LLMFailure(provider, model, "timeout", f"timed out after {timeout}s (streaming)")
    except requests.exceptions.RequestException as e:
        return LLMFailure(provider, model, "bad_response", str(e))

    if resp.status_code == 429:
        # Same real Cloudflare-4006 pattern-match as _call_openai_compat's
        # non-streaming path -- kept in sync by hand (see that function's
        # own comment for the full reasoning).
        body_text = resp.text[:500]
        if provider == "cloudflare" and ("4006" in body_text or "daily free allocation" in body_text):
            from quota_tracker import mark_exhausted
            mark_exhausted(provider, model, reason="Cloudflare 4006: daily free Neuron allocation exhausted")
        return LLMFailure(provider, model, "rate_limited", body_text)
    if resp.status_code >= 500:
        return LLMFailure(provider, model, "bad_response", f"HTTP {resp.status_code}: {resp.text[:500]}")
    if resp.status_code != 200:
        return LLMFailure(provider, model, "bad_response", f"HTTP {resp.status_code}: {resp.text[:500]}")

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    logprob_entries: list[dict] = []
    final_usage: Optional[dict] = None
    fingerprint: Optional[str] = None
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            fingerprint = chunk.get("system_fingerprint", fingerprint)
            if chunk.get("usage"):
                final_usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                if on_reasoning_chunk:
                    on_reasoning_chunk(reasoning_delta)
            content_delta = delta.get("content")
            if content_delta:
                content_parts.append(content_delta)
                if on_content_chunk:
                    on_content_chunk(content_delta)
            chunk_logprobs = (choices[0].get("logprobs") or {}).get("content")
            if chunk_logprobs:
                logprob_entries.extend(chunk_logprobs)
    except requests.exceptions.RequestException as e:
        return LLMFailure(provider, model, "bad_response", f"stream read error: {e}")

    text = "".join(content_parts)
    if not text:
        return LLMFailure(
            provider, model, "bad_response",
            f"empty streamed content (reasoning_chars={len(''.join(reasoning_parts))})",
        )

    parsed = _extract_json(text)
    if parsed is None:
        return LLMFailure(provider, model, "parse_failure", text[:500])

    confidence = None
    confidence_source = None
    if logprob_entries:
        avg_logprob = sum(t["logprob"] for t in logprob_entries) / len(logprob_entries)
        confidence = math.exp(avg_logprob)
        confidence_source = "logprob"
    elif isinstance(parsed.get("confidence"), (int, float)):
        confidence = parsed["confidence"]
        confidence_source = "self_reported"

    raw = {
        "streamed": True,
        "usage": final_usage or {},
        "system_fingerprint": fingerprint,
        "reasoning_text": "".join(reasoning_parts),
    }
    return LLMResult(
        text=text, parsed=parsed, provider=provider, model=model, tier=tier,
        confidence=confidence, confidence_source=confidence_source, raw=raw,
        version_fingerprint=fingerprint,
    )


def call_one_streaming(
    entry: dict, prompt: str, timeout: int = 30, system_prompt: str | None = None,
    episode_id: str | None = None,
    on_reasoning_chunk=None, on_content_chunk=None,
) -> Union[LLMResult, LLMFailure]:
    """
    Streaming twin of call_one(), built ONLY for the live Operator
    "Central Thinking Hub" widget -- lets a connected browser watch a
    real diagnosis call's reasoning arrive live. NOT a second/parallel
    call: this IS the one real production call for this chain entry,
    made once, same as call_one() would make it -- a caller must use ONE
    or the OTHER for a given entry/turn, never both (double-calling would
    double real quota spend and risk two different real answers for one
    logical attempt).

    Only cloudflare/deepinfra are streaming-capable in this chain today
    (STREAMING_CAPABLE_PROVIDERS above) -- callers MUST check
    `entry["provider"] in STREAMING_CAPABLE_PROVIDERS` before calling
    this and fall back to plain call_one() otherwise; raises ValueError
    if called against an unsupported entry rather than silently
    degrading to a non-streaming call the caller didn't ask for.

    Shares call_one()'s real quota-check-before-send / record-usage-after
    / clear-exhausted-on-success wiring, so this participates in the same
    real budget accounting as every other real call in the chain -- not a
    free/unaccounted side channel.
    """
    if entry["provider"] not in STREAMING_CAPABLE_PROVIDERS or entry["format"] != "openai_compat":
        raise ValueError(
            f"call_one_streaming: provider {entry['provider']!r} is not streaming-capable -- "
            "caller must use call_one() for this entry"
        )
    from quota_tracker import check_quota, record_call, clear_exhausted

    quota = check_quota(entry["provider"], entry["model"])
    if quota["status"] == "exhausted":
        detail = (
            f"real daily limit reached ({quota['used']}/{quota['limit']}) -- no request sent"
            if quota.get("limit") is not None
            else f"{quota.get('reason', 'marked exhausted')} -- no request sent"
        )
        return LLMFailure(entry["provider"], entry["model"], "quota_exhausted", detail)

    result = _call_openai_compat_streaming(
        entry["provider"], entry["base_url"], entry["model"], prompt, timeout,
        entry.get("max_tokens", 500), entry["tier"], system_prompt,
        on_reasoning_chunk, on_content_chunk,
    )
    record_call(
        entry["provider"], entry["model"], tokens=_extract_total_tokens(result),
        neurons=_extract_neurons(result), episode_id=episode_id,
    )
    if isinstance(result, LLMResult):
        clear_exhausted(entry["provider"], entry["model"])
    return result


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
    from quota_tracker import check_quota, record_call, clear_exhausted  # local import: avoids a hard dependency for any caller that only wants call_chain's pure LLM logic, e.g. a future unit test with no DB

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
    if isinstance(result, LLMResult):
        # Real self-heal (2026-08-1x fix): a confirmed real success is
        # strictly stronger evidence than any exhaustion mark's cooldown
        # timer -- clear it immediately rather than waiting for
        # is_marked_exhausted()'s next_probe_at to naturally elapse.
        # No-op if this provider/model was never marked.
        clear_exhausted(entry["provider"], entry["model"])
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
