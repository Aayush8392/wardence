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
    <div style={{ border: "1px solid #444", padding: 16, maxWidth: 320, marginTop: 12 }}>
      <form onSubmit={handleSubmit}>
        <div>
          <label>Username <input value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        </div>
        <div>
          <label>Password <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        </div>
        <div>
          <label>
            TOTP code (admin only){" "}
            <input value={totpCode} onChange={(e) => setTotpCode(e.target.value)} placeholder="6-digit code" />
          </label>
        </div>
        {error && <p style={{ color: "#e05" }}>{error}</p>}
        <button type="submit" disabled={loading}>{loading ? "Logging in…" : "Log in"}</button>
        <button type="button" onClick={onClose}>Cancel</button>
      </form>
    </div>
  );
}
