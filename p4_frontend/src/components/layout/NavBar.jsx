import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { useNavHistory } from "../../context/NavHistoryContext";
import { useAuth } from "../../context/AuthContext";
import { fetchSystemStatus } from "../../api/r2";
import LoginModal from "../auth/LoginModal";

const TABS = [
  { path: "/", label: "TRUST LADDER" },
  { path: "/replay", label: "REPLAY VIEWER" },
  { path: "/calibration", label: "CALIBRATION" },
  { path: "/operator", label: "OPERATOR" },
];

export default function NavBar() {
  const { history, goToHistoryIndex } = useNavHistory();
  const { user, role, logout } = useAuth();
  const [systemStatus, setSystemStatus] = useState(null);
  const [showLogin, setShowLogin] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchSystemStatus()
      .then((data) => { if (!cancelled) setSystemStatus(data); })
      .catch(() => { /* status pill just omits itself on failure */ });
    return () => { cancelled = true; };
  }, []);

  return (
    <nav className="relative w-full flex justify-between items-center px-4 h-14 border-b border-outline-variant bg-surface-container-lowest">
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2">
          <span className="material-symbols-outlined text-primary scale-125">shield_with_heart</span>
          <span className="font-headline-md text-lg font-bold tracking-tight text-on-surface">WARDENCE</span>
        </div>
        <div className="hidden md:flex gap-6 h-full items-center ml-4">
          {TABS.map((t) => (
            <NavLink
              key={t.path}
              to={t.path}
              end={t.path === "/"}
              className={({ isActive }) =>
                `font-label-caps text-[11px] h-full flex items-center px-1 border-b-2 ${
                  isActive
                    ? "text-primary border-primary"
                    : "text-on-surface-variant hover:text-on-surface border-transparent transition-colors"
                }`
              }
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-4">
        {history.length > 0 && (
          <select
            value=""
            onChange={(e) => goToHistoryIndex(Number(e.target.value))}
            className="bg-surface-variant text-on-surface-variant border border-outline-variant text-[11px] rounded-sm px-2 py-1"
          >
            <option value="" disabled>← Back to…</option>
            {history.map((h, i) => (
              <option key={i} value={i}>{h.label}</option>
            ))}
          </select>
        )}

        {/* System Guard -- real circuit-breaker status from system_status.json,
            not decorative. Green/pulsing when untripped, red/still on trip. */}
        {systemStatus && (
          <div
            className="flex items-center gap-2 px-3 py-1 bg-surface-variant border border-outline-variant rounded-sm"
            title={`${systemStatus.recent_failures} failure(s) in the last ${systemStatus.failure_window_s}s (trips at ${systemStatus.failure_threshold})`}
          >
            <span
              className={`w-2 h-2 rounded-full ${systemStatus.tripped ? "bg-red-500" : "bg-green-500 animate-pulse"}`}
            />
            <span className="font-data-mono text-[10px] text-on-surface-variant">
              {systemStatus.tripped ? "GUARD_TRIPPED" : "NODE_01: ACTIVE"}
            </span>
          </div>
        )}

        {!user ? (
          <button
            onClick={() => setShowLogin((v) => !v)}
            className="font-label-caps text-[11px] text-on-surface border border-outline-variant px-3 py-1.5 hover:border-primary transition-colors"
          >
            SIGN IN
          </button>
        ) : (
          <div className="flex items-center gap-2 font-data-mono text-[11px] text-on-surface-variant">
            <span>{user} ({role})</span>
            <button onClick={logout} className="text-primary hover:underline">Log out</button>
          </div>
        )}
      </div>

      {showLogin && !user && <LoginModal onClose={() => setShowLogin(false)} />}
    </nav>
  );
}
