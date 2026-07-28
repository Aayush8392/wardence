#!/usr/bin/env bash
# Enables the dormant Spring Cloud Sleuth Zipkin client already present
# in Sock Shop's Java services (2026-07-28).
#
# First attempt (setting a bare `ZIPKIN` env var, matching a leftover
# config found on `shipping`) was WRONG -- confirmed via direct evidence,
# not assumed: `kubectl exec ... ps -ef` on a real running carts pod
# showed spring.zipkin.enabled=true genuinely made it into the java
# command line (proving JAVA_OPTS itself works), but NO
# -Dspring.zipkin.baseUrl flag anywhere, even though the ZIPKIN env var
# was confirmed present via `kubectl exec ... env`. Root cause: the
# container's startup wrapper (/usr/local/bin/java.sh) just passes
# $JAVA_OPTS straight through to `java` -- it never reads $ZIPKIN at all.
# Separately, `ZIPKIN` also isn't a name Spring Boot's own env-var-to-
# property auto-binding would ever recognize (it needs the fully
# qualified SPRING_ZIPKIN_BASEURL, not a short custom name) -- so this
# was never going to work even without the wrapper-script issue.
# shipping's original leftover ZIPKIN config was equally dead, not a
# working integration we broke.
#
# Real fix: put the URL directly into JAVA_OPTS as its own -D flag, the
# exact mechanism already proven to work for spring.zipkin.enabled.
#
# Real Jaeger service exposes 9411/TCP (Zipkin-compatible ingest),
# confirmed via `kubectl get svc`.
#
# Usage: bash patch_enable_zipkin_tracing.sh

set -euo pipefail

NAMESPACE="sock-shop"
ZIPKIN_BASE_URL="http://jaeger.monitoring.svc.cluster.local:9411"

patch_service() {
  local deployment="$1"
  local java_opts="$2"

  echo "Patching $deployment..."
  kubectl patch deployment "$deployment" -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "'"$deployment"'",
            "env": [
              {"name": "JAVA_OPTS", "value": "'"$java_opts"'"}
            ]
          }
        ]
      }
    }
  }
}'
}

JAVA_OPTS_ENABLED="-Xms64m -Xmx128m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=true -Dspring.zipkin.baseUrl=${ZIPKIN_BASE_URL}"

patch_service "carts" "$JAVA_OPTS_ENABLED"
patch_service "orders" "$JAVA_OPTS_ENABLED"
patch_service "queue-master" "$JAVA_OPTS_ENABLED"
patch_service "shipping" "$JAVA_OPTS_ENABLED"

echo
echo "Waiting for all 4 rollouts..."
for d in carts orders queue-master shipping; do
  kubectl rollout status deployment/"$d" -n "$NAMESPACE" --timeout=120s
done

echo
echo "Done. Give it a minute or two for traffic_gen to generate real requests"
echo "through these services, then check whether Jaeger actually received"
echo "anything real:"
echo
echo "  kubectl port-forward -n monitoring svc/jaeger 16686:16686 &"
echo "  curl -s http://localhost:16686/api/services | python3 -m json.tool"
