#!/usr/bin/env bash
# Swaps the upstream microservices-demo manifest's x86 weaveworksdemos/*
# images over to the 7 real arm64 rebuilds pushed to GHCR (see
# wardence_buildlog.md's arm64 rebuild sessions, 2026-08-23). Needed
# because deploy/README.md section 1's `kubectl apply -f
# .../complete-demo.yaml` pulls the ORIGINAL x86-only images -- on an
# arm64-only host (Oracle A1) those 7 pods would sit in
# CrashLoopBackOff/exec-format-error without this.
#
# Run this ON wardence-prod, right after README.md section 1's
# complete-demo.yaml apply, before applying anything else in the runbook.
#
#   bash deploy/patch_arm64_images.sh
#
# Assumption (standard, unmodified upstream convention -- verified against
# the real manifest structure this project has been reading from all
# along, not guessed): each of these 7 deployments has exactly one
# container, and that container's name matches the deployment/image name
# exactly (e.g. deployment "carts" -> container "carts"). If a future
# upstream manifest version changes this, `kubectl set image` below will
# fail loudly (unknown container name) rather than silently doing nothing.
#
# Real tags, as actually pushed (from wardence_buildlog.md, not assumed):
#   front-end:0.3.12-arm64   carts:0.4.8-arm64   orders:0.4.7-arm64
#   queue-master:0.3.1-arm64 shipping:0.4.8-arm64
#   catalogue:0.3.5-arm64    payment:0.4.3-arm64  user:0.4.7-arm64

set -euo pipefail

NAMESPACE="sock-shop"
REGISTRY="ghcr.io/aayush8392"

# name:tag pairs -- deployment name, container name (same), and full
# arm64 image reference
declare -A IMAGES=(
  [front-end]="${REGISTRY}/front-end:0.3.12-arm64"
  [carts]="${REGISTRY}/carts:0.4.8-arm64"
  [orders]="${REGISTRY}/orders:0.4.7-arm64"
  [queue-master]="${REGISTRY}/queue-master:0.3.1-arm64"
  [shipping]="${REGISTRY}/shipping:0.4.8-arm64"
  [catalogue]="${REGISTRY}/catalogue:0.3.5-arm64"
  [payment]="${REGISTRY}/payment:0.4.3-arm64"
  [user]="${REGISTRY}/user:0.4.7-arm64"
)

echo "-- Confirming the sock-shop deployments exist before patching --"
for dep in "${!IMAGES[@]}"; do
  if ! kubectl get deployment "${dep}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "ERROR: deployment '${dep}' not found in namespace '${NAMESPACE}'." >&2
    echo "       Run README.md section 1's complete-demo.yaml apply first." >&2
    exit 1
  fi
done
echo "   all 8 deployments present."

echo
echo "-- Patching each deployment to its real arm64 GHCR image --"
for dep in "${!IMAGES[@]}"; do
  image="${IMAGES[$dep]}"
  echo "   ${dep} -> ${image}"
  kubectl set image "deployment/${dep}" "${dep}=${image}" -n "${NAMESPACE}"
done

echo
echo "-- Waiting for rollouts (this pulls the new images -- can take a"
echo "   while on first run under QEMU-free native arm64, but still real"
echo "   network pulls from GHCR) --"
for dep in "${!IMAGES[@]}"; do
  echo "   waiting on ${dep}..."
  if ! kubectl rollout status "deployment/${dep}" -n "${NAMESPACE}" --timeout=180s; then
    echo "WARNING: ${dep} did not roll out cleanly within 180s -- check"
    echo "         'kubectl describe pod -n ${NAMESPACE} -l name=${dep}'"
    echo "         (a common real cause: ghcr-pull-secret missing/wrong --"
    echo "         see provision_wardence_prod.sh's GHCR step)."
  fi
done

echo
echo "-- Final image check --"
for dep in "${!IMAGES[@]}"; do
  actual=$(kubectl get deployment "${dep}" -n "${NAMESPACE}" \
    -o jsonpath="{.spec.template.spec.containers[0].image}")
  echo "   ${dep}: ${actual}"
done

echo
echo "Done. If any deployment above is still on its old weaveworksdemos/*"
echo "tag, or any rollout warned above, resolve that before continuing to"
echo "the next section of deploy/README.md."
