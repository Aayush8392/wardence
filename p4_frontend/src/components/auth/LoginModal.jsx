import { useState } from "react";
import { useAuth } from "../../context/AuthContext";

export default function LoginModal({ onClose }) {
  const { login, loading, error } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    const ok = await login(username, password, totpCode);
    if (ok) onClose();
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="absolute right-4 top-14 w-72 bg-surface-container border border-outline-variant p-4 flex flex-col gap-3 z-50 shadow-2xl"
    >
      <label className="flex flex-col gap-1 text-xs text-on-surface-variant font-label-caps">
        USERNAME
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          className="bg-surface-container-lowest border border-outline-variant text-on-surface text-sm px-2 py-1.5"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-on-surface-variant font-label-caps">
        PASSWORD
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="bg-surface-container-lowest border border-outline-variant text-on-surface text-sm px-2 py-1.5"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-on-surface-variant font-label-caps">
        TOTP CODE (admin only)
        <input
          value={totpCode}
          onChange={(e) => setTotpCode(e.target.value)}
          placeholder="6-digit code"
          className="bg-surface-container-lowest border border-outline-variant text-on-surface text-sm px-2 py-1.5"
        />
      </label>

      {error && <p className="text-error text-xs">{error}</p>}

      <div className="flex gap-2 mt-1">
        <button
          type="submit"
          disabled={loading}
          className="flex-1 bg-primary text-on-primary text-xs font-label-caps py-2 disabled:opacity-60"
        >
          {loading ? "LOGGING IN…" : "LOG IN"}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="px-3 border border-outline-variant text-on-surface-variant text-xs font-label-caps"
        >
          CANCEL
        </button>
      </div>
    </form>
  );
}
