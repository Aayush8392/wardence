#!/usr/bin/env bash
# Same real need as patch_prometheus_nodeport.sh, for Loki instead:
# detector_service.py (p5_dl_hardening, port 8010) reads real log
# windows directly from Loki via log_sequence_pipeline.py's LOKI_URL --
# on wardence-prod that service runs as a bare host process (same
# placement decision as operator_api.py/p3_agent.py), not an in-cluster
# pod, so it needs a stable, non-port-forward way to reach Loki.
#
# Does NOT modify or replace the existing `loki` ClusterIP service
# (installed via `helm install loki grafana/loki`, see
# deploy/install_observability_stack.sh) -- only adds a second NodePort
# Service pointing at the same real pods, same non-invasive pattern as
# the Prometheus script.
#
# Usage: bash patch_loki_nodeport.sh

set -euo pipefail

NAMESPACE="monitoring"
TARGET_SERVICE="loki"
NODE_PORT="30100"

echo "Fetching real selector from $TARGET_SERVICE..."
REAL_SELECTOR=$(kubectl get svc "$TARGET_SERVICE" -n "$NAMESPACE" -o jsonpath='{.spec.selector}')
echo "  $REAL_SELECTOR"

if [ -z "$REAL_SELECTOR" ] || [ "$REAL_SELECTOR" = "{}" ]; then
  echo "ERROR: $TARGET_SERVICE has no selector, or doesn't exist -- check the" >&2
  echo "real service name first: kubectl get svc -n $NAMESPACE | grep -i loki" >&2
  exit 1
fi

echo "Creating wardence-loki-nodeport (NodePort $NODE_PORT -> 3100)..."
kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: wardence-loki-nodeport
  namespace: $NAMESPACE
  labels:
    app: wardence-loki-nodeport
spec:
  type: NodePort
  selector: $REAL_SELECTOR
  ports:
    - port: 3100
      targetPort: 3100
      nodePort: $NODE_PORT
EOF

echo
echo "Verifying it actually selected real endpoints (not zero)..."
kubectl get endpoints wardence-loki-nodeport -n "$NAMESPACE"

echo
echo "Done. Confirm real query access with:"
echo "  curl -s 'http://localhost:$NODE_PORT/loki/api/v1/labels' | head -c 300"
echo
echo "Then set on the detector_service.py host:"
echo "  export LOKI_URL=\"http://localhost:$NODE_PORT\""
