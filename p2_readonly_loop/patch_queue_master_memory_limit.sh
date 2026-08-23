#!/usr/bin/env bash
# Real fix, found 2026-08-2x during disk-full's arm64 re-validation on
# wardence-prod: queue-master turned out to be a Java Spring Boot
# service (Tomcat, RabbitMQ config beans, ~22s Spring context init --
# NOT the "lightweight Go service" an earlier session's readinessProbe
# fix incorrectly assumed), running with JAVA_OPTS -Xms64m -Xmx128m
# against the original WSL2-era 500Mi container memory limit. Same
# root cause already fixed for carts/orders on this hardware: non-heap
# JVM overhead (metaspace, JIT, thread stacks, especially during
# startup class-loading) runs higher on arm64 and pushed real usage
# past 500Mi, causing a genuine, repeated kernel OOM-kill crash loop --
# confirmed live via kubectl describe (exitCode 137, reason=OOMKilled,
# readinessProbe "connection refused" x23 since the container never
# got far enough to open port 80 before being killed again).
#
# disk-full's own exec-based write mechanism was what first stressed
# queue-master hard enough to expose this -- not a Wardence code bug,
# a pre-existing infra gap the arm64 migration's original service
# audit missed for this one service.
#
# Usage: bash patch_queue_master_memory_limit.sh

set -euo pipefail

NAMESPACE="sock-shop"
DEPLOYMENT="queue-master"

echo "Patching $DEPLOYMENT: memory limit 500Mi -> 1Gi (arm64 JVM overhead fix, same as carts/orders)..."
kubectl patch deployment "$DEPLOYMENT" -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "queue-master",
            "resources": {
              "limits": {"memory": "1Gi"}
            }
          }
        ]
      }
    }
  }
}'

echo "Waiting for rollout..."
kubectl rollout status deployment/"$DEPLOYMENT" -n "$NAMESPACE" --timeout=300s

echo
echo "Done. Confirm with:"
echo "  kubectl get deployment queue-master -n $NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
echo "  kubectl get pods -n $NAMESPACE -l name=queue-master"
