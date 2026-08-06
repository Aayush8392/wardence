// Real per-field thresholds, copied from p2_readonly_loop/agent.py /
// react_agent.py's own numeric diagnosis constants -- not guessed. Only
// fields with a real, hardcoded diagnostic threshold get the radial-gauge
// treatment; everything else gets a plainer real-data tile instead of a
// fabricated ceiling. Each caption is a short, plain-language description of
// what the field actually checks, grounded in agent.py's own real comments.
const GAUGE_FIELDS = {
  p95_latency_ms: {
    label: "P95_LATENCY", unit: "ms", threshold: 300, // HIGH_LATENCY_THRESHOLD_MS
    caption: "How slow the slowest 5% of requests are — the sign of network-latency.",
  },
  peak_memory_mib: {
    label: "PEAK_MEMORY", unit: "Mi", threshold: 380, // memory-leak numeric threshold
    caption: "Highest memory usage seen — the sign of memory-leak, or OOM if it spiked fast.",
  },
  peak_threads_connected: {
    label: "DB_CONNECTIONS", unit: "", threshold: 100, // connection-pool-exhaustion threshold
    caption: "Highest number of open DB connections seen — the sign of connection-pool-exhaustion.",
  },
  cpu_throttle_periods_increase: {
    label: "CPU_THROTTLE_PERIODS", unit: "", threshold: 100, // cpu-throttling threshold
    caption: "How much more the CPU got throttled — the sign of cpu-throttling.",
  },
};

// Real fields with no fixed diagnostic ceiling (evaluated relatively or
// used as context, not against a hardcoded threshold) -- shown as a plain
// number, never forced into a gauge with an invented ceiling.
const NUMERIC_FIELDS = {
  current_replicas: {
    label: "CURRENT_REPLICAS", unit: "",
    caption: "How many replicas are running right now — kept safe while sizing the fix.",
  },
  combined_throughput_bps: {
    label: "THROUGHPUT", unit: "bps",
    caption: "Combined network traffic; a sharp drop is the sign of network-partition.",
  },
};

// Real boolean signals -- a gauge doesn't apply, shown as a single lit/unlit
// status pip instead.
const BOOLEAN_FIELDS = {
  payment_stuck_not_ready: {
    label: "PAYMENT_STUCK",
    caption: "Checks if a payment pod is stuck and never becomes ready — the sign of init-failure.",
  },
  session_db_replicas_hit_zero: {
    label: "SESSION_DB_DOWN",
    caption: "Checks if session-db dropped to zero running replicas — the sign of session-cart-failure.",
  },
  front_end_image_pull_failing: {
    label: "IMAGE_PULL_FAIL",
    caption: "Checks if a new front-end pod is stuck pulling a bad image — the sign of bad-rollout.",
  },
};

// Real pod-name arrays shown as plain pill lists.
const PILL_FIELDS = {
  oom_pods: { label: "OOM_PODS", caption: "Pods killed for running out of memory." },
  evicted_pods: { label: "EVICTED_PODS", caption: "Pods evicted for using too much disk." },
};

const CRASHLOOP_CAPTION = "Pods stuck restarting in a loop.";

// Real per-class diagnostic driver(s), matching stub_diagnose's own priority
// order in agent.py -- NOT "whichever field happens to be present" (agent.py
// computes ALL 12 fields on every episode regardless of class: the numeric
// ones are real generic per-pod metrics that exist for any running pod, and
// the booleans are target-gated to a real False rather than omitted, so
// "present" was never the same thing as "relevant"). These are the field(s)
// that actually drove THIS predicted_class -- everything else present is
// real data too, just not diagnostically meaningful here, and gets demoted
// to the compact secondary strip instead of a full tile. Keyed by
// predicted_class since that's what the diagnoser actually looked at.
// under-provisioned-replicas' real driver (catalogue_probe_p95_ms) and
// disk-full's (a fresh eviction check) aren't fully captured by these 12
// published fields -- current_replicas/evicted_pods are the closest real
// stand-ins, not a perfect match.
export const FAULT_CLASS_RELEVANT_FIELDS = {
  "oom": ["peak_memory_mib", "oom_pods"],
  "crash-loop": ["crashlooping_pods"],
  "disk-full": ["evicted_pods"],
  "bad-rollout": ["front_end_image_pull_failing"],
  "init-failure": ["payment_stuck_not_ready"],
  "session-cart-failure": ["session_db_replicas_hit_zero"],
  "network-partition": ["combined_throughput_bps"],
  "network-latency": ["p95_latency_ms"],
  "memory-leak": ["peak_memory_mib"],
  "connection-pool-exhaustion": ["peak_threads_connected"],
  "cpu-throttling": ["cpu_throttle_periods_increase"],
  "under-provisioned-replicas": ["current_replicas"],
};

