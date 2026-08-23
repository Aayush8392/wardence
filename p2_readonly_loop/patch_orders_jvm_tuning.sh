#!/usr/bin/env bash
# Real gap found 2026-08-23 during the wardence-prod (Oracle A1) arm64
# deployment: unlike carts, `orders` never had a dedicated JVM-tuning
# patch at all -- its original 500Mi memory limit with no -Xmx cap
# OOMKilled it repeatedly on arm64 (old Java 8's default heap sizing
# doesn't reliably respect cgroup limits). Fixed with the same real
# JAVA_OPTS heap cap already proven on carts, plus the same real
# memory-limit bump to 1Gi -- the Oracle host has real, confirmed
# headroom (9.2GB free at the time), so the limit is raised rather than
# chasing tighter JVM flags on a box that doesn't need the squeeze.
#
# Usage: bash patch_orders_jvm_tuning.sh

set -euo pipefail

NAMESPACE="sock-shop"
DEPLOYMENT="orders"

echo "Patching $DEPLOYMENT: JVM heap cap + memory-limit bump..."
kubectl patch deployment "$DEPLOYMENT" -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "orders",
            "env": [
              {"name": "JAVA_OPTS", "value": "-Xms64m -Xmx256m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom"}
            ],
            "resources": {
              "limits": {"cpu": "500m", "memory": "1Gi"},
              "requests": {"cpu": "100m", "memory": "300Mi"}
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
echo "  kubectl get deployment orders -n $NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].env}{\"\n\"}{.spec.template.spec.containers[0].resources}'"
