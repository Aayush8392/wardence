#!/usr/bin/env bash
# Real fix (2026-07-21): session-db (Redis) ships with NO writable volume
# for /data despite readOnlyRootFilesystem: true. Every periodic RDB
# background save was failing silently, and Redis's default
# stop-writes-on-bgsave-error=yes meant this blocked ALL session writes
# cluster-wide -- every real POST /orders crashed front-end outright
# (front-end's own orders/index.js:67 does an unhandled read on
# req.session.customerId). Disabling periodic RDB snapshotting entirely
# removes the write-lock trigger; correct for this disposable,
# no-persistence-needed lab (no other security posture changed).
#
# Reconstructed 2026-08-23 from the live cluster's real current spec
# (deployment-readiness effort) -- not tracked in any committed manifest,
# since Sock Shop was deployed from the upstream official manifest URL.
# Rerun after any full redeploy, same pattern as this project's other
# patch_*.sh scripts.
#
# Usage: bash patch_session_db_disable_rdb.sh

set -euo pipefail

NAMESPACE="sock-shop"
DEPLOYMENT="session-db"

echo "Patching $DEPLOYMENT to disable RDB persistence..."
kubectl patch deployment "$DEPLOYMENT" -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "session-db",
            "args": ["redis-server", "--save", ""]
          }
        ]
      }
    }
  }
}'

echo "Waiting for rollout..."
kubectl rollout status deployment/"$DEPLOYMENT" -n "$NAMESPACE" --timeout=120s

echo
echo "Done. Confirm with:"
echo "  kubectl get deployment session-db -n $NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].args}'"
echo "Expect: [\"redis-server\",\"--save\",\"\"]"
