// Shared helpers for the Model Scorecard tab (model_scorecard.json) --
// deliberately NOT hardcoding the fault-class roster anywhere here
// (Operator/index.jsx's AUTO_FIX_CLASSES const is a known-stale example
// of that trap, still only listing 3 of the real 6 auto-fix classes) --
// every list below is derived from the real published data.

// Short display labels for the real PROVIDER_CHAIN keys
// (model_backend.py) -- purely cosmetic, falls back to the raw
// "provider:model" key for anything not in this map so a future chain
// change never silently disappears from the UI.
const MODEL_LABELS = {
  "cloudflare:@cf/google/gemma-4-26b-a4b-it": "Gemma",
  "deepinfra:nvidia/Nemotron-3-Nano-30B-A3B": "Nemotron",
  "groq:openai/gpt-oss-120b": "Groq (gpt-oss-120b)",
  "groq:llama-3.3-70b-versatile": "Groq (Llama-3.3)",
  "openrouter:openai/gpt-oss-20b:free": "OpenRouter (gpt-oss-20b)",
};

export function modelLabel(key) {
  return MODEL_LABELS[key] ?? key;
}

// Real, explicit exclusion from every MODEL-level comparison on this
// page (model card table, confusion matrix, efficiency frontier,
// confidence variance / calibration) -- user's call, 2026-08-1x: not a
// real routing candidate, its ~6-9 episodes are leftover smoke-test
// volume, never real production use. Deliberately NOT excluded from
// the Fallback Funnel -- that panel is PROVIDER-level (tier_accuracy
// groups by "cloudflare"/"deepinfra"/"groq", not by exact model), so
// Llama's real 2 fallback episodes are structurally invisible there
// already (never labeled "Llama" to begin with) -- nothing to remove.
const EXCLUDED_MODEL_KEYS = new Set(["groq:llama-3.3-70b-versatile"]);

export function isExcludedModel(key) {
  return EXCLUDED_MODEL_KEYS.has(key);
}

export function visibleModelKeys(keyedObject) {
  return Object.keys(keyedObject ?? {}).filter((k) => !isExcludedModel(k));
}

// Real tier framing, per provider prefix -- Gemma/Nemotron are the real
// PROVIDER_CHAIN primaries, everything else is fallback-tier. Matches
// tier_accuracy's own real by_tier_provider grouping in the published
// data, just keyed for lookup by the full model key here.
export function isPrimaryModel(key) {
  return key.startsWith("cloudflare:") || key.startsWith("deepinfra:");
}

// Real per-model confidence source -- Gemma/Nemotron/Gemini/Kimi get
// real logprobs, Groq/OpenRouter/llama-3.3 fall back to self-reported
// (model_backend.py). Used to pick which half of a split
// (confidence_variance/calibration_error) is the real one for a given
// model, rather than the frontend guessing from whichever has n>0.
export function primaryConfidenceSource(key) {
  return key.startsWith("groq:") || key.startsWith("openrouter:") ? "self_reported" : "logprob";
}

// Real class roster derivable from the confusion matrix itself (union
// of every actual_class key seen across all models) -- never a
// hardcoded list that can drift from the real roster.
export function realClassesFromConfusionMatrix(confusionMatrix) {
  const set = new Set();
  for (const byActual of Object.values(confusionMatrix ?? {})) {
    for (const actualClass of Object.keys(byActual)) {
      if (actualClass !== "none") set.add(actualClass);
    }
  }
  return [...set].sort();
}

// Real 12-color categorical palette for the fault-class roster (the
// Efficiency Frontier's real encoding: color = class, shape = model --
// flipped from an earlier color-per-model version per explicit
// request). Fixed, distinguishable hues, assigned by sorted class name
// so the same class always gets the same color across renders/sessions
// -- not re-derived from theme tokens, since index.css only defines 5
// --chart-N colors, not enough for a real 12-class roster.
const CLASS_COLOR_PALETTE = [
  "#58a6ff", "#4ade80", "#fbbf24", "#f43f5e", "#a78bfa", "#2dd4bf",
  "#fb923c", "#f472b6", "#84cc16", "#38bdf8", "#e879f9", "#facc15",
];

export function classColor(sortedClasses, cls) {
  const idx = sortedClasses.indexOf(cls);
  return CLASS_COLOR_PALETTE[idx % CLASS_COLOR_PALETTE.length] ?? "#8b919d";
}

// Real shape-per-model encoding -- 4 clearly distinguishable marker
// shapes at small size (matches the standard matplotlib/d3 categorical
// marker set: circle/square/triangle/diamond, before star/cross would
// be needed for a 5th/6th model). Falls back to circle for any model
// not explicitly mapped, same "never silently disappear" fallback
// principle as modelLabel/MODEL_LABELS above.
const MODEL_SHAPES = {
  "cloudflare:@cf/google/gemma-4-26b-a4b-it": "circle",
  "deepinfra:nvidia/Nemotron-3-Nano-30B-A3B": "square",
  "groq:openai/gpt-oss-120b": "triangle",
  "openrouter:openai/gpt-oss-20b:free": "diamond",
};

export function modelShape(key) {
  return MODEL_SHAPES[key] ?? "circle";
}

// Real per-(model, class) accuracy, derived from confusion_matrix's own
// per-row counts (no new backend field needed) -- diagonal count over
// row total. Simple predicted===actualClass equality is correct here
// (unlike the backend's fuller _is_correct, which also treats a
// predicted "none"/"no anomaly detected" as matching an actual "none")
// because this is only ever called with a REAL fault class as
// actualClass, never the "none" control class -- that equivalence case
// can't occur here.
export function classAccuracy(confusionMatrix, modelKey, actualClass) {
  const row = confusionMatrix?.[modelKey]?.[actualClass];
  if (!row) return { correct: 0, total: 0, accuracy: null };
  const total = Object.values(row).reduce((s, n) => s + n, 0);
  const correct = row[actualClass] ?? 0;
  return { correct, total, accuracy: total ? correct / total : null };
}

export function allPredictedClasses(confusionMatrix) {
  const set = new Set();
  for (const byActual of Object.values(confusionMatrix ?? {})) {
    for (const byPredicted of Object.values(byActual)) {
      for (const predicted of Object.keys(byPredicted)) set.add(predicted);
    }
  }
  return set;
}

// Real σ(confidence) < 0.05 threshold, Kimi review 27's own explicit
// number ("a model with σ(confidence) < 0.05 should trigger a ⚠️ badge
// regardless of accuracy") -- n >= 20 gate added on top so a 2-3
// sample cohort (e.g. groq:llama-3.3-70b-versatile's self_reported
// n=6) doesn't get flagged off a statistically meaningless stdev.
export const LOW_VARIANCE_STDEV_THRESHOLD = 0.05;
export const LOW_VARIANCE_MIN_N = 20;

export function isLowVarianceFlag(varianceEntry) {
  if (!varianceEntry || varianceEntry.n < LOW_VARIANCE_MIN_N) return false;
  return varianceEntry.stdev != null && varianceEntry.stdev < LOW_VARIANCE_STDEV_THRESHOLD;
}
