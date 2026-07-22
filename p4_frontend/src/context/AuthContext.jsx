import { createContext, useContext, useState, useCallback } from "react";
import { login as apiLogin } from "../api/operator";

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

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    setRole(null);
    localStorage.removeItem(STORAGE_KEY);
  }, []);

  return (
    <AuthCtx.Provider value={{ user, token, role, loading, error, login, logout }}>
      {children}
    </AuthCtx.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- Provider + hook in one file, matches Anchora's AuthContext convention
export const useAuth = () => useContext(AuthCtx);
