#!/usr/bin/env bash
# Real deployment-readiness need (2026-08-23 session): once operator_api.py
# runs on the Oracle host itself (decided: bare process, NOT an in-cluster
# pod -- same shape as today's WSL2 setup, no new infra), it needs a way to
# reach Prometheus that doesn't depend on a fragile, unsupervised
# `kubectl port-forward` staying alive forever.
#
# Fix: a dedicated NodePort Service pointing at the same real Prometheus
# pods the existing ClusterIP service already selects -- does NOT modify
# or replace that ClusterIP service (monitoring-kube-prometheus-prometheus),
# so nothing else in the cluster that depends on its name/DNS is affected.
# Since operator_api.py runs on the SAME node as the k3s node (Oracle A1 is
# a single-node cluster, same as the current WSL2/laptop setup), a NodePort
# is reachable at http://localhost:<NODE_PORT> from that host directly --
# no port-forward process needed at all.
#
# The real target Service's own selector is fetched live and embedded as
# JSON (valid YAML flow-style mapping) rather than reconstructed by hand --
# avoids hardcoding a selector that could drift if the Helm chart's label
# scheme ever changes.
#
# Usage: bash patch_prometheus_nodeport.sh

set -euo pipefail

NAMESPACE="monitoring"
TARGET_SERVICE="monitoring-kube-prometheus-prometheus"
NODE_PORT="30090"

echo "Fetching real selector from $TARGET_SERVICE..."
REAL_SELECTOR=$(kubectl get svc "$TARGET_SERVICE" -n "$NAMESPACE" -o jsonpath='{.spec.selector}')
echo "  $REAL_SELECTOR"

if [ -z "$REAL_SELECTOR" ] || [ "$REAL_SELECTOR" = "{}" ]; then
  echo "ERROR: $TARGET_SERVICE has no selector, or doesn't exist -- check the" >&2
  echo "real service name first: kubectl get svc -n $NAMESPACE | grep -i prometheus" >&2
  exit 1
fi

echo "Creating wardence-prometheus-nodeport (NodePort $NODE_PORT -> 9090)..."
kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: wardence-prometheus-nodeport
  namespace: $NAMESPACE
  labels:
    app: wardence-prometheus-nodeport
spec:
  type: NodePort
  selector: $REAL_SELECTOR
  ports:
    - port: 9090
      targetPort: 9090
      nodePort: $NODE_PORT
EOF

echo
echo "Verifying it actually selected real endpoints (not zero)..."
kubectl get endpoints wardence-prometheus-nodeport -n "$NAMESPACE"

echo
echo "Done. Confirm real query access with:"
echo "  curl -s 'http://localhost:$NODE_PORT/api/v1/query?query=up' | head -c 300"
echo
echo "Then set on the operator_api.py host:"
echo "  export PROMETHEUS_URL=\"http://localhost:$NODE_PORT\""
