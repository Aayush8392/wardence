import { createContext, useContext, useState, useCallback, useEffect } from "react";
import { login as apiLogin, apiLogout } from "../api/operator";

// Persisted across refresh -- deliberate tradeoff (JWT sits in localStorage,
// not just memory) noted in wardence_frontend.md's known gaps. No client-side
// expiry check: an expired token just gets rejected by the backend with a
// real 401 on the next call, surfacing as a normal error message rather than
// a silent auto-logout.
const STORAGE_KEY = "wardence_session";

function loadStoredSession() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// Anonymous is the default, real state (not a "viewer role") — Wardence's
// public showcase is fully readable with no login. Login only unlocks MORE
// (demo-trigger / admin), unlike Anchora where no-token means no app at all.
const AuthCtx = createContext({
  user: null,
  token: null,
  role: null, // null | "demo-trigger" | "admin"
  loading: false,
  error: null,
  login: async () => {},
  logout: () => {},
});

export function AuthProvider({ children }) {
  const stored = loadStoredSession();
  const [user, setUser] = useState(stored?.username ?? null);
  const [token, setToken] = useState(stored?.token ?? null);
  const [role, setRole] = useState(stored?.role ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const login = useCallback(async (username, password, totpCode) => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiLogin(username, password, totpCode);
      setToken(data.token);
      setRole(data.role);
      setUser(data.username);
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ token: data.token, role: data.role, username: data.username })
      );
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  // Real, global stale-session handler (2026-08-14/15 finding): api/
  // operator.js dispatches this the moment ANY authenticated call comes
  // back 401 -- a JWT that expired or otherwise stopped being valid
  // server-side. Clears local state directly rather than calling the
  // full logout() above, since that would try apiLogout() with the
  // already-known-bad token (harmless, just wasted -- but this is the
  // more honest "the session is already gone" path, not a user-initiated
  // logout). No apiLogin/apiLogout import needed here beyond what's
  // already in scope.
  useEffect(() => {
    const onExpired = () => {
      setToken(null);
      setUser(null);
      setRole(null);
      localStorage.removeItem(STORAGE_KEY);
    };
    window.addEventListener("wardence:session-expired", onExpired);
    return () => window.removeEventListener("wardence:session-expired", onExpired);
  }, []);

  const logout = useCallback(() => {
    // Best-effort abandon-signal (wardence_frontend.md's Operator redesign
    // arc) -- fire and forget, never awaited. If this account triggered an
    // episode that's still in flight, it lets the backend wind down sooner
    // than the 5-minute abandonment ceiling; if the call never completes
    // (tab closing, network hiccup), that ceiling is still the real
    // backstop, so local logout must never wait on this.
    if (token) apiLogout(token).catch(() => {});
    setToken(null);
    setUser(null);
    setRole(null);
    localStorage.removeItem(STORAGE_KEY);
  }, [token]);

  return (
    <AuthCtx.Provider value={{ user, token, role, loading, error, login, logout }}>
      {children}
    </AuthCtx.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- Provider + hook in one file, matches Anchora's AuthContext convention
export const useAuth = () => useContext(AuthCtx);
