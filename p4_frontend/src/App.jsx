import { Routes, Route } from "react-router-dom";
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
  return (
    <div className="min-h-screen bg-surface-container-lowest">
      <NavBar />
      <div className="flex">
        <Sidebar />
        <main className="flex-1 px-4 py-6 min-w-0">
          <Routes>
            <Route path="/" element={<TrustLadder />} />
            <Route path="/replay" element={<ReplayViewer />} />
            <Route path="/replay/:episodeId" element={<ReplayViewer />} />
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
