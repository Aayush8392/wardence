#!/usr/bin/env bash
# Real deployment-readiness need (2026-08-25 session): the Sock Shop
# storefront (the `front-end` pod, real app port 8079) has only ever
# been reachable via an unsupervised `kubectl port-forward` or an SSH
# tunnel -- fine for manual dev testing, not something a public visitor
# (or a Cloudflare quick tunnel sitting in front of it) can depend on.
#
# Fix: a dedicated NodePort Service pointing at the same real `front-end`
# pods the existing ClusterIP service already selects -- does NOT modify
# or replace that ClusterIP service, so nothing else depending on its
# name/DNS (e.g. other in-cluster services calling front-end) is
# affected. Same shape as patch_prometheus_nodeport.sh / patch_loki_nodeport.sh
# -- selector fetched live, not hardcoded, to avoid drifting if the
# base manifest's labels ever change.
#
# Usage: bash patch_frontend_nodeport.sh

set -euo pipefail

NAMESPACE="sock-shop"
TARGET_SERVICE="front-end"
NODE_PORT="30079"

echo "Fetching real selector from $TARGET_SERVICE..."
REAL_SELECTOR=$(kubectl get svc "$TARGET_SERVICE" -n "$NAMESPACE" -o jsonpath='{.spec.selector}')
echo "  $REAL_SELECTOR"

if [ -z "$REAL_SELECTOR" ] || [ "$REAL_SELECTOR" = "{}" ]; then
  echo "ERROR: $TARGET_SERVICE has no selector, or doesn't exist -- check the" >&2
  echo "real service name first: kubectl get svc -n $NAMESPACE | grep -i front-end" >&2
  exit 1
fi

echo "Creating wardence-frontend-nodeport (NodePort $NODE_PORT -> 8079)..."
kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: wardence-frontend-nodeport
  namespace: $NAMESPACE
  labels:
    app: wardence-frontend-nodeport
spec:
  type: NodePort
  selector: $REAL_SELECTOR
  ports:
    - port: 8079
      targetPort: 8079
      nodePort: $NODE_PORT
EOF

echo
echo "Verifying it actually selected real endpoints (not zero)..."
kubectl get endpoints wardence-frontend-nodeport -n "$NAMESPACE"

echo
echo "Done. Confirm real access with:"
echo "  curl -sI 'http://localhost:$NODE_PORT/' | head -5"
echo
echo "Then this is what the storefront cloudflared tunnel should point at:"
echo "  http://localhost:$NODE_PORT"
