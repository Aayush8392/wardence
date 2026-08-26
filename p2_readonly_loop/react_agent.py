"""
Phase H step 3: the real multi-turn ReAct evidence loop.

Design locked from `reviews/10_react_loop_design_kimi_review.md`, folded
in and adjusted this session (2026-07-30) after finding that review
already existed and had never been built against:
- Two-phase split: this file is Phase 1 (evidence-gathering + diagnosis)
  ONLY. Phase 2 (single-shot action proposal, gated by trust state) is
  explicitly NOT built here -- deferred, see the module docstring's
  bottom note.
- One plain-text JSON-in-prose prompt format, identical across every
  provider -- deliberately sidesteps the real Gemini-native-function-
  calling vs. Groq/OpenRouter-OpenAI-compatible-tools-schema fork Kimi
  flagged, by never using either provider's native tool-calling API at
  all. Full transcript re-sent as plain text each turn, no compaction
  (review's own reasoning: at a 5-turn cap this is ~2,500-3,000 tokens,
  cheaper than the debugging surface compaction would add).
- Episode-scoped provider lock, implemented for real: each provider-
  chain entry gets a FRESH transcript (no carryover), using
  model_backend.call_one() per turn, never call_chain(). A provider-
  level failure (timeout/rate-limit/bad-response, or 2 parse failures)
  aborts that provider's attempt and restarts the WHOLE episode from
  turn 1 against the next chain entry.
- Simplification beyond review 10, decided this session: tools are
  exposed to the LLM as ZERO-ARGUMENT choices (just "which tool", never
  "which target/namespace/service") -- the caller binds each tool to
  THIS episode's real target/namespace before ever offering it to the
  model. There is no real decision to make about *which* target to
  query (an episode always diagnoses one specific target), so asking
  the model to also supply parameters was pure hallucination surface
  with no corresponding real flexibility. This also means the tool-
  call-validator's parameter-schema machinery (built for REAL actions
  in tool_call_validator.py) doesn't apply here at all -- this loop
  only ever validates "is this a tool name that exists," nothing else.
- Max 5 turns (review 10's reasoning: >=2 independent signals for
  confusable pairs needs at least 2 real turns before diagnosing;
  every extra turn beyond ~5 is a billed call against thin free-tier
  quotas with no real evidence it improves convergence).

COMPARISON-ONLY BY CONSTRUCTION, not by a floor -- this module itself
has no import of trust_engine, actions.py, or ACTION_MAP, and returns a
plain dict for the CALLER to log wherever it likes (llm_diagnosis_log,
or a caller-side console comparison). It never decides whether its own
output drives a real action; that's the caller's job.

CORRECTED 2026-07-30/31 (this line used to gate on a 150-episode-per-
class floor -- that floor is DROPPED, see wardence_context.md's Model
Strategy section, "THE 150-EPISODE FLOOR IS DROPPED"). What actually
gates whether this module's output drives real production behavior now
is p3_trust_action/llm_trust_state.py's per-class diagnoser_mode
(stub/llm, promoted via 5-consecutive-correct background-comparison
streaks, reverted instantly on one miss) -- see p3_agent.py's /handle
for the real wiring.

NOT built here (Phase 2 IS built now, in a separate module):
- Phase 2 (single-shot action proposal) lives in action_proposer.py,
  not this file -- kept separate per Kimi review 12's structural
  finding (see that module's own docstring).
- query_logs as a fourth tool (review 10's own recommendation: ship the
  3-tool loop, validate it, add logs only if a specific class's real
  accuracy shows it's needed).
- Real per-episode budget accounting against the full 6-provider
  chain -- flagged as unconfirmed for multi-turn (this loop costs more
  real calls per episode than the single-shot replay test already
  measured); re-derive from real usage once this has actually run a
  handful of times, don't trust the old single-shot-derived estimate.
"""
import json
import os
from typing import Callable, Optional

from model_backend import (
    PROVIDER_CHAIN, STREAMING_CAPABLE_PROVIDERS, call_one, call_one_streaming, LLMFailure,
)

MAX_TURNS = 5

FAULT_CLASSES = [
    "crash-loop", "oom", "disk-full", "network-latency", "memory-leak",
    "connection-pool-exhaustion", "network-partition", "init-failure",
    "session-cart-failure", "cpu-throttling", "under-provisioned-replicas",
    "bad-rollout", "none",
]