const CIRCUMFERENCE = 2 * Math.PI * 40; // r=40 on a 0-100 viewBox, matches GaugeTile's <circle>

// Large/unreliable readings (e.g. p95_latency_ms on sparse traffic) can run
// into 5+ digits -- fixed at a small font size they'd overflow the 20x20
// gauge ring, so size down once the printed string gets long.
function gaugeValueTextClass(str) {
  if (str.length > 7) return "text-[10px]";
  if (str.length > 5) return "text-xs";
  return "text-sm";
}

// Shared tile chrome -- corner bracket + consistent panel background, kept
// per-tile (not just on the outer wrapper) so each stays legible as its own
// module inside the grouped panel. The "hero" tile (the one real field whose
// value actually crossed its real threshold, i.e. the one that drove the
// diagnosis) gets a highlighted border instead of the plain default -- real
// visual hierarchy instead of every tile competing equally, adapted from
// the Stitch export's featured-tile treatment.
function tileCss(hero) {
  return `border p-3 relative ${
    hero ? "border-primary/60 bg-surface-container-low" : "border-outline-variant/40 bg-surface-container-lowest"
  }`;
}
const CORNER = (hero) => (
  <div className={`absolute top-0 left-0 w-2 h-2 border-t border-l ${hero ? "border-primary/80" : "border-outline-variant/60"}`} />
);

function Caption({ text }) {
  if (!text) return null;
  return <div className="font-data-md text-[10px] text-on-surface-variant/70 mt-1.5 leading-snug">{text}</div>;
}

function GaugeTile({ label, value, unit, threshold, caption, hero }) {
  const pct = Math.min(100, (value / threshold) * 100);
  const breached = value >= threshold;
  const dashOffset = CIRCUMFERENCE - (pct / 100) * CIRCUMFERENCE;
  // Values under 1000 keep one decimal (matches the source's own .toFixed(1));
  // anything larger rounds to a whole number -- a large reading is already an
  // unusual/unreliable case (see FAULT_CLASS_HERO_FIELD's comment), and an
  // extra decimal digit there just makes the overflow worse for no real gain.
  const displayValue = value >= 1000 ? Math.round(value) : value;
  const valueStr = `${displayValue}${unit}`;
  return (
    <div className={`${tileCss(hero)} flex flex-col items-center`}>
      {CORNER(hero)}
      <div className="font-label-caps text-[9px] text-on-surface-variant mb-1 self-start">{label}</div>
      <div className="relative w-20 h-20">
        <svg className="w-20 h-20 -rotate-90" viewBox="0 0 100 100">
          <circle
            className="text-surface-container-highest"
            cx="50" cy="50" r="40" fill="none" stroke="currentColor" strokeWidth="8"
          />
          <circle
            className={breached ? "text-error-red" : "text-primary"}
            cx="50" cy="50" r="40" fill="none" stroke="currentColor" strokeWidth="8"
            strokeDasharray={CIRCUMFERENCE}
            strokeDashoffset={dashOffset}
            strokeLinecap="round"
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center px-2">
          <span className={`font-data-mono ${gaugeValueTextClass(valueStr)} ${breached ? "text-error-red" : "text-on-surface"}`}>
            {valueStr}
          </span>
        </div>
      </div>
      <div className="font-data-mono text-[9px] text-on-surface-variant text-center mt-1">
        {breached ? "THRESH_EXCEEDED" : `LIMIT ${threshold}${unit}`}
      </div>
      <Caption text={caption} />
    </div>
  );
}

function NumericTile({ label, value, unit, caption, hero }) {
  return (
    <div className={tileCss(hero)}>
      {CORNER(hero)}
      <div className="font-label-caps text-[9px] text-on-surface-variant mb-1">{label}</div>
      <div className="font-data-mono text-xl text-on-surface">
        {value}
        <span className="text-xs ml-0.5">{unit}</span>
      </div>
      <Caption text={caption} />
    </div>
  );
}

function StatusPip({ label, active, caption, hero }) {
  return (
    <div className={tileCss(hero)}>
      {CORNER(hero)}
      <div className="flex items-center gap-2">
        <span
          className={`w-3 h-3 rounded-full shrink-0 ${
            active ? "bg-error-red shadow-[0_0_6px_var(--color-error-red)]" : "border border-outline-variant bg-surface-container-highest"
          }`}
        />
        <div className="font-label-caps text-[9px] text-on-surface-variant">{label}</div>
        <div className={`ml-auto font-data-mono text-[10px] ${active ? "text-error-red" : "text-on-surface-variant"}`}>
          {active ? "TRUE" : "FALSE"}
        </div>
      </div>
      <Caption text={caption} />
    </div>
  );
}

