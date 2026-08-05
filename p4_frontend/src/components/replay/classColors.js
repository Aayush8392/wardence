// Fixed per-class color palette, carried over from the dot-sphere prototype
// (prototypes/dot_sphere_prototype.html) so the sphere, its legend, and any
// other per-class UI stay visually consistent. Real class names, not
// placeholders -- matches injector.py's FAULT_CONFIG roster. A class not
// in this map (future roster growth) falls back to a deterministic hash
// color rather than crashing.
export const CLASS_COLORS = {
  "crash-loop": 0xff6b6b,
  "oom": 0xffa94d,
  "disk-full": 0xffd43b,
  "cpu-throttling": 0x69db7c,
  "under-provisioned-replicas": 0x38d9a9,
  "bad-rollout": 0x4dabf7,
  "network-latency": 0x748ffc,
  "network-partition": 0x9775fa,
  "memory-leak": 0xc19a6b,
  "connection-pool-exhaustion": 0xf783ac,
  "init-failure": 0xe599f7,
  "session-cart-failure": 0x66d9e8,
};

function hashColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return (h & 0xffffff) | 0x404040; // keep it away from near-black
}

export function colorForClass(name) {
  return CLASS_COLORS[name] ?? hashColor(name);
}

export function hexToCss(hex) {
  return `#${hex.toString(16).padStart(6, "0")}`;
}
