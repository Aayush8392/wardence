// Real RBAC scope, read directly from p3_trust_action/manifests/rbac.yaml --
// static because the cage itself is a fixed config, not per-episode data.
const ALLOWED_VERBS = ["get", "list", "patch"];
const BLOCKED_VERBS = ["delete", "exec", "create"];

export default function SecurityCage() {
  return (
    <div className="border border-outline-variant bg-surface-container-low p-4">
      <div className="font-label-caps text-[10px] text-on-surface-variant tracking-widest mb-3">
        BLAST-RADIUS CAGE — fixed RBAC scope
      </div>
      <div className="text-xs mb-2">
        <span className="text-[#238636]">ALLOWED </span>
        {ALLOWED_VERBS.map((v) => (
          <code key={v} className="font-data-mono mr-2">{v}</code>
        ))}
      </div>
      <div className="text-xs">
        <span className="text-error">BLOCKED </span>
        {BLOCKED_VERBS.map((v) => (
          <code key={v} className="font-data-mono mr-2">{v}</code>
        ))}
      </div>
    </div>
  );
}
