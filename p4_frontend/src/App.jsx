import { Routes, Route } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { NavHistoryProvider } from "./context/NavHistoryContext";
import NavBar from "./components/layout/NavBar";
import TrustLadder from "./tabs/TrustLadder";
import ReplayViewer from "./tabs/ReplayViewer";
import Calibration from "./tabs/Calibration";
import Operator from "./tabs/Operator";

// NavHistoryProvider needs Router context (useNavigate/useLocation), which
// main.jsx already supplies via <BrowserRouter> wrapping <App />.
function Shell() {
  return (
    <div>
      <NavBar />
      <main style={{ padding: 20 }}>
        <Routes>
          <Route path="/" element={<TrustLadder />} />
          <Route path="/replay" element={<ReplayViewer />} />
          <Route path="/replay/:episodeId" element={<ReplayViewer />} />
          <Route path="/calibration" element={<Calibration />} />
          <Route path="/operator" element={<Operator />} />
        </Routes>
      </main>
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