# Real bug found live (2026-07-30, before running the full class roster):
# unlike llm_replay_test.py's single-shot PROMPT_TEMPLATE, this loop's
# prompt gave the model ZERO threshold guidance for the numeric-signal
# classes -- it happened to work for oom/crash-loop because those are
# list/boolean fields readable at face value, but network-latency/
# memory-leak/connection-pool-exhaustion/cpu-throttling/under-
# provisioned-replicas all depend on a real numeric cutoff the model was
# never told. Duplicated here by hand (same convention as FAULT_CLASSES
# above) rather than importing agent.py's real constants, since agent.py
# will import THIS module (to call run_react_diagnosis) -- importing
# back would be a circular import. Keep these in sync with agent.py's
# own HIGH_LATENCY_THRESHOLD_MS/MEMORY_LEAK_THRESHOLD_MIB/
# CONNECTION_POOL_THRESHOLD/NETWORK_PARTITION_MAX_THROUGHPUT_BPS/
# NETWORK_PARTITION_LATENCY_SATURATION_MS/CPU_THROTTLE_INCREASE_THRESHOLD/
# UNDER_PROVISIONED_PROBE_THRESHOLD_MS by hand if any of those ever change.
NETWORK_PARTITION_MAX_THROUGHPUT_BPS = int(os.environ.get("NETWORK_PARTITION_MAX_THROUGHPUT_BPS", "200"))

FIELD_GUIDANCE = f"""Field meanings and thresholds (a null/false/empty field means that signal did not fire):
- oom_pods: non-empty -> oom.
- evicted_pods: non-empty -> disk-full.
- crashlooping_pods: non-empty (and oom_pods/evicted_pods/front_end_image_pull_failing all empty/false) -> crash-loop. Check front_end_image_pull_failing (below) BEFORE concluding crash-loop -- a bad-rollout episode's own image-reset step can leave residual restart activity on front-end that satisfies this signal even when nothing is actually crash-looping; front_end_image_pull_failing is the more specific, direct signal and wins.
- p95_latency_ms (orders only), if NOT null: >= 10000ms -> network-partition (a request hanging until client timeout, not organic latency -- the real network-latency mechanism only ever injects 500ms+jitter, so nothing that high can be real latency). >= 300ms and < 10000ms -> network-latency. Check p95_latency_ms BEFORE combined_throughput_bps below when p95_latency_ms is present -- confirmed via real production data (Kimi review 20) that combined_throughput_bps alone cannot reliably distinguish these two classes (real network-partition and real network-latency episodes' throughput readings genuinely overlap), but a present, non-null p95_latency_ms in either band above is a clean, reliable signal -- across all real ground-truth network-partition episodes checked, none ever showed a mid-range p95 value.
- combined_throughput_bps (orders only): < {NETWORK_PARTITION_MAX_THROUGHPUT_BPS} bytes/s -> network-partition, but ONLY if p95_latency_ms above is null or did not already give you an answer. This is a weaker, fallback signal, not a primary one, for this specific pair of classes.
- payment_stuck_not_ready: true -> init-failure.
- session_db_replicas_hit_zero: true -> session-cart-failure.
- heap_rise_kb (shipping only): >= 20000 KB (20 MiB) above the episode's own captured pre-injection heap floor -> memory-leak.
- peak_threads_connected (catalogue-db only): >= 100 -> connection-pool-exhaustion.
- cpu_throttle_periods_increase (user only): >= 100 -> cpu-throttling.
- front_end_image_pull_failing: true -> bad-rollout.
- catalogue_probe_p95_ms (from probe_catalogue_capacity): >= 130ms -> under-provisioned-replicas, but ONLY after you have already called query_prometheus at least once this episode and confirmed oom_pods/crashlooping_pods/evicted_pods are all empty. A degraded or dying catalogue pod (oom, crash-loop, disk-full) can ALSO show an elevated capacity-probe reading right before it's killed -- oom_pods/crashlooping_pods/evicted_pods are the more specific, direct signals and win. Never diagnose under-provisioned-replicas from catalogue_probe_p95_ms alone without having called query_prometheus first this episode.
- dl_detector_result.is_anomalous (from call_dl_detector) on catalogue, with no other signal above having fired: this is NOT enough on its own to conclude "none" -- you MUST call probe_catalogue_capacity (if you have not already this episode) before concluding anything, since it is the one signal that can actually confirm or rule out under-provisioned-replicas. Only after probe_catalogue_capacity has been called and its result checked against the threshold above may you fall back to the closest matching class or "none". "log-anomaly detected (unclassified)" is NOT a valid diagnosis for you to output.
- If NONE of the above are met, diagnosis is "none"."""

