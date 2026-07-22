import { NavLink } from "react-router-dom";
import { useNavHistory } from "../../context/NavHistoryContext";

const TABS = [
  { path: "/", label: "Trust Ladder" },
  { path: "/replay", label: "Replay Viewer" },
  { path: "/calibration", label: "Calibration" },
  { path: "/operator", label: "Operator" },
];

export default function NavBar() {
  const { history, goToHistoryIndex } = useNavHistory();

  return (
    <nav style={{ display: "flex", alignItems: "center", gap: 16, padding: "12px 20px", borderBottom: "1px solid #333" }}>
      <strong>Wardence</strong>
      {TABS.map((t) => (
        <NavLink key={t.path} to={t.path} end={t.path === "/"}>
          {t.label}
        </NavLink>
      ))}

      {/* Back-history list — Wardence's multi-level version of Anchora's
          single FloatingBackButton. Only renders once there's somewhere to
          go back to. Real styling/positioning is a later design pass. */}
      {history.length > 0 && (
        <select
          value=""
          onChange={(e) => goToHistoryIndex(Number(e.target.value))}
          style={{ marginLeft: "auto" }}
        >
          <option value="" disabled>
            ← Back to…
          </option>
          {history.map((h, i) => (
            <option key={i} value={i}>
              {h.label}
            </option>
          ))}
        </select>
      )}
    </nav>
  );
}
