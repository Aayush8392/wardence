// Runtime endpoint discovery.
//
// The operator API and storefront sit behind Cloudflare *quick* tunnels whose
// hostname is re-randomised every time cloudflared restarts (no account/domain =
// no stable name). deploy/publish_endpoints.py writes the current URLs to R2 as
// runtime_config.json (every ~2 min, and ~15s after any tunnel restart); we
// fetch that once at app start so a churn self-heals without a Vercel redeploy.
//
// The VITE_* env vars remain the fallback: used until this fetch resolves, and
// whenever R2 is unreachable or the file is missing.
const R2_BASE = import.meta.env.VITE_R2_BASE_URL;

let resolved = null;

export async function loadRuntimeConfig() {
  if (!R2_BASE) return;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 3000);
    const res = await fetch(`${R2_BASE}/runtime_config.json`, {
      cache: "no-store",
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    if (res.ok) {
      const data = await res.json();
      if (data && typeof data === "object") resolved = data;
    }
  } catch {
    // offline / R2 down / timeout / bad JSON -- the env-var fallback covers it
  }
}

export function operatorBaseUrl() {
  return resolved?.operator_api_url || import.meta.env.VITE_OPERATOR_API_URL || "";
}

export function storefrontUrl() {
  return (
    resolved?.storefront_url ||
    import.meta.env.VITE_STOREFRONT_URL ||
    "http://localhost:8079"
  );
}
