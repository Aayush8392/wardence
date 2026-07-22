import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchEpisodes } from "../../api/r2";
import { fetchLiveTrust } from "../../api/operator";
import { useNavHistory } from "../../context/NavHistoryContext";
import { useAuth } from "../../context/AuthContext";

export default function ReplayViewer() {
  const { episodeId } = useParams();
  const { currentContext, navigateTo } = useNavHistory();
  const { token, role } = useAuth();
  const [episodes, setEpisodes] = useState(null);
  const [error, setError] = useState(null);
  const [trustMap, setTrustMap] = useState({});

  useEffect(() => {
    let cancelled = false;
    fetchEpisodes()
      .then((data) => { if (!cancelled) setEpisodes(data); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  // Live (not R2-snapshot) trust state -- only needed for the admin-only
  // "Promote Class" link below, so only fetched when there's an admin
  // token to fetch it with.
  useEffect(() => {
    if (role !== "admin" || !token) return;
    let cancelled = false;
    fetchLiveTrust(token)
      .then((states) => {
        if (cancelled) return;
        setTrustMap(Object.fromEntries(states.map((s) => [s.fault_class, s.state])));
      })
      .catch(() => {}); // non-critical -- Promote link just won't show
    return () => { cancelled = true; };
  }, [role, token]);

  if (error) return <p>Failed to load episodes: {error}</p>;
  if (!episodes) return <p>Loading…</p>;

  if (episodeId) {
    const ep = episodes.find((e) => e.episode_id === episodeId);
    if (!ep) return <p>Episode {episodeId} not found.</p>;
    return (
      <EpisodeDetail
        episode={ep}
        canPromote={role === "admin" && trustMap[ep.fault_class] === "report_only" && ep.correct && !ep.scores_action_taken}
        onPromote={() => navigateTo("/operator", { type: "promoteClass", faultClass: ep.fault_class }, "Replay Viewer")}
      />
    );
  }

  // Consume a cross-tab jump from Trust Ladder ({type:"faultClass", faultClass})
  const filterClass = currentContext?.type === "faultClass" ? currentContext.faultClass : null;
  const visible = filterClass ? episodes.filter((e) => e.fault_class === filterClass) : episodes;

  return (
    <div>
      <h1>Replay Viewer</h1>
      {filterClass && <p>Filtered to fault class: <strong>{filterClass}</strong></p>}

      <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 16 }}>
        <thead>
          <tr style={{ textAlign: "left", borderBottom: "1px solid #444" }}>
            <th>Episode</th>
            <th>Fault Class</th>
            <th>Target</th>
            <th>Predicted</th>
            <th>Correct</th>
            <th>t0</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((ep) => (
            <tr
              key={ep.episode_id}
              style={{ cursor: "pointer", borderBottom: "1px solid #2a2a2a" }}
              onClick={() => navigateTo(`/replay/${ep.episode_id}`, null, "Replay Viewer")}
            >
              <td>{ep.episode_id.slice(0, 8)}</td>
              <td>{ep.fault_class}</td>
              <td>{ep.target}</td>
              <td>{ep.predicted_class}</td>
              <td>{ep.correct ? "✓" : "✗"}</td>
              <td>{ep.t0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EpisodeDetail({ episode, canPromote, onPromote }) {
  return (
    <div>
      <h1>Episode {episode.episode_id}</h1>
      <p>
        <strong>{episode.fault_class}</strong> on <code>{episode.target}</code> ({episode.namespace})
      </p>
      <p>Predicted: {episode.predicted_class} — {episode.correct ? "Correct" : "Incorrect"}</p>

      {canPromote && (
        <p>
          <button onClick={onPromote}>Promote Class →</button>{" "}
          <span style={{ fontSize: 12, opacity: 0.7 }}>
            (currently report-only, correctly diagnosed here — jump to Operator's override controls)
          </span>
        </p>
      )}

      {episode.reasoning ? (
        <>
          <h3>Reasoning</h3>
          <pre style={{ whiteSpace: "pre-wrap", background: "#1a1a1a", padding: 12 }}>{episode.reasoning}</pre>
        </>
      ) : (
        <p><em>No reasoning snapshot captured for this episode (predates Tier A snapshot capture).</em></p>
      )}

      {episode.action_result && (
        <>
          <h3>Action Result</h3>
          <pre style={{ whiteSpace: "pre-wrap", background: "#1a1a1a", padding: 12 }}>
            {JSON.stringify(episode.action_result, null, 2)}
          </pre>
        </>
      )}

      {episode.snapshot_durability_verdict && (
        <p>Durability verdict: <strong>{episode.snapshot_durability_verdict}</strong> ({episode.durability_elapsed_s}s)</p>
      )}
    </div>
  );
}
