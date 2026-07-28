#!/usr/bin/env bash
# Real bug fix (2026-07-28): carts-db was running unpinned `mongo` (i.e.
# mongo:latest), which resolves to whatever the newest MongoDB build is at
# any given time. carts' own driver (mongodb-driver-core 3.2.2, bundled in
# weaveworksdemos/carts:0.4.8) only speaks the legacy OP_QUERY wire
# protocol, which MongoDB removed in 5.1+ -- so every real DB query from
# carts was throwing "Unsupported OP_QUERY command: find", confirmed via a
# direct Loki log sample (2026-07-28). This spammed ~230 log lines/sec,
# dominating carts' log volume with error stack traces instead of real
# request traffic -- a problem for any future log-based work (DeepLog),
# not just noise.
#
# Fix: pin carts-db to mongo:4.4, a known-compatible, still-LTS version
# that still supports OP_QUERY. Not tracked in a local manifest (Sock Shop
# was deployed straight from the upstream official manifest URL), so this
# script is the reproducible fix -- rerun it after any future full
# redeploy, same pattern as this project's other patch_*.sh scripts.
#
# Usage: bash patch_carts_db_mongo_pin.sh

set -euo pipefail

NAMESPACE="sock-shop"
DEPLOYMENT="carts-db"
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
echo "Waiting 15s for carts to reconnect, then sending a few real requests"
echo "to confirm the OP_QUERY error is actually gone (not just that the"
echo "pod came up)..."
sleep 15

CARTS_POD=$(kubectl get pods -n "$NAMESPACE" -l name=carts \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')

for i in 1 2 3 4 5; do
  kubectl exec -n "$NAMESPACE" "$CARTS_POD" -- \
    curl -s -o /dev/null -w "request $i: HTTP %{http_code}\n" \
    "http://localhost:80/carts/testuser1/items" || true
done

echo
echo "Now check the real carts logs for any fresh OP_QUERY errors:"
echo "  kubectl logs -n $NAMESPACE $CARTS_POD --since=1m | grep -i 'OP_QUERY\|UncategorizedMongoDb' || echo 'clean'"