# Fields checked for a real, non-subjective "you already have an
# unambiguous signal" shortcut -- see _has_strong_signal below. List/
# boolean fields only (never the numeric-threshold ones, which
# genuinely need the model's own threshold comparison against
# FIELD_GUIDANCE above, not a code-side shortcut).
_STRONG_SIGNAL_FIELDS = {
    "oom_pods", "evicted_pods", "crashlooping_pods",
    "payment_stuck_not_ready", "session_db_replicas_hit_zero", "front_end_image_pull_failing",
}

# Real bug found and fixed 2026-08-05 (diag_predicts_none_pattern.py +
# diag_predicts_none_reasoning.py, run against the live DB): 5 real
# episodes across under-provisioned-replicas/cpu-throttling/session-
# cart-failure/network-partition were all misdiagnosed "none" by the
# SAME provider (nvidia/Nemotron-3-Nano-30B-A3B via DeepInfra), costing
# 3 real Dimension A demotions (incl. a 29-episode streak on
# cpu-throttling) and 5 Dimension B demotions. In 4 of the 5, the
# decisive numeric field was ALREADY PRESENT in the tool result and
# already past its documented FIELD_GUIDANCE threshold (e.g.
# catalogue_probe_p95_ms=296.89 vs. the 130ms cutoff,
# cpu_throttle_periods_increase=602.96 vs. the 100 cutoff) -- the model
# had the decisive number and still failed to apply the threshold
# comparison correctly. This is the exact failure shape the numeric
# fields were deliberately left OUT of _has_strong_signal for (see
# comment above) on the theory the model's own comparison was reliable
# enough -- real data now shows it isn't always. Fix: compute the
# threshold comparison in code (same numbers FIELD_GUIDANCE already
# states in prose) and, when crossed, tell the model the concluded
# diagnosis directly instead of leaving the arithmetic to it. Kept
# deliberately narrow -- only single-threshold, single-diagnosis fields
# with no other class contending for the same signal. p95_latency_ms/
# combined_throughput_bps (network-latency vs. network-partition) are
# NOT included here: that pair already has its own recently-tuned,
# accepted-limitation logic (2026-08-03 session, Kimi review 20) and
# touching it again isn't warranted by this specific finding.
_NUMERIC_THRESHOLD_FIELDS = {
    # field: (threshold, diagnosis)
    "cpu_throttle_periods_increase": (100, "cpu-throttling"),
    "catalogue_probe_p95_ms": (130, "under-provisioned-replicas"),
    "heap_rise_kb": (20000, "memory-leak"),
    "peak_threads_connected": (100, "connection-pool-exhaustion"),
}


def _numeric_threshold_hit(tool_name: str, observation) -> tuple[str, float, float, str] | None:
    """
    Returns (field, value, threshold, diagnosis) if `observation` contains
    a numeric field from _NUMERIC_THRESHOLD_FIELDS whose value is already
    >= its documented threshold -- the numeric-field counterpart to
    _has_strong_signal, added 2026-08-05 (see comment above). Only
    query_prometheus/probe_catalogue_capacity results are checked (same
    scope as the tools these fields actually come from).
    """
    if not isinstance(observation, dict):
        return None
    for field, (threshold, diagnosis) in _NUMERIC_THRESHOLD_FIELDS.items():
        value = observation.get(field)
        if isinstance(value, (int, float)) and value >= threshold:
            return field, value, threshold, diagnosis
    return None

