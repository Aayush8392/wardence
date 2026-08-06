// Real fallback-chain funnel, from tier_accuracy.by_tier_provider (real
// dispatch resolution share + accuracy per provider that actually
// serves a live episode). Deliberately does NOT fabricate an OpenRouter
// stage with a fake accuracy number -- OpenRouter is comparison-only
// (comparison_sampling_log), it never has a real episode_snapshots row
// and therefore never appears in this real data at all. Shown as an
// honest "never dispatches" tile instead of a 4th real-looking stage,
// per the explicit decision not to fabricate what isn't there.
const KNOWN_ORDER = ["primary:cloudflare", "primary:deepinfra", "fallback:groq"];

function providerShortLabel(provider) {
  if (provider === "cloudflare") return "GEMMA";
  if (provider === "deepinfra") return "NEMOTRON";
  if (provider === "groq") return "GROQ";
  if (provider === "openrouter") return "OPENROUTER";
  return provider.toUpperCase();
}

export default function FallbackFunnel({ tierAccuracy }) {
  const byTierProvider = tierAccuracy?.by_tier_provider ?? {};
  const orderedKeys = [
    ...KNOWN_ORDER.filter((k) => byTierProvider[k]),
    ...Object.keys(byTierProvider).filter((k) => !KNOWN_ORDER.includes(k)),
  ];
  const hasOpenRouter = orderedKeys.some((k) => k.includes("openrouter"));

  return (
    <div className="bg-surface-container-low border border-outline-variant p-5">
      <span className="font-label-caps text-[11px] text-on-surface-variant block mb-4">FALLBACK_CHAIN_FUNNEL</span>

      <div className="space-y-3">
        {orderedKeys.map((key) => {
          const v = byTierProvider[key];
          const isPrimary = v.tier === "primary";
          return (
            <div key={key} className="flex items-center gap-4">
              <div className="w-20 text-right font-data-mono text-lg text-primary">
                {v.pct_of_total != null ? `${v.pct_of_total}%` : "—"}
              </div>
              <div
                className={`flex-1 px-4 py-2.5 border ${
                  isPrimary ? "border-primary/30 bg-primary/10" : "border-warning-amber/30 bg-warning-amber/10"
                }`}
              >
                <span className="font-data-mono text-sm text-on-surface">
                  {providerShortLabel(v.provider)} ({v.tier.toUpperCase()})
                </span>
                <span className="font-data-mono text-sm text-on-surface-variant ml-2">
                  — ACC: {v.accuracy != null ? `${(v.accuracy * 100).toFixed(0)}%` : "—"}
                </span>
                <span className="text-[10px] text-on-surface-variant/60 ml-2">({v.resolved} real episodes)</span>
              </div>
            </div>
          );
        })}

        {!hasOpenRouter && (
          <div className="flex items-center gap-4 opacity-50">
            <div className="w-20 text-right font-data-mono text-lg text-on-surface-variant">—</div>
            <div className="flex-1 px-4 py-2.5 border border-outline-variant border-dashed">
              <span className="font-data-mono text-sm text-on-surface-variant">OPENROUTER (FALLBACK)</span>
              <span className="text-[10px] text-on-surface-variant/70 ml-2">
                — comparison-only, never actually dispatches a real episode
              </span>
            </div>
          </div>
        )}
      </div>

      <p className="text-[10px] text-on-surface-variant mt-4">
        Real resolution share of {tierAccuracy?.total_resolved_with_tier ?? 0} tiered episodes. Groq's fallback
        share is genuinely this thin ({byTierProvider["fallback:groq"]?.resolved ?? 0} real episodes) — the primary
        tier almost never fails.
      </p>
    </div>
  );
}