function ListTile({ label, items, caption, hero }) {
  return (
    <div className={tileCss(hero)}>
      {CORNER(hero)}
      <div className="font-label-caps text-[9px] text-on-surface-variant mb-1.5">{label}</div>
      {items.length === 0 ? (
        <div className="font-data-mono text-sm text-on-surface-variant">none</div>
      ) : (
        <div className="flex flex-wrap gap-1">
          {items.map((it) => (
            <span key={it} className="font-data-mono text-[10px] text-error-red bg-error-red/10 border border-error-red/30 px-1.5 py-0.5 rounded-sm">
              {it}
            </span>
          ))}
        </div>
      )}
      <Caption text={caption} />
    </div>
  );
}

// Real pod count as pips -- no fabricated restart tally (agent.py's real
// Prometheus query only checks whether restarts increased above a
// threshold, it never returns an actual restart count).
function PipMeterTile({ label, pods, caption, hero }) {
  return (
    <div className={tileCss(hero)}>
      {CORNER(hero)}
      <div className="flex items-center justify-between mb-1.5">
        <div className="font-label-caps text-[9px] text-on-surface-variant">{label}</div>
        <div className="font-data-mono text-[10px] text-error-red">{pods.length}</div>
      </div>
      {pods.length === 0 ? (
        <div className="font-data-mono text-sm text-on-surface-variant">none</div>
      ) : (
        <div className="flex flex-wrap gap-1">
          {pods.map((p) => (
            <div key={p} title={p} className="w-4 h-6 bg-error-red shadow-[0_0_4px_var(--color-error-red)]" />
          ))}
        </div>
      )}
      <Caption text={caption} />
    </div>
  );
}

// Real values, compact -- everything present but NOT relevant to this
// episode's diagnosis. Still real data (not hidden), just visually
// subordinate to the primary tile(s) instead of competing for equal
// attention. Hover for the same real caption the full tile would show.
function compactValue(kind, key, toolOutput) {
  if (kind === "gauge" || kind === "numeric") {
    const def = (kind === "gauge" ? GAUGE_FIELDS : NUMERIC_FIELDS)[key];
    const raw = toolOutput[key];
    const value = raw >= 1000 ? Math.round(raw) : Number(raw.toFixed(1));
    return `${value}${def.unit}`;
  }
  if (kind === "boolean") return toolOutput[key] ? "TRUE" : "FALSE";
  if (kind === "pill" || kind === "crashloop") {
    const items = toolOutput[key];
    return items.length === 0 ? "none" : `${items.length}`;
  }
  return "";
}