SYSTEM_TEMPLATE = """You are an SRE agent diagnosing a fault in a Kubernetes cluster running Sock Shop (target={target}, namespace={namespace}).
You have {turns_left} turn(s) remaining.

Available tools (call one at a time, no parameters needed -- each is already scoped to this episode's real target):
{tools_desc}

Fault classes (choose EXACTLY one): {classes}

{field_guidance}

Rules:
1. Respond with EXACTLY ONE JSON object. No markdown, no text outside the JSON.
2. To call a tool: {{"action": "call_tool", "tool": "<tool name>"}}
3. To give your final answer: {{"action": "diagnose", "diagnosis": "<one of the classes above>", "confidence": <0-1>, "reasoning": "<one sentence>"}}
4. {evidence_rule}
5. If genuinely ambiguous after gathering evidence, diagnose "none" with confidence < 0.6.

TRANSCRIPT:
{transcript}
[Turn {turn_num}]
Action:"""


def _tool_desc(tools: dict) -> str:
    return "\n".join(f"- {name}" for name in tools)


def _evidence_rule(tools: dict) -> str:
    # Real bug found live (2026-07-30, crash-loop test): with only ONE
    # tool available (e.g. carts has no DL-detector/capacity-probe
    # coverage), a flat "gather >=2 signals" rule forced the model to
    # call the SAME tool twice for zero new information, burning a real
    # turn/quota for nothing -- it still converged correctly, just
    # wastefully. Fixed: only ask for a second signal when a second,
    # genuinely DISTINCT tool actually exists to call.
    if len(tools) <= 1:
        return "Diagnose as soon as you have one tool result -- there is only one real signal available for this target."
    return "Gather at least 2 independent tool results before diagnosing, unless one result is completely unambiguous."


def _has_strong_signal(tool_name: str, observation) -> bool:
    """
    True if `observation` (query_prometheus's result) already contains a
    non-empty list or a `true` boolean on one of the deterministic,
    class-defining fields -- the SAME fields stub_diagnose checks first,
    before any of the threshold/numeric ones. This is a code-side,
    non-subjective check (not left to the model to judge "am I done
    yet"), added specifically to stop a real observed waste: the oom
    test called probe_catalogue_capacity (a real k6 burst against the
    live cluster) as a "second signal" even though oom_pods from turn 1
    was already conclusive.
    """
    if tool_name != "query_prometheus" or not isinstance(observation, dict):
        return False
    return any(observation.get(field) for field in _STRONG_SIGNAL_FIELDS)


