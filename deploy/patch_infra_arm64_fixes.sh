#!/usr/bin/env bash
# Real infra-image arm64 fixes found live on wardence-prod, beyond the
# original 7 custom app-service images -- these are the sock-shop
# DEPENDENCY images (a DB, a message broker) that also turned out to be
# amd64-only, found only once the full namespace was checked pod-by-pod
# after the app-service patch.
#
# Run this ON wardence-prod, any time after README.md section 1's base
# apply. rabbitmq's fix applies immediately (just a tag bump, no
# separate build needed). catalogue-db/user-db's fixes require their
# own rebuild scripts to have been run FIRST (rebuild_arm64_catalogue_db.sh,
# rebuild_arm64_user_db.sh, run from WSL2/wherever docker+buildx live) --
# this script will fail loudly on those two if the GHCR images don't
# exist yet, rather than silently skipping.
#
# Usage: bash deploy/patch_infra_arm64_fixes.sh

set -euo pipefail

NAMESPACE="sock-shop"
REGISTRY="ghcr.io/aayush8392"

echo "-- rabbitmq: bumping 3.6.8-management (real, checked: amd64-only,"
echo "   predates Docker Hub multi-arch manifest lists) to 3.13-management"
echo "   (real, checked: confirmed multi-arch including arm64) --"
kubectl set image deployment/rabbitmq rabbitmq=rabbitmq:3.13-management -n "${NAMESPACE}"
kubectl rollout status deployment/rabbitmq -n "${NAMESPACE}" --timeout=180s || \
  echo "WARNING: rabbitmq did not roll out cleanly -- check 'kubectl describe pod -n ${NAMESPACE} -l name=rabbitmq'"

echo
echo "-- catalogue-db / user-db: real GHCR arm64 rebuilds --"
for dep in catalogue-db user-db; do
  image="${REGISTRY}/${dep}:0.3.0-arm64"
  # NOTE: no pre-flight existence check here -- `k3s crictl pull` doesn't
  # use the Kubernetes imagePullSecret (that's a kubelet-level mechanism
  # via ghcr-pull-secret, wired up in provision_wardence_prod.sh); it only
  # consults containerd's own registry auth config, which isn't set up.
  # Against a real PRIVATE GHCR image that gives a false "not found"
  # every time, even for an image that pulls fine via kubelet. If the
  # image genuinely doesn't exist yet, `kubectl rollout status` below
  # will time out and report it clearly instead.
  echo "   ${dep} -> ${image}"
  kubectl set image "deployment/${dep}" "${dep}=${image}" -n "${NAMESPACE}"
done

echo
echo "-- Waiting for catalogue-db/user-db rollouts --"
for dep in catalogue-db user-db; do
  echo "   waiting on ${dep}..."
  if ! kubectl rollout status "deployment/${dep}" -n "${NAMESPACE}" --timeout=180s; then
    echo "WARNING: ${dep} did not roll out cleanly within 180s -- check"
    echo "         'kubectl describe pod -n ${NAMESPACE} -l name=${dep}' and"
    echo "         'kubectl logs -n ${NAMESPACE} -l name=${dep}'"
  fi
done

echo
echo "-- Final state --"
kubectl get pods -n "${NAMESPACE}" -l 'name in (rabbitmq,catalogue-db,user-db)'

echo
echo "Done. If catalogue-db/user-db are Running/Ready, this closes the last"
echo "of the arm64-incompatibility gaps found in the full pod sweep."
echo "user-db specifically: worth a real data-seeding check (see"
echo "rebuild_arm64_user_db.sh's own note) once the user service can"
echo "actually log in against it, not just 'process is up'."
