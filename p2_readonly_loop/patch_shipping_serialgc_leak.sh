#!/bin/bash
# patch_shipping_serialgc_leak.sh -- standing per-environment patch (committed,
# like the other patch_*.sh). Sibling: patch_shipping_cpu_limit.sh touches this
# same deployment's resources (not JAVA_OPTS) -- no conflict.
#
# Switches the production `shipping` deployment from stock G1GC to the tuned
# SerialGC config the memory-leak GRAPH mechanism was validated against
# (check_memory_leak_churn.sh, 2026-09-02). G1's short concurrent pauses do not
# produce the long stop-the-world stalls the felt-effect demo needs; SerialGC's
# single-threaded full-heap STW does. This is a standing per-environment change
# (like patch_catalogue_upr_visibility.sh's 75m catalogue CPU limit), NOT part
# of the fault mechanism -- injector.py still sends the same class of command
# and diagnosis still keys on post-GC heap elevated vs baseline, so historical
# episodes stay valid (methodology-continuity note in wardence_worklog.md).
#
# Idempotent. Only touches shipping's JAVA_OPTS env var (via `kubectl set env`,
# so the deployment's rollout strategy -- minReadySeconds=400 etc from
# install_shipping_leak_agent.py -- is preserved); the -javaagent path and the
# agent volume mounts are already on the deployment and are preserved too.
#
# PREREQUISITE: run `python3 p2_readonly_loop/install_shipping_leak_agent.py`
# FIRST if prod's agent jar predates the GRAPH command (2026-09-01). The
# installer rebuilds the jar from wardence/LeakAgent.java. This script's
# post-patch check greps the agent status for `graph_slots=` -- if that field
# is absent, the jar is stale and GRAPH will be silently ignored at trigger
# time (ramp never confirms -> _inject_and_verify_memory_leak ABORTs). Run the
# installer BEFORE this patch (installer resets JAVA_OPTS to +javaagent only;
# running it after would undo the SerialGC change).
#
#   Apply:   bash p2_readonly_loop/patch_shipping_serialgc_leak.sh
#   Revert (one line):
#     kubectl set env deployment/shipping -n sock-shop JAVA_OPTS='-Xms64m -Xmx192m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=false -javaagent:/agent/leak-agent.jar' && kubectl -n sock-shop rollout status deployment/shipping
#
# reqsync: left OFF here (-Dwardence.leak.reqsyncEnabled=false), matching the
# buildlog's production-wiring spec -- GRAPH is the mechanism now, not reqsync.
# To re-enable, drop that one token from NEW_OPTS below and re-run.

set -uo pipefail
NS="sock-shop"
DEP="shipping"

echo "=== patch_shipping_serialgc_leak.sh ==="

CUR=$(kubectl get deployment "$DEP" -n "$NS" \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="JAVA_OPTS")]}{.value}{end}')
if [[ -z "$CUR" ]]; then
  echo "FAILED: could not read shipping's current JAVA_OPTS." >&2
  exit 1
fi
echo "  current JAVA_OPTS: $CUR"

# Preserve exactly the agent + egd + zipkin tokens already present; strip
# everything we're going to set ourselves (heap / GC / new-size / gc-logging /
# leak-props / OOM-exit), then append the tuned set.
KEPT=""
for tok in $CUR; do
  case "$tok" in
    -Xm*|-XX:+Use*GC|-XX:+UseParallelOldGC|-XX:NewSize=*|-XX:MaxNewSize=*|-XX:NewRatio=*|-Xmn*) ;;
    -Xloggc:*|-verbose:gc|-XX:+PrintGC*|-XX:+ExitOnOutOfMemoryError) ;;
    -Dwardence.leak.*) ;;
    *) KEPT="${KEPT:+$KEPT }$tok" ;;
  esac
done
echo "  preserved tokens: $KEPT"

NEW_OPTS="-Xms256m -Xmx256m -XX:+UseSerialGC -XX:NewSize=48m -XX:MaxNewSize=48m ${KEPT} -Xloggc:/tmp/gc.log -verbose:gc -XX:+PrintGCDetails -XX:+PrintGCDateStamps -XX:+PrintGCApplicationStoppedTime -XX:+ExitOnOutOfMemoryError -Dwardence.leak.governorMode=passive -Dwardence.leak.governorCeilingMib=246 -Dwardence.leak.reqsyncEnabled=false"

if [[ "$CUR" == "$NEW_OPTS" ]]; then
  echo "  already patched -- no change."
  exit 0
fi

echo ""
echo "  new JAVA_OPTS: $NEW_OPTS"
echo ""
kubectl set env deployment/"$DEP" -n "$NS" JAVA_OPTS="$NEW_OPTS"
# shipping's install-time strategy has minReadySeconds=400 (real boot ~285-335s)
# -- give the rollout well over that before calling it stuck.
echo "  waiting for rollout (shipping boots slowly + minReadySeconds=400, up to ~8min)..."
kubectl -n "$NS" rollout status deployment/"$DEP" --timeout=540s || { echo "FAILED: rollout did not complete in 540s." >&2; exit 1; }

POD=$(kubectl get pod -n "$NS" -l name="$DEP" --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')
echo ""
echo "  new pod: $POD"
echo "  --- agent load + reqsync MBean check (wait ~15s for Tomcat to register) ---"
sleep 15
kubectl logs -n "$NS" "$POD" -c "$DEP" --tail=200 | grep -E 'wardence-leak-agent\].*loaded|SerialGC|reqsync' | sed 's/^/    /'
kubectl exec -n "$NS" "$POD" -c "$DEP" -- sh -c 'cat /agent-ctl/status 2>/dev/null' \
  | grep -E '^(state|reqsync_enabled|sync_mbean_unavailable|last_error|graph_slots)=' | sed 's/^/    /'

echo ""
echo "  --- BASELINE HEALTH CHECK (no fault): watch shipping for 60s, expect NO restarts, ---"
echo "  --- normal /health latency. If it flaps on SerialGC under baseline load, REVERT. ---"
R0=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')
for i in $(seq 1 12); do
  sleep 5
  RN=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
  RDY=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  echo "    t+$((i*5))s restarts=${RN:-?} ready=${RDY:-?}"
  [[ -n "$RN" && "$RN" != "$R0" ]] && { echo "    >>> RESTARTED on SerialGC under baseline load -- REVERT (command in this script's header)."; exit 1; }
done
echo ""
echo "  OK -- shipping is on SerialGC and healthy at baseline. Next: patch_orders_pool.sh (if needed) + trigger memory-leak."