def _run_episode_with_provider(
    entry: dict, target: str, namespace: str, tools: dict, episode_id: str | None = None,
    on_event=None,
) -> dict:
    """
    One full attempt of the evidence loop against a SINGLE provider-chain
    entry, fresh transcript, no carryover from any prior provider. Returns
    a dict with status in {"diagnosed", "max_turns_exceeded",
    "invalid_tool_name", "provider_failure"}.

    on_event: optional callable(dict), added 2026-08-1x for the live
    Operator "Central Thinking Hub" widget. None everywhere else (every
    batch-run/comparison-sampling caller) -- purely additive, zero
    behavior change when omitted. Fires real events only, never
    synthetic/fabricated ones: "provider_attempt" once per chain entry
    tried (the real handoff moment), "turn_start" once per ReAct turn,
    and "reasoning_chunk" carrying real streamed reasoning text (only for
    STREAMING_CAPABLE_PROVIDERS; for other providers, at most one
    "reasoning_chunk" fires post-hoc if the provider's own response
    happened to include reasoning text, never fabricated).
    """
    if on_event:
        on_event({"type": "provider_attempt", "provider": entry["provider"],
                   "model": entry["model"], "tier": entry["tier"]})

    transcript_lines = []
    parse_failures = 0
    called_tools = set()
    # Real structured tool results, 2026-08-01 addition -- transcript_lines
    # is human-readable only (formatted strings), which forced any caller
    # wanting an actual gathered value (e.g. propose_action() needing the
    # real catalogue_probe_p95_ms the loop already fetched) to either
    # parse transcript text or re-invoke a real, non-free tool a second
    # time (probe_catalogue_capacity fires an actual k6 burst -- see its
    # own docstring on why it's deliberately not called eagerly). This
    # dict is the clean alternative: reuse what was already gathered.
    observations = {}

    for turn_num in range(1, MAX_TURNS + 1):
        if on_event:
            on_event({"type": "turn_start", "turn": turn_num})
        prompt = SYSTEM_TEMPLATE.format(
            target=target, namespace=namespace,
            turns_left=MAX_TURNS - turn_num + 1,
            tools_desc=_tool_desc(tools),
            classes=", ".join(FAULT_CLASSES),
            field_guidance=FIELD_GUIDANCE,
            evidence_rule=_evidence_rule(tools),
            transcript="\n".join(transcript_lines) if transcript_lines else "(none yet)",
            turn_num=turn_num,
        )
        # Real streaming path, live Operator widget only (on_event set) --
        # streams the SAME single real call this turn would make anyway,
        # never a second parallel call. Every other caller (batch runs,
        # comparison sampling, on_event=None) is completely unaffected.
        if on_event and entry["provider"] in STREAMING_CAPABLE_PROVIDERS and entry["format"] == "openai_compat":
            result = call_one_streaming(
                entry, prompt, timeout=30, episode_id=episode_id,
                on_reasoning_chunk=lambda chunk: on_event({"type": "reasoning_chunk", "text": chunk}),
            )
        else:
            result = call_one(entry, prompt, timeout=30, episode_id=episode_id)
            if on_event and not isinstance(result, LLMFailure):
                # Best-effort, real-data-only post-hoc reasoning surface for
                # a non-streaming-capable provider (e.g. gemini has none;
                # deepinfra/nemotron always does -- confirmed live via
                # check_stream_confidence_source.py). Never fabricated --
                # only fires if the provider's own raw response actually
                # included this field. Real bug found+fixed via that same
                # live test: the openai_compat shape nests this field at
                # raw["choices"][0]["message"]["reasoning_content"], NOT
                # top-level raw["reasoning_content"] (the first version of
                # this line always read None for every real openai_compat
                # provider -- confirmed live, 0 chunks seen for nemotron).
                reasoning_text = None
                if isinstance(result.raw, dict):
                    choices = result.raw.get("choices") or []
                    if choices:
                        reasoning_text = choices[0].get("message", {}).get("reasoning_content")
                if reasoning_text:
                    on_event({"type": "reasoning_chunk", "text": reasoning_text})
        if isinstance(result, LLMFailure):
            return {"status": "provider_failure", "detail": result, "transcript": transcript_lines}

        parsed = result.parsed
        action = parsed.get("action") if parsed else None

        if action == "diagnose":
            return {
                "status": "diagnosed", "result": result, "parsed": parsed,
                "turns_used": turn_num, "transcript": transcript_lines, "observations": observations,
            }

        if action == "call_tool":
            tool_name = parsed.get("tool")
            if tool_name not in tools:
                return {
                    "status": "invalid_tool_name",
                    "detail": f"model called non-existent tool {tool_name!r}",
                    "turns_used": turn_num, "transcript": transcript_lines, "observations": observations,
                }
            try:
                observation = tools[tool_name]()
            except Exception as e:  # real tool execution failure -- feed back, don't crash the loop
                observation = {"error": str(e)}
            observations[tool_name] = observation
            transcript_lines.append(f"[Turn {turn_num}] Action: call_tool {tool_name}")
            transcript_lines.append(f"Result: {json.dumps(observation, default=str)}")
            called_tools.add(tool_name)
            numeric_hit = _numeric_threshold_hit(tool_name, observation)
            if _has_strong_signal(tool_name, observation):
                transcript_lines.append(
                    "System: The result above already contains an unambiguous signal "
                    "(a non-empty list or a true flag on a class-defining field). "
                    "You have enough evidence -- diagnose now."
                )
            elif numeric_hit:
                field, value, threshold, diagnosis = numeric_hit
                transcript_lines.append(
                    f"System: {field}={value} is >= the {threshold} threshold in the guidance "
                    f"above -- this means diagnosis={diagnosis}. You have enough evidence -- "
                    "diagnose now."
                )
            elif called_tools == set(tools):
                transcript_lines.append(
                    "System: You have now called every available tool at least once -- "
                    "calling any of them again will return the same result. Diagnose now."
                )
            continue

        # Neither "diagnose" nor "call_tool" -- unparseable JSON, missing
        # "action", or an unrecognized action value. Treated identically:
        # one retry with a format reminder (counts as a turn, correct
        # incentive per review 10), a second failure aborts this
        # provider's attempt (at temperature=0 a repeat failure is a
        # real capability mismatch, not noise -- switch providers).
        parse_failures += 1
        if parse_failures >= 2:
            return {
                "status": "provider_failure",
                "detail": LLMFailure(entry["provider"], entry["model"], "parse_failure",
                                      "two unparseable/invalid-schema responses in one episode attempt"),
                "transcript": transcript_lines,
            }
        transcript_lines.append(f"[Turn {turn_num}] Action: (unparseable or invalid-schema response)")
        transcript_lines.append(
            'System: Your response could not be parsed. Respond with EXACTLY one JSON object -- '
            '{"action": "call_tool", "tool": "..."} or {"action": "diagnose", "diagnosis": "...", '
            '"confidence": <0-1>, "reasoning": "..."}. No markdown, no other text. Try again.'
        )

    return {"status": "max_turns_exceeded", "transcript": transcript_lines, "observations": observations}


