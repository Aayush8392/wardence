// Live operator API -- p3_trust_action/operator_api.py, run locally
// against the real k3s cluster (NOT deployed to Vercel, unlike the R2
// static data). Must be running: uvicorn operator_api:app --reload
// --app-dir p3_trust_action --port 8002
const BASE_URL = import.meta.env.VITE_OPERATOR_API_URL;

async function request(path, { method = "GET", token, body, params } = {}) {
  if (!BASE_URL) {
    throw new Error("VITE_OPERATOR_API_URL is not set (check p4_frontend/.env)");
  }
  const url = new URL(`${BASE_URL}${path}`);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  }

  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(data?.detail ?? `${method} ${path} failed: ${res.status}`);
    // Status attached (2026-07-24) so callers can distinguish error KINDS
    // without string-matching messages -- e.g. a 410 from /trigger/resolve
    // means the fault's diagnosability window expired and the episode
    // will never be scored, which needs different handling (abandon it,
    // don't let the user retry) than a transient failure would.
    err.status = res.status;
    throw err;
  }
  return data;
}

// Public, no auth -- feeds the "X of Y daily triggers left" widget for
// anonymous and logged-in visitors alike.
export function fetchTriggerStatus() {
  return request("/trigger/status");
}

export function login(username, password, totpCode) {
  return request("/login", {
    method: "POST",
    body: { username, password, totp_code: totpCode || undefined },
  });
}

export function fetchSystemStatus(token) {
  return request("/system-status", { token });
}

// Live current trust state (not the R2 snapshot, which only updates when
// publish_to_r2.py is re-run) -- used where "is this class report-only
// RIGHT NOW" needs to be real-time-accurate, e.g. deciding whether to show
// a Promote-Class link in the Replay Viewer.
export function fetchLiveTrust(token) {
  return request("/trust", { token }).then((d) => d.states);
}

// Two-phase trigger flow (2026-07-24, see wardence_frontend.md): inject
// returns immediately once the fault has landed; resolve is a SEPARATE,
// user-clicked action that runs the real diagnose+act+score pipeline.
// resolve may take longer than its own network round-trip suggests -- the
// backend silently pads out the remaining settle window server-side if
// resolve is called too soon after inject. The frontend must show one
// continuous busy state for the whole resolve call, never a distinct
// "waiting to stabilize" message -- see that file for why.
export function injectFault(faultClass, token) {
  return request("/trigger/inject", { method: "POST", token, params: { fault_class: faultClass } });
}

export function resolveFault(episodeId, token) {
  return request("/trigger/resolve", { method: "POST", token, params: { episode_id: episodeId } });
}

// Real async state-machine build (Phase 1/2, 2026-08-1x/13) -- inject and
// resolve above both now return almost immediately; this is what the
// frontend polls for real progress (episode_state: injecting -> holding ->
// awaiting_fix -> resolving -> resolved/failed). See operator_api.py's own
// docstring on GET /trigger/live-status for the full field meaning.
export function fetchLiveStatus(episodeId, token) {
  return request("/trigger/live-status", { token, params: { episode_id: episodeId } });
}

// Real SSE URL for the live Operator "Central Thinking Hub" widget.
// NOT run through the shared request() helper above -- EventSource takes
// a raw URL string and connects itself, it doesn't use fetch() at all.
// Token travels as a query param, not an Authorization header (see
// operator_api.py's trigger_reasoning_stream docstring for why --
// EventSource structurally cannot set custom headers).
export function reasoningStreamUrl(episodeId, token) {
  if (!BASE_URL) {
    throw new Error("VITE_OPERATOR_API_URL is not set (check p4_frontend/.env)");
  }
  const url = new URL(`${BASE_URL}/trigger/reasoning-stream/${episodeId}`);
  url.searchParams.set("token", token);
  return url.toString();
}

// Crash-loop/cpu-throttling's (and, per the same mechanism, all 6
// report-only classes') early-exit click -- only valid once live-status's
// own can_stop_hold_early flips true. A 409 here means the window closed
// between the poll and the click (e.g. it just naturally finished); the
// caller should treat that as "already moving on," not a real error.
export function stopHold(episodeId, token) {
  return request("/trigger/stop-hold", { method: "POST", token, params: { episode_id: episodeId } });
}

// Sanitized live readout for the 3 classes with no other Operator-screen
// visibility (disk-full/init-failure/memory-leak -- their active fault
// window is invisible on the real storefront by mechanism design, see
// wardence_frontend.md's "customer-visibility audit"). Real Prometheus
// data, server-projected down to derived fields only -- never raw label
// data. Safe to call for any class; only meaningful for these 3.
export function fetchFaultStatus(faultClass, token) {
  return request(`/operator/fault-status/${faultClass}`, { token });
}

// Best-effort abandon-signal (Kimi review 36 finding 8) -- never a hard
// dependency, the 5-minute abandonment ceiling is the real backstop
// regardless of whether this call ever completes. Caller should fire this
// and clear local session state without waiting on it.
export function apiLogout(token) {
  return request("/logout", { method: "POST", token });
}

export function promoteClass(faultClass, token) {
  return request("/promote", { method: "POST", token, params: { fault_class: faultClass } });
}

export function demoteClass(faultClass, token) {
  return request("/demote", { method: "POST", token, params: { fault_class: faultClass } });
}
