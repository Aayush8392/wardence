// Single source of truth for the real 12-class v1 roster, mirroring
// p2_readonly_loop/injector.py's FAULT_CONFIG (target/namespace) and
// operator_api.py's IMPLEMENTED_CLASSES/AUTO_FIX_CLASSES/SAFE_DEMO_CLASSES
// (all 12 are demo-safe as of the 2026-08-1x reopen -- see that file's own
// comment on SAFE_DEMO_CLASSES for why). Every other Operator component
// should import from here rather than re-declaring its own class list, so
// a roster change only ever needs editing in one place.

export const AUTO_FIX_CLASSES = [
  "crash-loop",
  "oom",
  "disk-full",
  "cpu-throttling",
  "under-provisioned-replicas",
  "bad-rollout",
];

export const REPORT_ONLY_CLASSES = [
  "network-latency",
  "network-partition",
  "memory-leak",
  "connection-pool-exhaustion",
  "init-failure",
  "session-cart-failure",
];

export const ALL_CLASSES = [...AUTO_FIX_CLASSES, ...REPORT_ONLY_CLASSES];

// The classes whose active fault window is invisible on the real
// storefront by mechanism design (see wardence_frontend.md's
// "customer-visibility audit" session) -- the only ones the live-status
// readout (GET /operator/fault-status/{class}) is meaningful for.
// bad-rollout and init-failure REMOVED 2026-08-1x -- both were masked by
// the same "old healthy pod keeps serving" RollingUpdate default
// (maxSurge=25%/maxUnavailable=25%), and both were fixed the same real
// way, live-confirmed: patching the target Deployment's rollout strategy
// to maxUnavailable=100%/maxSurge=0% forces the old pod down before the
// new (broken) one comes up, producing a real, visible failure instead
// of a silently-masked one. init-failure's real confirmed symptom: a
// genuine 500 (IllegalStateException, "Unable to create order due to
// unspecified IO error") on checkout specifically, not a full-site
// outage like bad-rollout -- payment's own strategy was patched via
// `kubectl patch deployment payment -n sock-shop -p
// '{"spec":{"strategy":{"rollingUpdate":{"maxUnavailable":"100%","maxSurge":"0%"}}}}'`,
// same one-off live-cluster-only pattern as front-end's own fix (never
// a repo-tracked manifest, Sock Shop's deployments aren't version-
// controlled here).
export const INVISIBLE_CLASSES = ["disk-full", "memory-leak"];

// target/namespace verbatim from injector.py's real FAULT_CONFIG.
export const FAULT_TARGETS = {
  "crash-loop": { target: "carts", namespace: "sock-shop" },
  oom: { target: "catalogue", namespace: "sock-shop" },
  "disk-full": { target: "queue-master", namespace: "sock-shop" },
  "cpu-throttling": { target: "user", namespace: "sock-shop" },
  "under-provisioned-replicas": { target: "catalogue", namespace: "sock-shop" },
  "bad-rollout": { target: "front-end", namespace: "sock-shop" },
  "network-latency": { target: "orders", namespace: "sock-shop" },
  "network-partition": { target: "orders", namespace: "sock-shop" },
  "memory-leak": { target: "shipping", namespace: "sock-shop" },
  "connection-pool-exhaustion": { target: "catalogue-db", namespace: "sock-shop" },
  "init-failure": { target: "payment", namespace: "sock-shop" },
  "session-cart-failure": { target: "session-db", namespace: "sock-shop" },
};

export const CLASS_LABELS = {
  "crash-loop": "Crash-loop",
  oom: "OOM (Out of Memory)",
  "disk-full": "Disk-full Overflow",
  "cpu-throttling": "CPU Throttling",
  "under-provisioned-replicas": "Under-provisioned Replicas",
  "bad-rollout": "Bad Rollout",
  "network-latency": "Network Latency",
  "network-partition": "Network Partition",
  "memory-leak": "Memory Leak",
  "connection-pool-exhaustion": "Connection-pool Exhaustion",
  "init-failure": "Init/Startup-probe Failure",
  "session-cart-failure": "Session/Cart-store Failure",
};

// Real per-service groupings, derived directly from FAULT_TARGETS above --
// not a separately-maintained list, so a target change can't silently
// desync the two.
export function groupByTarget() {
  const groups = {};
  for (const fc of ALL_CLASSES) {
    const { target } = FAULT_TARGETS[fc];
    if (!groups[target]) groups[target] = [];
    groups[target].push(fc);
  }
  return groups;
}
