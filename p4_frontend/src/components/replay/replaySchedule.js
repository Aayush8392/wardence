// Pure, no React. Builds the real replay timeline for one episode -- nothing
// here fetches or fabricates data, it only decides ORDER and DURATION for
// content that already exists on the episode/trustEntry objects. Total
// duration is a straight sum of unit durations, proportional to how much
// this specific episode actually has (locked: "purely presentational,
// never claims to represent real elapsed episode time").
//
// Fixed content-shape rules (locked, not decided per class/episode):
//   instant  -- header only. Never fades, always fully visible immediately.
//   fade     -- a fact/tile-batch with nothing to "read": fades in as a
//               whole, no per-character reveal.
//   fade-typewriter -- a real prose string: the container fades in over a
//               short lead-in, then the text types in beneath it. A unit
//               with no real text (e.g. an enriched turn with no Result
//               line) degrades to a plain fade over its full duration.
import { FAULT_CLASS_RELEVANT_FIELDS } from "./EvidenceGrid";
import { computeTrustContext } from "./TrustContext";

const CHARS_PER_SECOND = 20;
const MIN_TYPEWRITER_S = 1.1;
const HEADER_S = 0.8; // header only -- the one instant exception
const FADE_RAMP_S = 0.9; // how long the opacity ramp itself takes (slowed down, was 0.35)
const GAP_S = 0.7; // real settle/pause AFTER a unit visually finishes, before the next one starts
const FADE_ONLY_S = FADE_RAMP_S + GAP_S; // plain fade unit: ramp, then hold, then next section begins

function typewriterDuration(text) {
  return Math.max(MIN_TYPEWRITER_S, text.length / CHARS_PER_SECOND);
}

// A fade-typewriter unit with no real text still needs a real duration --
// same length as a plain fade unit. One with text gets ramp + typing + the
// same real gap before the next section starts, so a slow reader (or a
// paused scrub) always has visible breathing room between sections instead
// of one fade finishing exactly as the next one begins.
function fadeTypewriterDuration(text) {
  if (!text) return FADE_ONLY_S;
  return FADE_RAMP_S + typewriterDuration(text) + GAP_S;
}

// Same lite-tier sentence split ReasoningStream.jsx already uses -- not
// re-deriving a different rule, just reusing the real one.
function splitReasoning(text) {
  return (text ?? "")
    .split(/(?<=[.!])\s+|\n+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 2);
}

// Same real transcript_lines parsing as ReasoningSequence.jsx (Snapshot's
// enriched-tier renderer) -- including the Result/Observe line, which the
// first Replay pass dropped entirely. Not re-deriving a different rule.
function parseTranscriptTurns(lines) {
  const turns = [];
  let current = null;
  for (const line of lines) {
    const m = line.match(/^\[Turn (\d+)\] Action: (.*)$/);
    if (m) {
      if (current) turns.push(current);
      current = { turn: Number(m[1]), action: m[2], extra: [] };
    } else if (current) {
      current.extra.push(line);
    }
  }
  if (current) turns.push(current);
  return turns.map((t) => ({
    turn: t.turn,
    action: t.action,
    resultText: t.extra.find((l) => l.startsWith("Result:"))?.replace(/^Result:\s*/, "") ?? null,
  }));
}

