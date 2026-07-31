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
from typing import Callable, Optional

from model_backend import PROVIDER_CHAIN, call_one, LLMFailure

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
FIELD_GUIDANCE = """Field meanings and thresholds (a null/false/empty field means that signal did not fire):
- oom_pods: non-empty -> oom.
- evicted_pods: non-empty -> disk-full.
- crashlooping_pods: non-empty (and oom_pods/evicted_pods empty) -> crash-loop.
- combined_throughput_bps (orders only): < 200 bytes/s -> network-partition (check before either latency check below).
- p95_latency_ms (orders only): >= 10000ms -> network-partition, NOT network-latency (a request hanging until client timeout, not organic latency -- the real network-latency mechanism only ever injects 500ms+jitter, so nothing that high can be real latency). Check this BEFORE the network-latency check below.
- p95_latency_ms (orders only): >= 300ms (and < 10000ms) -> network-latency.
- payment_stuck_not_ready: true -> init-failure.
- session_db_replicas_hit_zero: true -> session-cart-failure.
- peak_memory_mib (shipping only): >= 380 MiB -> memory-leak.
- peak_threads_connected (catalogue-db only): >= 100 -> connection-pool-exhaustion.
- cpu_throttle_periods_increase (user only): >= 100 -> cpu-throttling.
- front_end_image_pull_failing: true -> bad-rollout.
- catalogue_probe_p95_ms (from probe_catalogue_capacity): >= 200ms -> under-provisioned-replicas.
- dl_detector_result.is_anomalous (from call_dl_detector): true, with no other signal above having fired -> "log-anomaly detected (unclassified)" is NOT a valid diagnosis for you to output -- treat it as a hint only; if it's the only thing that fired, diagnose the closest matching class if any field pattern suggests one, otherwise "none".
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


def _run_episode_with_provider(entry: dict, target: str, namespace: str, tools: dict) -> dict:
    """
    One full attempt of the evidence loop against a SINGLE provider-chain
    entry, fresh transcript, no carryover from any prior provider. Returns
    a dict with status in {"diagnosed", "max_turns_exceeded",
    "invalid_tool_name", "provider_failure"}.
    """
    transcript_lines = []
    parse_failures = 0
    called_tools = set()

    for turn_num in range(1, MAX_TURNS + 1):
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
        result = call_one(entry, prompt, timeout=30)
        if isinstance(result, LLMFailure):
            return {"status": "provider_failure", "detail": result, "transcript": transcript_lines}

        parsed = result.parsed
        action = parsed.get("action") if parsed else None

        if action == "diagnose":
            return {
                "status": "diagnosed", "result": result, "parsed": parsed,
                "turns_used": turn_num, "transcript": transcript_lines,
            }

        if action == "call_tool":
            tool_name = parsed.get("tool")
            if tool_name not in tools:
                return {
                    "status": "invalid_tool_name",
                    "detail": f"model called non-existent tool {tool_name!r}",
                    "turns_used": turn_num, "transcript": transcript_lines,
                }
            try:
                observation = tools[tool_name]()
            except Exception as e:  # real tool execution failure -- feed back, don't crash the loop
                observation = {"error": str(e)}
            transcript_lines.append(f"[Turn {turn_num}] Action: call_tool {tool_name}")
            transcript_lines.append(f"Result: {json.dumps(observation, default=str)}")
            called_tools.add(tool_name)
            if _has_strong_signal(tool_name, observation):
                transcript_lines.append(
                    "System: The result above already contains an unambiguous signal "
                    "(a non-empty list or a true flag on a class-defining field). "
                    "You have enough evidence -- diagnose now."
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

    return {"status": "max_turns_exceeded", "transcript": transcript_lines}


def run_react_diagnosis(target: str, namespace: str, tools: dict[str, Callable[[], object]]) -> dict:
    """
    tools: {tool_name: zero_arg_callable} -- the caller's own real tool
    functions, each already bound (via lambda/functools.partial) to THIS
    episode's real target/namespace, e.g.:
        {"query_prometheus": lambda: query_prometheus(target, namespace),
         "call_dl_detector": lambda: call_dl_detector(target)}
    Injected rather than imported, so this module has zero import-time
    dependency on agent.py/p3_agent.py and can never create a circular
    import wiring it into either.

    Returns a plain dict, always with a "status" key
    ("diagnosed" | "max_turns_exceeded" | "invalid_tool_name" | "llm_unavailable").
    Never raises on a normal LLM/tool failure. COMPARISON-ONLY -- see
    this module's docstring; the caller decides where this result gets
    logged and must not feed it into trust_engine/ACTION_MAP.
    """
    failed_attempts = []
    for entry in PROVIDER_CHAIN:
        attempt = _run_episode_with_provider(entry, target, namespace, tools)

        if attempt["status"] == "diagnosed":
            parsed, result = attempt["parsed"], attempt["result"]
            return {
                "status": "diagnosed",
                "llm_diagnosis": parsed.get("diagnosis"),
                "llm_confidence": result.confidence,
                "llm_confidence_source": result.confidence_source,
                "llm_reasoning": parsed.get("reasoning"),
                "provider": result.provider, "model": result.model, "tier": result.tier,
                "turns_used": attempt["turns_used"],
                "transcript": attempt["transcript"],
                "failed_attempts": failed_attempts,
            }
        if attempt["status"] in ("invalid_tool_name", "max_turns_exceeded"):
            return {
                "status": attempt["status"], "llm_diagnosis": None,
                "detail": attempt.get("detail"), "transcript": attempt["transcript"],
                "failed_attempts": failed_attempts,
            }
        # "provider_failure" -- abort this provider's attempt, retry the
        # WHOLE episode from turn 1 against the next chain entry. Fresh
        # transcript next loop iteration (no carryover by construction,
        # since transcript_lines is local to _run_episode_with_provider).
        failed_attempts.append(attempt["detail"])

    return {"status": "llm_unavailable", "llm_diagnosis": None, "failed_attempts": failed_attempts}