def run_react_diagnosis(
    target: str, namespace: str, tools: dict[str, Callable[[], object]],
    chain: list[dict] | None = None, episode_id: str | None = None,
    on_event=None,
) -> dict:
    """
    tools: {tool_name: zero_arg_callable} -- the caller's own real tool
    functions, each already bound (via lambda/functools.partial) to THIS
    episode's real target/namespace, e.g.:
        {"query_prometheus": lambda: query_prometheus(target, namespace),
         "call_dl_detector": lambda: call_dl_detector(target)}
    Injected rather than imported, so this module has zero import-time
    dependency on agent.py/p3_agent.py and can never create a circular
    import wiring it into either.

    `chain` defaults to the real, locked PROVIDER_CHAIN -- override only
    for a deliberate calibration/test call that needs to force a
    SPECIFIC provider (e.g. test_react_agent.py's --force-provider),
    never for a real production episode, which must always see the full
    real fallback chain.

    Returns a plain dict, always with a "status" key
    ("diagnosed" | "max_turns_exceeded" | "invalid_tool_name" | "llm_unavailable").
    Never raises on a normal LLM/tool failure. COMPARISON-ONLY -- see
    this module's docstring; the caller decides where this result gets
    logged and must not feed it into trust_engine/ACTION_MAP.

    on_event: optional callable(dict), threaded straight through to every
    _run_episode_with_provider() attempt -- see that function's own
    docstring for the real event shapes. None (every caller except the
    live Operator widget) is a no-op, zero behavior change.
    """
    failed_attempts = []
    for entry in (chain if chain is not None else PROVIDER_CHAIN):
        attempt = _run_episode_with_provider(entry, target, namespace, tools, episode_id=episode_id, on_event=on_event)

        if attempt["status"] == "diagnosed":
            parsed, result = attempt["parsed"], attempt["result"]
            return {
                "status": "diagnosed",
                "llm_diagnosis": parsed.get("diagnosis"),
                "llm_confidence": result.confidence,
                "llm_confidence_source": result.confidence_source,
                "llm_reasoning": parsed.get("reasoning"),
                "provider": result.provider, "model": result.model, "tier": result.tier,
                "llm_version_fingerprint": result.version_fingerprint,
                "turns_used": attempt["turns_used"],
                "transcript": attempt["transcript"],
                "failed_attempts": failed_attempts,
                # Real structured tool results gathered during THIS
                # diagnosis attempt (e.g. catalogue_probe_p95_ms) -- lets
                # a caller like propose_action() reuse real data the loop
                # already fetched instead of re-invoking a non-free tool
                # (probe_catalogue_capacity fires a real k6 burst).
                "observations": attempt.get("observations", {}),
            }
        if attempt["status"] in ("invalid_tool_name", "max_turns_exceeded"):
            return {
                "status": attempt["status"], "llm_diagnosis": None,
                "detail": attempt.get("detail"), "transcript": attempt["transcript"],
                "failed_attempts": failed_attempts,
                "observations": attempt.get("observations", {}),
            }
        # "provider_failure" -- abort this provider's attempt, retry the
        # WHOLE episode from turn 1 against the next chain entry. Fresh
        # transcript next loop iteration (no carryover by construction,
        # since transcript_lines is local to _run_episode_with_provider).
        failed_attempts.append(attempt["detail"])

    return {"status": "llm_unavailable", "llm_diagnosis": None, "failed_attempts": failed_attempts}
