import { NavLink } from "react-router-dom";

// Mirrors NavBar's TABS exactly (same paths, same labels) -- there was
// previously a second, differently-worded set of links here (AUTONOMY_MAP/
// AUDIT_TRAIL/POLICY_ENGINE, only 3 of the 4 real tabs), which read as a
// separate, inconsistent nav system rather than a second entry point into
// the same one.
const SCOPE_LINKS = [
  { path: "/", label: "TRUST LADDER", icon: "analytics" },
  { path: "/replay", label: "REPLAY VIEWER", icon: "history" },
  { path: "/calibration", label: "CALIBRATION", icon: "insights" },
  { path: "/operator", label: "OPERATOR", icon: "security" },
];

export default function Sidebar() {
  return (
    <aside className="hidden lg:flex flex-col w-60 shrink-0 border-r border-outline-variant bg-surface-container-lowest p-4">
      <div className="mb-6 p-3 border border-outline-variant bg-surface-container-low">
        <div className="font-label-caps text-[10px] text-on-surface-variant mb-1">SYSTEM_ID</div>
        {/* Real lab identity, not a fabricated "PROD" claim -- this is the
            local disposable k3s lab (wardence_context.md), never hosted prod. */}
        <div className="font-data-mono text-sm text-primary">WARDENCE-LAB-01</div>
      </div>

      <nav className="flex-1 flex flex-col gap-1">
        <div className="font-label-caps text-[10px] text-on-surface-variant px-3 py-2 mb-1">NAVIGATION</div>
        {SCOPE_LINKS.map((link) => (
          <NavLink
            key={link.path}
            to={link.path}
            end={link.path === "/"}
            className={({ isActive }) =>
              `flex items-center gap-3 p-3 ${
                isActive
                  ? "text-on-surface bg-surface-variant border-l-2 border-primary"
                  : "text-on-surface-variant hover:bg-surface-variant"
              }`
            }
          >
            <span className="material-symbols-outlined">{link.icon}</span>
            <span className="font-label-caps text-[11px]">{link.label}</span>
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
