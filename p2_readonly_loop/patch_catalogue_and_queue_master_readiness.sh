#!/usr/bin/env bash
# Two real readiness-probe fixes from the 2026-08-15 live storefront-
# testing session, bundled here since both are simple retunes (unlike
# carts' fuller cold-start fix, kept in its own script):
#
# 1. catalogue's readinessProbe was over-conservative (initialDelaySeconds
#    180) despite being a fast Go binary (confirmed via its own
#    `command: ["/app"]`). Tightened to 15s -- live-confirmed via a full
#    oom-trigger-to-fixed-storefront cycle loading instantly on refresh.
#
# 2. queue-master had NO readinessProbe at all, same class of gap as
#    carts -- fixed the same way, lighter margin since it's a lightweight
#    Go service, not slow Java.
#
# Reconstructed 2026-08-23 from the live cluster's real current spec
# (deployment-readiness effort) -- not tracked in any committed manifest.
# Rerun after any full redeploy.
#
# Usage: bash patch_catalogue_and_queue_master_readiness.sh

set -euo pipefail

NAMESPACE="sock-shop"

echo "1/2: patching catalogue's readinessProbe (initialDelaySeconds 180 -> 15)..."
kubectl patch deployment catalogue -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "catalogue",
            "readinessProbe": {
              "httpGet": {"path": "/health", "port": 80, "scheme": "HTTP"},
              "initialDelaySeconds": 15,
              "periodSeconds": 3,
              "timeoutSeconds": 1,
              "successThreshold": 1,
              "failureThreshold": 3
            }
          }
        ]
      }
    }
  }
}'
kubectl rollout status deployment/catalogue -n "$NAMESPACE" --timeout=120s

echo "2/2: adding queue-master's readinessProbe..."
kubectl patch deployment queue-master -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "queue-master",
            "readinessProbe": {
              "httpGet": {"path": "/health", "port": 80, "scheme": "HTTP"},
              "initialDelaySeconds": 10,
              "periodSeconds": 5,
              "timeoutSeconds": 1,
              "successThreshold": 1,
              "failureThreshold": 12
            }
          }
        ]
      }
    }
  }
}'
kubectl rollout status deployment/queue-master -n "$NAMESPACE" --timeout=120s

echo
echo "Done. Confirm with:"
echo "  kubectl get deployment catalogue queue-master -n $NAMESPACE -o jsonpath='{.items[*].spec.template.spec.containers[0].readinessProbe}'"
