import { createContext, useContext, useState, useCallback } from "react";
import { useNavigate, useLocation } from "react-router-dom";

// Wardence's cross-tab nav: a real history STACK, not Anchora's single
// `fromTab` pointer (see wardence_frontend.md "Cross-Tab Navigation" section
// for the full reasoning — Anchora can only go back one level; this can jump
// back to any prior stop). Each entry: { path, label, context, timestamp }.
// `context` is the same shape Anchora used (`{ type, ...payload }`) so a
// destination tab can filter/restore against it.
const NavHistoryCtx = createContext({
  history: [],
  currentContext: null,
  navigateTo: () => {},
  goToHistoryIndex: () => {},
  clearHistory: () => {},
});

export function NavHistoryProvider({ children }) {
  const [history, setHistory] = useState([]);
  const [currentContext, setCurrentContext] = useState(null);
  const navigate = useNavigate();
  const location = useLocation();

  // Cross-tab jump. `label` is what the back-list shows (e.g. "Trust Ladder").
  const navigateTo = useCallback(
    (path, context = null, label = path) => {
      setHistory((prev) => [
        ...prev,
        { path: location.pathname, label, context: currentContext, timestamp: Date.now() },
      ]);
      setCurrentContext(context);
      navigate(path, { state: context });
    },
    [navigate, location.pathname, currentContext]
  );

  // Jump back to any prior stop in the list, discarding everything after it
  // (standard breadcrumb behavior — matches Anchora's "round trip complete"
  // framing, just generalized past one level).
  const goToHistoryIndex = useCallback(
    (index) => {
      const entry = history[index];
      if (!entry) return;
      setHistory((prev) => prev.slice(0, index));
      setCurrentContext(entry.context);
      navigate(entry.path, { state: entry.context });
    },
    [history, navigate]
  );

  const clearHistory = useCallback(() => {
    setHistory([]);
    setCurrentContext(null);
  }, []);

  return (
    <NavHistoryCtx.Provider
      value={{ history, currentContext, navigateTo, goToHistoryIndex, clearHistory }}
    >
      {children}
    </NavHistoryCtx.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- Provider + hook in one file, matches Anchora's AuthContext convention
export const useNavHistory = () => useContext(NavHistoryCtx);
