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
    throw new Error(data?.detail ?? `${method} ${path} failed: ${res.status}`);
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

export function triggerFault(faultClass, token) {
  return request("/trigger", { method: "POST", token, params: { fault_class: faultClass } });
}

export function promoteClass(faultClass, token) {
  return request("/promote", { method: "POST", token, params: { fault_class: faultClass } });
}

export function demoteClass(faultClass, token) {
  return request("/demote", { method: "POST", token, params: { fault_class: faultClass } });
}
