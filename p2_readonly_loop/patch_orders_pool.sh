#!/bin/bash
# patch_orders_pool.sh -- standing per-environment patch (committed, like the
# other patch_*.sh). Sibling: patch_orders_jvm_tuning.sh also edits orders'
# JAVA_OPTS -- this script only strips/re-appends server.tomcat.* tokens and
# preserves everything else, so the two compose.
#
# Shrinks the `orders` service's embedded-Tomcat worker pool (default 200) so
# that when `shipping` does a long GC stop-the-world pause, the `orders` threads
# blocked in Future.get(shipping, 5s) saturate the pool -- new checkouts then
# QUEUE instead of sailing straight through, which is what moves real checkout
# p50/p95 (not just p99/max). `orders` is in the real checkout path
# (front-end -> orders -> shipping); the memory-leak load burst hits it via
# front-end.
#
# Standing per-environment change, same category as patch_catalogue_upr_visibility.sh
# -- NOT part of the fault mechanism. But `orders` is also the target of
# network-latency / network-partition: after applying, RE-CHECK those two still
# diagnose correctly (their active connectivity probe is unaffected, but a
# saturated pool during a partition could shift the picture -- verify, don't assume).
#
# Idempotent. Only touches orders' JAVA_OPTS.
#
#   Apply:   bash p2_readonly_loop/patch_orders_pool.sh [pool_size]      # default 24
#   Revert:  bash p2_readonly_loop/patch_orders_pool.sh revert
#     (revert restores the exact JAVA_OPTS captured in the annotation below;
#      if that annotation is missing, it just strips the server.tomcat.* tokens)

set -uo pipefail
NS="sock-shop"
DEP="orders"
POOL="${1:-24}"
ANNOT="wardence.io/orders-pool-orig-java-opts"

echo "=== patch_orders_pool.sh (pool=${POOL}) ==="

CUR=$(kubectl get deployment "$DEP" -n "$NS" \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="JAVA_OPTS")]}{.value}{end}')
[[ -n "$CUR" ]] || { echo "FAILED: could not read orders' JAVA_OPTS." >&2; exit 1; }
echo "  current JAVA_OPTS: $CUR"

# strip any server.tomcat.threads / max-threads / min-spare tokens we may have added before
STRIPPED=""
for tok in $CUR; do
  case "$tok" in
    -Dserver.tomcat.threads.*|-Dserver.tomcat.max-threads=*|-Dserver.tomcat.min-spare-threads=*) ;;
    *) STRIPPED="${STRIPPED:+$STRIPPED }$tok" ;;
  esac
done

if [[ "$POOL" == "revert" ]]; then
  ORIG=$(kubectl get deployment "$DEP" -n "$NS" -o jsonpath="{.metadata.annotations['${ANNOT}']}")
  TARGET="${ORIG:-$STRIPPED}"
  echo "  reverting orders JAVA_OPTS to: $TARGET"
  kubectl set env deployment/"$DEP" -n "$NS" JAVA_OPTS="$TARGET"
  kubectl annotate deployment/"$DEP" -n "$NS" "${ANNOT}-" >/dev/null 2>&1 || true
  kubectl -n "$NS" rollout status deployment/"$DEP" --timeout=300s
  echo "  reverted."
  exit 0
fi

[[ "$POOL" =~ ^[0-9]+$ ]] || { echo "FAILED: pool_size must be an integer or 'revert', got '$POOL'." >&2; exit 1; }

# stash the pre-patch value ONCE (don't overwrite an existing annotation with an already-patched value)
if ! kubectl get deployment "$DEP" -n "$NS" -o jsonpath="{.metadata.annotations['${ANNOT}']}" | grep -q .; then
  kubectl annotate deployment/"$DEP" -n "$NS" "${ANNOT}=${CUR}" --overwrite >/dev/null
fi

NEW_OPTS="${STRIPPED} -Dserver.tomcat.threads.max=${POOL} -Dserver.tomcat.max-threads=${POOL} -Dserver.tomcat.threads.min-spare=2 -Dserver.tomcat.min-spare-threads=2"

if [[ "$CUR" == "$NEW_OPTS" ]]; then
  echo "  already at pool=${POOL} -- no change."
  exit 0
fi

echo "  new JAVA_OPTS: $NEW_OPTS"
kubectl set env deployment/"$DEP" -n "$NS" JAVA_OPTS="$NEW_OPTS"
kubectl -n "$NS" rollout status deployment/"$DEP" --timeout=300s || { echo "FAILED: rollout did not complete in 300s." >&2; exit 1; }

POD=$(kubectl get pod -n "$NS" -l name="$DEP" --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')
echo ""
echo "  new pod: $POD"
echo "  --- baseline health (no fault): 45s, expect no restarts, normal /health ---"
R0=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')
for i in $(seq 1 9); do
  sleep 5
  RN=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
  RDY=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  echo "    t+$((i*5))s restarts=${RN:-?} ready=${RDY:-?}"
  [[ -n "$RN" && "$RN" != "$R0" ]] && { echo "    >>> orders RESTARTED at pool=${POOL} under baseline load -- raise pool_size and re-run."; exit 1; }
done
echo ""
echo "  OK -- orders on pool=${POOL}, healthy at baseline."
echo "  RE-CHECK network-latency + network-partition still diagnose correctly before trusting this."