export function buildReplaySchedule(episode, trustEntry) {
  const units = [];
  let t = 0;
  function push(unit) {
    units.push({ ...unit, start: t, end: t + unit.duration, textStart: unit.textStart ?? 0 });
    t += unit.duration;
  }

  push({ id: "header", kind: "instant", duration: HEADER_S, section: "header" });
  push({ id: "context", kind: "fade", duration: FADE_ONLY_S, section: "context" });

  // Reasoning: real turn-by-turn for enriched episodes (real transcript),
  // falling back to the same 2-sentence lite split ReasoningStream uses --
  // never a different, invented structure per tier. Each turn's own real
  // Result text (when present) types in beneath its action label, instead
  // of the whole turn just popping in as one static block.
  if (episode.transcript && episode.transcript.length > 0) {
    const turns = parseTranscriptTurns(episode.transcript);
    turns.forEach((turn, i) => {
      push({
        id: `turn-${i}`,
        kind: "fade-typewriter",
        duration: fadeTypewriterDuration(turn.resultText),
        textStart: FADE_RAMP_S,
        section: "reasoning-turn",
        text: turn.resultText,
        data: { turn: turn.turn, action: turn.action },
      });
    });
    const decision = (episode.reasoning ?? "").trim();
    if (decision) {
      push({
        id: "decision", kind: "fade-typewriter", duration: fadeTypewriterDuration(decision),
        textStart: FADE_RAMP_S, section: "reasoning-decision", text: decision,
      });
    }
  } else {
    const sentences = splitReasoning(episode.reasoning);
    sentences.forEach((s, i) => {
      push({
        id: `sentence-${i}`, kind: "fade-typewriter", duration: fadeTypewriterDuration(s),
        textStart: FADE_RAMP_S, section: "reasoning-sentence", text: s,
        label: i === 0 ? "OBSERVATION" : "HYPOTHESIS",
      });
    });
  }

  // Evidence: primary tile(s) as one combined fade, secondary strip as a
  // second, separate fade -- matches the real primary/secondary split
  // EvidenceGrid itself already draws, not a new distinction.
  const toolOutput = episode.tool_output;
  if (toolOutput) {
    const relevantKeys = new Set(FAULT_CLASS_RELEVANT_FIELDS[episode.predicted_class] ?? []);
    const presentKeys = Object.keys(toolOutput).filter((k) => toolOutput[k] !== null && toolOutput[k] !== undefined);
    const hasPrimary = presentKeys.some((k) => relevantKeys.has(k));
    const hasSecondary = presentKeys.some((k) => !relevantKeys.has(k));
    if (hasPrimary) push({ id: "evidence-primary", kind: "fade", duration: FADE_ONLY_S, section: "evidence-primary" });
    if (hasSecondary) push({ id: "evidence-secondary", kind: "fade", duration: FADE_ONLY_S, section: "evidence-secondary" });
  }

  // Action Taken -- real progress_log steps if present, one unit each
  // (mirrors ExecutionPhaseTrack's own existing revealCount granularity),
  // each step's own real `detail` text (when present) types in beneath it.
  // Skipped entirely if no action was taken, never shown empty.
  if (episode.scores_action_taken) {
    const steps = episode.action_result?.progress_log;
    if (steps && steps.length > 0) {
      steps.forEach((step, i) => {
        push({
          id: `action-step-${i}`, kind: "fade-typewriter", duration: fadeTypewriterDuration(step.detail),
          textStart: FADE_RAMP_S, section: "action-step", text: step.detail ?? null,
          data: { revealCount: i + 1 },
        });
      });
    } else {
      push({ id: "action-step-0", kind: "fade", duration: FADE_ONLY_S, section: "action-step", data: { revealCount: 1 } });
    }
  }

  if (episode.gate_substitution) {
    const reason = episode.gate_substitution.reason ?? "";
    push({
      id: "gate-substitution", kind: "fade-typewriter", duration: fadeTypewriterDuration(reason),
      textStart: FADE_RAMP_S, section: "gate-substitution", text: reason || null,
    });
  }

  if (trustEntry) {
    const computed = computeTrustContext(trustEntry);
    const body = computed?.body ?? "";
    push({
      id: "trust-context", kind: "fade-typewriter", duration: fadeTypewriterDuration(body),
      textStart: FADE_RAMP_S, section: "trust-context", text: body || null,
    });
  }

  push({ id: "durability", kind: "fade", duration: FADE_ONLY_S, section: "durability" });

  return { units, totalDuration: t };
}

// Real, continuous 0..1 values -- pure functions of elapsed time, same
// result whether reached by playing forward or dragging backward (which is
// what makes un-fade/un-type on scrub-back "just work" with no special
// reverse-direction logic anywhere).
export function fadeProgress(unit, elapsed) {
  if (!unit) return 0;
  if (unit.kind === "instant") return 1;
  // Real fix: the ramp only ever takes FADE_RAMP_S, regardless of the
  // unit's total duration -- previously a plain "fade" unit's ramp was
  // stretched across its ENTIRE slot (duration = FADE_ONLY_S = ramp + gap),
  // so it finished fading in exactly as the next section started, with no
  // visible pause. The gap now genuinely holds at full opacity instead.
  const leadTime = unit.kind === "fade-typewriter" ? unit.textStart : FADE_RAMP_S;
  if (elapsed <= unit.start) return 0;
  if (elapsed >= unit.start + leadTime) return 1;
  return leadTime > 0 ? (elapsed - unit.start) / leadTime : 1;
}

export function textProgress(unit, elapsed) {
  if (!unit || !unit.text) return 0;
  const textStartAbs = unit.start + unit.textStart;
  if (elapsed <= textStartAbs) return 0;
  if (elapsed >= unit.end) return 1;
  const span = unit.end - textStartAbs;
  return span > 0 ? (elapsed - textStartAbs) / span : 1;
}

export function typedText(unit, elapsed) {
  if (!unit?.text) return "";
  const count = Math.round(unit.text.length * textProgress(unit, elapsed));
  return unit.text.slice(0, count);
}