function SecondaryStrip({ entries, toolOutput }) {
  if (entries.length === 0) return null;
  return (
    <div className="mt-2 pt-2 border-t border-outline-variant/20">
      <div className="font-label-caps text-[8px] text-on-surface-variant/50 mb-1.5">
        ALSO CHECKED — NOT RELEVANT HERE
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {entries.map(({ kind, key, label, caption }) => (
          <span key={key} title={caption} className="font-data-mono text-[10px] text-on-surface-variant/70">
            {label}: <span className="text-on-surface-variant">{compactValue(kind, key, toolOutput)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

// Real fields only. Considers all 12 real tool_output fields
// (p2_readonly_loop/agent.py's query_prometheus return shape) that are
// actually present on THIS episode -- but agent.py computes all of them on
// every episode regardless of class, so "present" alone doesn't mean
// "relevant." Split into two tiers: the field(s) that actually drove THIS
// predicted_class (FAULT_CLASS_RELEVANT_FIELDS) get full featured tiles;
// everything else present is real but demoted to a compact strip rather
// than hidden or given equal visual weight.
// opacity/primaryOpacity/secondaryOpacity are optional continuous 0..1
// values -- omitted (Snapshot's own call site), everything shows at full
// opacity immediately. Replay passes real, scrub-position-derived values so
// the whole section (and its two tiers separately) can fade in without ever
// being removed from the DOM -- removing/re-adding is what caused layout
// shift before; opacity alone on an always-mounted element can't.
export default function EvidenceGrid({
  toolOutput, predictedClass, dispatchedField, dispatchedOpacity = 1,
  opacity = 1, primaryOpacity = 1, secondaryOpacity = 1, active = false,
}) {
  if (!toolOutput) return null;

  const gaugeFields = Object.entries(GAUGE_FIELDS).filter(([k]) => typeof toolOutput[k] === "number");
  const numericFields = Object.entries(NUMERIC_FIELDS).filter(([k]) => typeof toolOutput[k] === "number");
  const booleanFields = Object.entries(BOOLEAN_FIELDS).filter(([k]) => typeof toolOutput[k] === "boolean");
  const pillFields = Object.entries(PILL_FIELDS).filter(([k]) => Array.isArray(toolOutput[k]));
  const hasCrashLooping = Array.isArray(toolOutput.crashlooping_pods);

  if (
    gaugeFields.length === 0 &&
    numericFields.length === 0 &&
    booleanFields.length === 0 &&
    pillFields.length === 0 &&
    !hasCrashLooping
  ) {
    return null;
  }

  const relevantKeys = new Set(FAULT_CLASS_RELEVANT_FIELDS[predictedClass] ?? []);

  const primaryGauge = gaugeFields.filter(([k]) => relevantKeys.has(k));
  const primaryNumeric = numericFields.filter(([k]) => relevantKeys.has(k));
  const primaryBoolean = booleanFields.filter(([k]) => relevantKeys.has(k));
  const primaryPill = pillFields.filter(([k]) => relevantKeys.has(k));
  const primaryCrashloop = hasCrashLooping && relevantKeys.has("crashlooping_pods");

  const hasPrimary = primaryGauge.length + primaryNumeric.length + primaryBoolean.length + primaryPill.length > 0 || primaryCrashloop;

  const secondary = [
    ...gaugeFields.filter(([k]) => !relevantKeys.has(k)).map(([k, f]) => ({ kind: "gauge", key: k, label: f.label, caption: f.caption })),
    ...numericFields.filter(([k]) => !relevantKeys.has(k)).map(([k, f]) => ({ kind: "numeric", key: k, label: f.label, caption: f.caption })),
    ...booleanFields.filter(([k]) => !relevantKeys.has(k)).map(([k, f]) => ({ kind: "boolean", key: k, label: f.label, caption: f.caption })),
    ...pillFields.filter(([k]) => !relevantKeys.has(k)).map(([k, f]) => ({ kind: "pill", key: k, label: f.label, caption: f.caption })),
    ...(hasCrashLooping && !relevantKeys.has("crashlooping_pods")
      ? [{ kind: "crashloop", key: "crashlooping_pods", label: "CRASHLOOPING_PODS", caption: CRASHLOOP_CAPTION }]
      : []),
  ];

  return (
    <div className={`border border-outline-variant bg-surface-container-low p-4 pt-5 relative ${active ? "content-live-glow" : ""}`} style={{ opacity }}>
      <div className="absolute top-0 left-3 -translate-y-1/2 px-2 py-0.5 bg-surface-variant font-label-caps text-[9px] text-on-surface-variant">
        TELEMETRY_SNAPSHOT
      </div>

      {dispatchedField && (
        <div
          className="flex items-center gap-2 mb-3 pb-3 border-b border-dashed border-primary/30"
          style={{ opacity: dispatchedOpacity }}
        >
          <span className="material-symbols-outlined text-primary text-base">bolt</span>
          <span className="font-label-caps text-[9px] text-primary">{dispatchedField.label}</span>
          <span className="font-data-mono text-lg text-primary ml-auto">
            {dispatchedField.value}{dispatchedField.unit}
          </span>
        </div>
      )}

      {hasPrimary && (
        <div className="grid grid-cols-2 gap-2" style={{ opacity: primaryOpacity }}>
          {primaryGauge.map(([key, f]) => (
            <GaugeTile
              key={key}
              label={f.label}
              value={Number(toolOutput[key].toFixed(1))}
              unit={f.unit}
              threshold={f.threshold}
              caption={f.caption}
              hero
            />
          ))}
          {primaryNumeric.map(([key, f]) => (
            <NumericTile
              key={key}
              label={f.label}
              value={Number(toolOutput[key].toFixed(1))}
              unit={f.unit}
              caption={f.caption}
              hero
            />
          ))}
          {primaryBoolean.map(([key, f]) => (
            <StatusPip key={key} label={f.label} active={toolOutput[key] === true} caption={f.caption} hero />
          ))}
          {primaryPill.map(([key, f]) => (
            <ListTile key={key} label={f.label} items={toolOutput[key]} caption={f.caption} hero />
          ))}
          {primaryCrashloop && (
            <PipMeterTile label="CRASHLOOPING_PODS" pods={toolOutput.crashlooping_pods} caption={CRASHLOOP_CAPTION} hero />
          )}
        </div>
      )}

      <div style={{ opacity: secondaryOpacity }}>
        <SecondaryStrip entries={secondary} toolOutput={toolOutput} />
      </div>
    </div>
  );
}
