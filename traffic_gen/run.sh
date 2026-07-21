#!/bin/sh
# Wardence traffic generator entrypoint.
# Alternates baseline.js (570s continuous low-rate) and burst.js (30s
# spike) forever -- each cycle is ~600s (10min), giving one burst per
# baseline window rather than needing k6 to schedule bursts internally.

set -e

while true; do
  echo "[traffic_gen] starting baseline (570s)"
  k6 run /scripts/baseline.js

  echo "[traffic_gen] starting burst (30s)"
  k6 run /scripts/burst.js
done
