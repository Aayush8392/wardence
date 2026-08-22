#!/usr/bin/env bash
# Real gap found 2026-08-23 during the deployment-readiness cluster-state
# audit (Point 3 of the deployment checklist): orders-db is ALSO pinned to
# mongo:4.4 live, same fix as carts-db's own documented OP_QUERY bug
# (mongo:latest drifts past 5.1, which removes the legacy OP_QUERY wire
# protocol orders' driver speaks) -- but this was NEVER captured as its
# own script or buildlog entry. Confirmed via the deployment's own
# `kubectl.kubernetes.io/last-applied-configuration` annotation, which
# still shows the ORIGINAL upstream image ("mongo", i.e. mongo:latest) --
# proving the mongo:4.4 pin was applied imperatively (kubectl set image,
# same mechanism as carts-db's) and never went through `kubectl apply`,
# which is why it never got written up alongside carts-db's version.
#
# Reconstructed 2026-08-23 from the live cluster's real current spec.
# Not tracked in any committed manifest until now. Rerun after any full
# redeploy, same pattern as patch_carts_db_mongo_pin.sh.
#
# Usage: bash patch_orders_db_mongo_pin.sh

set -euo pipefail

NAMESPACE="sock-shop"
DEPLOYMENT="orders-db"
PINNED_IMAGE="mongo:4.4"

echo "Current image:"
kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
echo

echo "Patching $DEPLOYMENT to $PINNED_IMAGE..."
kubectl set image deployment/"$DEPLOYMENT" \
  "$DEPLOYMENT"="$PINNED_IMAGE" -n "$NAMESPACE"

echo "Waiting for rollout..."
kubectl rollout status deployment/"$DEPLOYMENT" -n "$NAMESPACE" --timeout=180s

echo
echo "New image:"
kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
echo

echo
echo "Done. Real checkout traffic (POST /orders) is what actually exercises"
echo "this -- confirm clean via a real checkout or:"
echo "  ORDERS_POD=\$(kubectl get pods -n $NAMESPACE -l name=orders --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')"
echo "  kubectl logs -n $NAMESPACE \$ORDERS_POD --since=2m | grep -i 'OP_QUERY\\|UncategorizedMongoDb' || echo 'clean'"
