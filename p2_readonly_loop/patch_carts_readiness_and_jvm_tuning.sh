#!/usr/bin/env bash
# Two real, separate fixes bundled onto the same deployment, both found
# during the 2026-08-15 live storefront-testing session:
#
# 1. carts had NO readinessProbe at all. A freshly-restarted pod was
#    marked "Ready" instantly, well before its real ~4-7 minute Spring
#    Boot cold-start (Tomcat + MongoDB driver + REST controller mapping)
#    actually finished -- real add-to-cart requests silently failed/hung
#    against a technically-Ready-but-not-serving pod for that whole
#    window. Fixed with a real httpGet /health readinessProbe, generous
#    initialDelaySeconds/failureThreshold to cover the real cold-start
#    variance observed (as fast as ~8s, as slow as ~2.5min on a
#    single-node cluster under CPU contention).
#
# 2. JVM env-var tuning tried the same session (real but small ~12%
#    improvement to context-load time only, confirmed via Kimi reviews
#    40/41 -- the CPU-contention floor itself was accepted as the honest
#    cost of a resource-constrained single-node lab running a third-party
#    image, not chased further). -XX:+UseContainerSupport was ALSO tried
#    and crashed the JVM outright (too old, flag unsupported on this
#    image) -- deliberately NOT included here.
#
# Reconstructed 2026-08-23 from the live cluster's real current spec
# (deployment-readiness effort) -- not tracked in any committed manifest.
# Rerun after any full redeploy.
#
# 3. Real memory-limit bump, added 2026-08-23 during the wardence-prod
#    (Oracle A1) deployment: the original 500Mi limit (tuned against the
#    tight WSL2/laptop dev box) OOMKilled carts repeatedly on the arm64
#    host even with -Xmx128m already capping the heap -- non-heap JVM
#    overhead (metaspace, thread stacks, JIT code cache) pushed real
#    usage past 500Mi. The Oracle host has real, confirmed headroom
#    (9.2GB free at the time), so the limit is raised to 1Gi rather than
#    chasing tighter JVM flags on a box that doesn't need the squeeze --
#    same "don't assume timing/resource numbers transfer to new
#    hardware" discipline as everything else in deploy/README.md.
#
# Usage: bash patch_carts_readiness_and_jvm_tuning.sh

set -euo pipefail

NAMESPACE="sock-shop"
DEPLOYMENT="carts"

echo "Patching $DEPLOYMENT: readinessProbe/livenessProbe + JVM cold-start tuning..."
kubectl patch deployment "$DEPLOYMENT" -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "carts",
            "env": [
              {"name": "JAVA_TOOL_OPTIONS", "value": "-Djava.security.egd=file:/dev/./urandom -Dspring.jmx.enabled=false"},
              {"name": "SPRING_MAIN_LAZY_INITIALIZATION", "value": "true"}
            ],
            "resources": {
              "limits": {"cpu": "300m", "memory": "1Gi"},
              "requests": {"cpu": "100m", "memory": "200Mi"}
            },
            "readinessProbe": {
              "httpGet": {"path": "/health", "port": 80, "scheme": "HTTP"},
              "initialDelaySeconds": 60,
              "periodSeconds": 10,
              "timeoutSeconds": 1,
              "successThreshold": 1,
              "failureThreshold": 30
            },
            "livenessProbe": {
              "httpGet": {"path": "/health", "port": 80, "scheme": "HTTP"},
              "initialDelaySeconds": 60,
              "periodSeconds": 10,
              "timeoutSeconds": 1,
              "successThreshold": 1,
              "failureThreshold": 60
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
echo "  kubectl get deployment carts -n $NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].readinessProbe}'"
