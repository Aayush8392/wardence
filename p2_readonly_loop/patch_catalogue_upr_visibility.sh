#!/bin/bash
# ORACLE ONLY (wardence-prod). Do NOT run on WSL2.
#
# under-provisioned-replicas' whole premise is "1 catalogue replica
# can't keep up, scale to 3 fixes it". On Oracle's fast Ampere cores,
# catalogue at its stock 200m CPU limit serves a 130-VU hold at only
# ~0.36s p50 / ~0.63s p95 -- barely perceptible on the storefront, which
# defeats the point of a live demo. Tightening the CPU limit makes the
# under-provisioning genuinely bite:
#
#   catalogue limits.cpu 200m -> 75m  (requests 100m -> 50m)
#     1 replica, 130-VU hold : p50 ~0.9s / p95 ~1.5s  (clearly slow)
#     3 replicas (post-fix)  : p95 ~0.14s              (recovered)
#   Full tuning data: check_upr_cpu_limit_tuning.py, 2026-08-28.
#
# The fault mechanism, diagnosis logic, fix action and scoring are all
# UNCHANGED -- only catalogue's per-pod CPU envelope. The matching
# probe-threshold recalibration (140 -> 240) is set via
# WARDENCE_UPR_PROBE_THRESHOLD_MS on wardence-operator-api.service /
# wardence-p3-agent.service (see deploy/*.service).
#
# livenessProbe is loosened (timeoutSeconds 1 -> 3, failureThreshold
# 3 -> 4) so the occasional CPU-throttled /health check can't stack 3
# failures during a hold and trigger a restart -> crash-loop
# misdiagnosis. /health max was 1.08s in tuning; 3s timeout covers it.
#
# Idempotent -- safe to re-run. Reverts with:
#   kubectl patch deployment catalogue -n sock-shop --type=strategic -p \
#     '{"spec":{"template":{"spec":{"containers":[{"name":"catalogue","resources":{"limits":{"cpu":"200m","memory":"200Mi"},"requests":{"cpu":"100m","memory":"100Mi"}},"livenessProbe":{"timeoutSeconds":1,"failureThreshold":3}}}]}}}}'
#
# Usage: bash patch_catalogue_upr_visibility.sh
set -euo pipefail

NS="sock-shop"

echo "Current catalogue resources / livenessProbe:"
kubectl get deployment catalogue -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].resources}{"\n"}{.spec.template.spec.containers[0].livenessProbe}{"\n"}'

HAS_LIVENESS=$(kubectl get deployment catalogue -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].livenessProbe.httpGet.path}')

if [ -n "$HAS_LIVENESS" ]; then
  PATCH='{"spec":{"template":{"spec":{"containers":[{"name":"catalogue","resources":{"limits":{"cpu":"75m","memory":"200Mi"},"requests":{"cpu":"50m","memory":"100Mi"}},"livenessProbe":{"timeoutSeconds":3,"failureThreshold":4}}]}}}}'
  echo "Patching resources + loosening livenessProbe (path=$HAS_LIVENESS)..."
else
  PATCH='{"spec":{"template":{"spec":{"containers":[{"name":"catalogue","resources":{"limits":{"cpu":"75m","memory":"200Mi"},"requests":{"cpu":"50m","memory":"100Mi"}}}]}}}}'
  echo "catalogue has no httpGet livenessProbe -- patching resources only."
fi

kubectl patch deployment catalogue -n "$NS" --type=strategic -p "$PATCH"
kubectl rollout status deployment/catalogue -n "$NS" --timeout=180s

echo
echo "Done. New state:"
kubectl get deployment catalogue -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].resources}{"\n"}{.spec.template.spec.containers[0].livenessProbe}{"\n"}'
echo
echo "Remember: set WARDENCE_UPR_PROBE_THRESHOLD_MS=240 on"
echo "  wardence-operator-api.service AND wardence-p3-agent.service,"
echo "  daemon-reload + restart both, and (for manual batch runs)"
echo "  export WARDENCE_UPR_PROBE_THRESHOLD_MS=240 in your shell."
