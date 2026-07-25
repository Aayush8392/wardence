"""
Phase D setup step 1: builds the full 7x7 = 49 ordered-pair matrix (6 real
auto-fix classes + "none" control), shuffles it randomly, and splits it
into two batch files (24 + 25 pairs) so an overnight run can go through
as two back-to-back scripts with independent timing.

Usage:
    python3 phase_d_generate_batches.py [--seed N]   # omit --seed for a
                                                       # fresh random split

Writes (this directory):
    phase_d_batch1.json (24 pairs)
    phase_d_batch2.json (25 pairs)
"""

import argparse
import json
import random
from pathlib import Path

REAL_CLASSES = [
    "crash-loop", "oom", "disk-full",
    "cpu-throttling", "under-provisioned-replicas", "bad-rollout",
]
ALL_CLASSES = REAL_CLASSES + ["none"]

OUT_DIR = Path(__file__).parent


def build_pairs() -> list[tuple[str, str]]:
    return [(a, b) for a in ALL_CLASSES for b in ALL_CLASSES]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=None, help="set for a reproducible split")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    pairs = build_pairs()
    assert len(pairs) == 49, f"expected 49 pairs (7x7), got {len(pairs)}"
    random.shuffle(pairs)

    batch1, batch2 = pairs[:24], pairs[24:]
    assert len(batch1) == 24 and len(batch2) == 25

    (OUT_DIR / "phase_d_batch1.json").write_text(json.dumps(batch1, indent=2))
    (OUT_DIR / "phase_d_batch2.json").write_text(json.dumps(batch2, indent=2))

    print("Wrote phase_d_batch1.json (24 pairs) and phase_d_batch2.json (25 pairs).")
    print(f"Seed: {args.seed if args.seed is not None else '(none -- rerun for a different split, not reproducible)'}")


if __name__ == "__main__":
    main()
