#!/bin/sh
# Wardence traffic generator entrypoint.
# Alternates baseline.js (570s continuous low-rate) and burst.js (30s
# spike) forever -- each cycle is ~600s (10min), giving one burst per
# baseline window rather than needing k6 to schedule bursts internally.
#
# -o experimental-prometheus-rw pushes real request-latency metrics
# (http_req_duration) to Prometheus so the network-latency fault class
# has an actual observed-latency signal to diagnose against, instead of
# only the injected delay itself. Built into grafana/k6 since v0.42.0,
# no custom xk6 build needed. K6_PROMETHEUS_RW_SERVER_URL and
# K6_PROMETHEUS_RW_TREND_STATS come from the Deployment's env.

set -e

while true; do
  echo "[traffic_gen] starting baseline (570s)"
  k6 run -o experimental-prometheus-rw /scripts/baseline.js

  echo "[traffic_gen] starting burst (30s)"
  k6 run -o experimental-prometheus-rw /scripts/burst.js
done
