// Zone 2 read side: the SPA fetches static JSON published by
// p3_trust_action/publish_to_r2.py at runtime -- no live DB query, per the
// locked architecture ("site builds ONCE"). Base URL is the bucket's public
// r2.dev dev URL (read-only; write access uses separate, private credentials
// that never touch the frontend).
const BASE_URL = import.meta.env.VITE_R2_BASE_URL;

async function fetchJson(path) {
  if (!BASE_URL) {
    throw new Error("VITE_R2_BASE_URL is not set (check p4_frontend/.env)");
  }
  // no-store: this data changes over time (publish_to_r2.py re-uploads the
  // full file on every run) and the browser must never serve a stale cached
  // copy -- also avoids the browser hanging onto a cached failed
  // request/CORS preflight from before the bucket's CORS policy existed.
  const res = await fetch(`${BASE_URL}/${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`Failed to fetch ${path}: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

// "none" is the internal negative-control class (episodes with no injected
// fault, used to check the agent doesn't hallucinate anomalies) -- not one
// of the real 8 taxonomy classes, so it's excluded from anything shown
// publicly as a "fault class."
const INTERNAL_ONLY_CLASSES = new Set(["none"]);

export async function fetchTrustLadder() {
  const rows = await fetchJson("trust_ladder.json");
  return rows.filter((r) => !INTERNAL_ONLY_CLASSES.has(r.fault_class));
}

export async function fetchTrustHistory() {
  return fetchJson("trust_history.json");
}

export async function fetchEpisodes() {
  const episodes = await fetchJson("episodes.json");
  return episodes.filter((e) => !INTERNAL_ONLY_CLASSES.has(e.fault_class));
}

export async function fetchSystemStatus() {
  return fetchJson("system_status.json");
}

export async function fetchModelScorecard() {
  return fetchJson("model_scorecard.json");
}

// Comparison-only models (Groq/OpenRouter) never actually dispatch, so
// they have no rows in episodes.json -- this is the real per-sample
// data the Calibration drill-down needs to render a scatter for them
// instead of an empty graph (see publish_to_r2.py's
// build_comparison_episodes for the full reasoning).
export async function fetchComparisonEpisodes() {
  const rows = await fetchJson("comparison_episodes.json");
  return rows.filter((r) => !INTERNAL_ONLY_CLASSES.has(r.fault_class));
}

// Real per-class 6-axis data for the Operator tab's Trust Dossier radar
// chart -- object keyed by fault_class, not an array, so no
// INTERNAL_ONLY_CLASSES filtering is needed here (radar_dossier.json is
// built directly from REAL_FAULT_CLASSES, "none" was never a key).
export async function fetchRadarDossier() {
  return fetchJson("radar_dossier.json");
}
