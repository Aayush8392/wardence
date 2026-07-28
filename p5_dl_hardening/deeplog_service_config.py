"""
Locked per-service drain3 config for DeepLog, derived from real
measurement (see wardence_buildlog.md, 2026-07-29 session):

- depth=4 (drain3 default) merges distinct methods into one wildcard
  cluster when several real methods share the same real token count
  (confirmed for `user`: GetAddresses/GetCards/GetUsers/PostCard/
  Register all 6 tokens -> merged at depth=4). depth=6 fixes this for
  `user` and `front-end` (both real key=value/access-log style lines,
  confirmed via direct before/after comparison -- 33->39 templates for
  front-end, all real new splits, not noise).
- depth=6 is NOT safe for `orders`: it logs Java exception stack
  traces, and depth=6 over-fragments them by hard-splitting on the
  specific class/method in each stack frame instead of recognizing
  them as the same `at <*>` pattern (23->73 templates, confirmed noise
  via direct comparison). `orders` stays at depth=4.
- `catalogue` is identical at both depths (3 templates either way) --
  kept at depth=4 for consistency/no reason to change it.

DeepLog candidate tier (locked same session):
  Tier 1 (real model): front-end, orders, user
  Tier 2 (thin, deferred): catalogue, queue-master
  Not viable (silence-detection only): carts, payment, shipping,
    catalogue-db, session-db
"""

SERVICE_DRAIN_DEPTH = {
    "user": 6,
    "front-end": 6,
    "orders": 4,
    "catalogue": 4,
    "queue-master": 4,  # not yet re-verified at a tuned depth -- default until checked
}

DEFAULT_DEPTH = 4  # for any service not explicitly listed above

DEEPLOG_TIER_1 = ["front-end", "orders", "user"]
DEEPLOG_TIER_2_THIN = ["catalogue", "queue-master"]
DEEPLOG_NOT_VIABLE = ["carts", "payment", "shipping", "catalogue-db", "session-db"]

# Real, locked primary-detector decision per Track-B service (Kimi review
# 07 follow-up, 2026-07-29): NOT template count alone -- transition
# DETERMINISM. queue-master's near-1:1 alternation (D~0.99) makes any
# deviation genuinely rare under the model; catalogue's 3-way mixing
# (D~0.5-0.6) means even its rarest transition shows up in most 20-event
# windows by pure chance (coupon-collector exposure ~0.88), so a
# min-per-step HMM scorer there produced a real 25.8% false-positive
# rate -- confirmed a correct model describing a genuinely
# low-determinism process, not a bug. See track_b_suitability_check.py
# for the reusable D/H_norm/exposure test this was derived from, meant
# to predict this upfront for any future Track-B candidate service.
TRACK_B_PRIMARY_DETECTOR = {
    "queue-master": "hmm",
    "catalogue": "spc",
}

# Real Loki-scoped services for the log-sequence pipeline (DeepLog Tier 1
# + HMM/SPC Tier 2 only -- not the full 10-service roster, since the
# other 5 are confirmed to have no viable log-sequence signal at all,
# see reviews/07_deeplog_coverage_kimi_review.md).
PIPELINE_SERVICES = DEEPLOG_TIER_1 + DEEPLOG_TIER_2_THIN

# Real, confirmed noise templates to exclude from Track B's (HMM/SPC)
# feature vectors -- found for queue-master, 2026-07-29 session:
# 77.8% of its real accumulated traffic is a dead DockerSpawner feature
# (tries to reach /var/run/docker.sock, which doesn't exist under
# k3s/containerd) constantly retrying and erroring, unrelated to any
# real fault-relevant behavior. Matched by substring against the real
# template text (not cluster IDs, which aren't guaranteed stable across
# separate miner instances). Empty list = no filtering needed for that
# service (confirmed for `catalogue`: all 3 of its templates are real
# business logic, no noise found).
SERVICE_NOISE_MARKERS = {
    "queue-master": ["RetryExec", "AFUNIXSocketException"],
    "catalogue": [],
}


def get_noise_markers(service: str):
    return SERVICE_NOISE_MARKERS.get(service, [])


def is_noise_template(service: str, template: str) -> bool:
    return any(marker in template for marker in get_noise_markers(service))


def get_depth(service: str) -> int:
    return SERVICE_DRAIN_DEPTH.get(service, DEFAULT_DEPTH)


# Same real masking rules used throughout every check_*.py script in this
# session -- kept as the one shared definition so template IDs mined here
# are directly comparable to everything already measured.
from drain3.masking import MaskingInstruction  # noqa: E402

MASKING_INSTRUCTIONS = [
    MaskingInstruction(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", "TIMESTAMP"),
    MaskingInstruction(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "UUID"
    ),
    MaskingInstruction(r"\b[0-9a-f]{24}\b", "OBJID"),
    MaskingInstruction(r"\d+(\.\d+)?(µs|us|ms|ns|s)\b", "DURATION"),
    MaskingInstruction(r"\d+\.\d+ (µs|us|ms|ns|s)\b", "DURATION"),
    MaskingInstruction(r"\b\d+\.\d+\b", "NUM"),
    MaskingInstruction(r"\b\d+\b", "NUM"),
]
