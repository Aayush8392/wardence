import { useEffect, useState } from "react";
import { fetchFaultStatus } from "../../api/operator";
import { INVISIBLE_CLASSES, CLASS_LABELS } from "../../constants/faultClasses";

const POLL_MS = 10_000;

// Real live-status readout for disk-full/init-failure/memory-leak, the 3
// classes confirmed storefront-invisible by mechanism design (see
// wardence_frontend.md's "customer-visibility audit" session). Polls
// GET /operator/fault-status/{class} for each -- server-sanitized,
// derived fields only, never raw Prometheus label data.
export default function LiveStatusDetail({ token }) {
  const [statusByClass, setStatusByClass] = useState({});

  useEffect(() => {
    if (!token) return;
    let cancelled = false;

    const poll = () => {
      INVISIBLE_CLASSES.forEach((fc) => {
        fetchFaultStatus(fc, token)
          .then((d) => { if (!cancelled) setStatusByClass((prev) => ({ ...prev, [fc]: d })); })
          .catch(() => {}); // a single flaky class shouldn't blank the whole panel
      });
    };
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [token]);

  if (!token) return null;

  return (
    <div className="bg-surface-container border border-outline">
      <div className="p-4 border-b border-outline-variant flex items-center gap-2">
        <span className="material-symbols-outlined text-primary text-base">visibility</span>
        <h2 className="font-headline-md text-lg">Live Evidence Detail</h2>
        <span className="font-body-sm text-xs text-on-surface-variant ml-auto">
          For the 3 classes with no visible storefront symptom
        </span>
      </div>
      <div className="p-4 space-y-3">
        {INVISIBLE_CLASSES.map((fc) => {
          const s = statusByClass[fc];
          return (
            <div key={fc} className="flex items-center justify-between border-b border-outline-variant/40 pb-3 last:border-0 last:pb-0">
              <div>
                <p className="font-data-mono text-sm text-primary">{fc}</p>
                <p className="font-body-sm text-xs text-on-surface-variant">{CLASS_LABELS[fc]}</p>
              </div>
              <div className="text-right">
                {!s && <span className="font-data-mono text-xs text-on-surface-variant">…</span>}
                {s?.warning && <span className="font-data-mono text-xs text-error">unavailable</span>}
                {s && !s.warning && fc === "disk-full" && (
                  <span
                    className={`font-label-caps text-[10px] px-2 py-1 border ${
                      s.indication === "no_eviction_detected"
                        ? "border-outline-variant text-on-surface-variant"
                        : "border-primary text-primary"
                    }`}
                  >
                    {s.indication.replaceAll("_", " ").toUpperCase()}
                  </span>
                )}
                {s && !s.warning && fc === "init-failure" && (
                  <span
                    className={`font-label-caps text-[10px] px-2 py-1 border ${
                      s.ready_false_present ? "border-primary text-primary" : "border-outline-variant text-on-surface-variant"
                    }`}
                  >
                    {s.ready_false_present ? "FAILED_PROBE" : "READY"}
                  </span>
                )}
                {s && !s.warning && fc === "memory-leak" && (
                  <span className="font-data-mono text-sm">
                    {s.heap_used_mib != null ? `${s.heap_used_mib} MiB` : "n/a"}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
