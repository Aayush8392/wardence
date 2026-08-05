import { useEffect } from "react";
import { Routes, Route, useNavigate, useLocation } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { NavHistoryProvider } from "./context/NavHistoryContext";
import NavBar from "./components/layout/NavBar";
import Sidebar from "./components/layout/Sidebar";
import TrustLadder from "./tabs/TrustLadder";
import ReplayViewer from "./tabs/ReplayViewer";
import Calibration from "./tabs/Calibration";
import Operator from "./tabs/Operator";

// NavHistoryProvider needs Router context (useNavigate/useLocation), which
// main.jsx already supplies via <BrowserRouter> wrapping <App />.
function Shell() {
  const navigate = useNavigate();
  const location = useLocation();

  // A hard page refresh and typing/pasting a URL both load the exact same
  // path -- there's no way to tell them apart from the URL alone. The
  // Navigation Timing API can, though: performance.getEntriesByType
  // ("navigation")[0].type is "reload" for an actual refresh, "navigate" for
  // a fresh URL entry, a link click, or a cross-tab jump. Only strip the
  // selected episode on a genuine reload, so a shared/typed
  // "/replay/<id>" URL still opens straight to that episode as designed.
  useEffect(() => {
    const navEntry = performance.getEntriesByType("navigation")[0];
    if (navEntry?.type === "reload" && location.pathname.startsWith("/replay/")) {
      navigate("/replay", { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="min-h-screen bg-surface-container-lowest">
      <NavBar />
      <div className="flex">
        <Sidebar />
        <main className="flex-1 px-4 py-6 min-w-0">
          <Routes>
            <Route path="/" element={<TrustLadder />} />
            {/* One route with an optional param, not two separate <Route>s --
                two entries pointing to the same component still count as two
                different route matches, so React unmounts/remounts
                ReplayViewer (and rebuilds the whole 2000+-dot three.js scene)
                every time selection toggled between "/replay" and
                "/replay/:id". An optional param keeps it the same match. */}
            <Route path="/replay/:episodeId?" element={<ReplayViewer />} />
            <Route path="/calibration" element={<Calibration />} />
            <Route path="/operator" element={<Operator />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <NavHistoryProvider>
        <Shell />
      </NavHistoryProvider>
    </AuthProvider>
  );
}
