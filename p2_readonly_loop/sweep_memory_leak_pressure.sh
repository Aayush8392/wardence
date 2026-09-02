#!/bin/bash
# sweep_memory_leak_pressure.sh -- MEASUREMENT + MANUAL-TEST HARNESS.
# Not part of the fault mechanism. No injector, no scorer, no episode, no DB
# row, no ground-truth label, no trust-state change. Writes only /agent-ctl/cmd,
# byte-identical to how injector.py does it, and always RELEASEs on exit.
#
# WHY (2026-09-02): the memory-leak felt-effect arc was tuned one ~15-minute
# prod rollout at a time with the gaps filled by prediction, which is why
# several changes each moved the numbers sideways. The live-set size and graph
# shape are sent over the agent control file and need NO JVM restart, so the
# whole parameter space is measurable in ~2 min/point.
#
# The governing relation (confirmed by the first static sweep):
#     STW duty cycle  ~  live_set / (heap_size - live_set)
# HEADROOM is the lever. Growing -Xmx to lengthen pauses also makes them rarer,
# more than proportionally -- 1.0s every 2.8s (36%) at -Xmx256/live183 vs
# 2.0s every 9.7s (21%) at -Xmx640/live~300.
#
# MODES
#   static <list>   sweep static-companion MiB at fixed edges  (headroom lever)
#   edges  <list>   sweep refs-per-node at fixed static        (mark-cost lever)
#   hold <static> <edges> <seconds>
#                   arm the SAME leak a real episode arms and hold it, printing
#                   a rolling duty cycle, so the felt effect can be tested by
#                   hand (probe loop / real storefront clicks) with NO episode.
#
#   bash p2_readonly_loop/sweep_memory_leak_pressure.sh static "120 200 280 360 440"
#   bash p2_readonly_loop/sweep_memory_leak_pressure.sh edges  "40 85 130 175"
#   bash p2_readonly_loop/sweep_memory_leak_pressure.sh hold 360 85 600
#
# Duty cycle is measured as (delta stw_pause_ms / delta gc_sampled_at_ms) -- a
# monotonic STW counter stamped with the JVM own clock (LeakAgent.java review-57
# fields, added for exactly this), immune to bash-loop delay. gc.log is diffed
# over the same window for the pause-size distribution, because duty alone is
# not the target: 20x100ms and 2x1000ms are identical duty and only one of them
# is visible to a user.
#
# Companion probe, run in a SECOND terminal during `hold`:
#   bash p2_readonly_loop/probe_shipping_latency.sh 30

set -uo pipefail
NS="sock-shop"; DEP="shipping"; CTR="shipping"

MODE="${1:-static}"
SLOTS_K=200
WRITES_K=100
EDGES_FIXED="${EDGES_FIXED:-85}"
STATIC_FIXED="${STATIC_FIXED:-200}"
RAMP_MAX_S=150; SAMPLE_S=45; DRAIN_S=45

exec_sh() { kubectl exec -n "$NS" deploy/"$DEP" -c "$CTR" -- sh -c "$1" 2>/dev/null; }
send_cmd() { exec_sh "printf '%s\n' '$1' > /agent-ctl/cmd.tmp && mv /agent-ctl/cmd.tmp /agent-ctl/cmd"; }
status() { exec_sh 'cat /agent-ctl/status'; }
field()  { printf '%s\n' "$1" | grep "^$2=" | head -1 | cut -d= -f2-; }
gclines(){ exec_sh 'wc -l < /tmp/gc.log' | tr -d ' \r'; }

cleanup() {
  echo ""; echo "  releasing agent..."
  send_cmd "RELEASE" >/dev/null 2>&1; sleep 3
  local s; s=$(status)
  echo "  final: state=$(field "$s" state) allocated_mb=$(field "$s" allocated_mb) graph_slots=$(field "$s" graph_slots)"
}
trap 'cleanup; exit 130' INT TERM

ramp() {
  local want="$1" am gs st
  for ((i=0; i<RAMP_MAX_S; i+=3)); do
    sleep 3; st=$(status)
    am=$(field "$st" allocated_mb); gs=$(field "$st" graph_slots)
    [[ -z "$am" ]] && am=0; [[ -z "$gs" ]] && gs=0
    if (( am * 100 >= want * 85 )) && (( gs > 0 )); then return 0; fi
  done
  echo "  RAMP TIMEOUT (allocated_mb=$am graph_slots=$gs)"; return 1
}

measure() {
  local GL0 A B GL1 stw0 ts0 stw1 ts1 postgc hmax govrel dstw dts duty n paus cnt o1 o2 mx
  GL0=$(gclines); A=$(status); sleep "$SAMPLE_S"; B=$(status); GL1=$(gclines)
  stw0=$(field "$A" stw_pause_ms); ts0=$(field "$A" gc_sampled_at_ms)
  stw1=$(field "$B" stw_pause_ms); ts1=$(field "$B" gc_sampled_at_ms)
  postgc=$(field "$B" post_gc_heap_mib); hmax=$(field "$B" heap_max_mib)
  govrel=$(field "$B" governor_release_events)
  dstw=$(( stw1 - stw0 )); dts=$(( ts1 - ts0 ))
  duty="n/a"; (( dts > 0 )) && duty=$(awk "BEGIN{printf \"%.1f\", $dstw*100/$dts}")
  n=$(( GL1 - GL0 )); (( n < 1 )) && n=1
  paus=$(exec_sh "tail -n $n /tmp/gc.log | grep -o 'threads were stopped: [0-9.]*' | awk '{print \$4}'")
  cnt=$(printf '%s\n' "$paus" | grep -c '[0-9]')
  o1=$(printf '%s\n' "$paus" | awk '$1>1.0' | grep -c '[0-9]')
  o2=$(printf '%s\n' "$paus" | awk '$1>2.0' | grep -c '[0-9]')
  mx=$(printf '%s\n' "$paus" | sort -g | tail -1 | awk '{printf "%d", $1*1000}')
  echo "$duty $cnt $o1 $o2 ${mx:-0} $postgc $(( hmax - postgc )) $govrel"
}

echo "=== sweep_memory_leak_pressure.sh (mode: $MODE) ==="
S0=$(status)
[[ -z "$S0" ]] && { echo "FAILED: cannot read /agent-ctl/status." >&2; exit 1; }
ST=$(field "$S0" state)
if [[ "$ST" != "READY" && "$ST" != "IDLE" ]]; then
  echo "FAILED: agent state '$ST' -- an episode or prior run is still active." >&2; exit 1
fi
R0=$(kubectl get pod -n "$NS" -l name="$DEP" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
echo "  heap_max=$(field "$S0" heap_max_mib)Mi  governor=$(field "$S0" governor_abs_ceiling_mib)Mi  reqsync=$(field "$S0" reqsync_enabled)  restarts=$R0"

restart_check() {
  local RN; RN=$(kubectl get pod -n "$NS" -l name="$DEP" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null)
  if [[ -n "$RN" && "$RN" != "$R0" ]]; then
    echo "  >>> shipping RESTARTED (OOM?) -- this setting is the ceiling. Stopping."; cleanup; exit 1
  fi
}

case "$MODE" in
  hold)
    HS="${2:-360}"; HE="${3:-85}"; HD="${4:-600}"
    echo "  arming static=${HS}MiB edges=${HE} for ${HD}s -- the SAME leak a real"
    echo "  episode arms. No episode/scorer/DB. Ctrl-C releases early."
    send_cmd "GRAPH $SLOTS_K $WRITES_K $HE static=$HS ttl=$(( HD + 300 ))" >/dev/null
    ramp "$HS" || { cleanup; exit 1; }
    echo ""
    echo "  ARMED. In a SECOND terminal now:"
    echo "    bash p2_readonly_loop/probe_shipping_latency.sh 30"
    echo "  ...and click through real checkouts on the storefront."
    echo ""
    printf '  %-8s %-8s %-7s %-6s %-6s %-8s %-8s %-6s\n' "elapsed" "DUTY%" "pauses" ">1s" ">2s" "maxMs" "postGC" "govRel"
    el=0
    while (( el < HD )); do
      read -r d c o1 o2 mx pg hr gr <<<"$(measure)"
      el=$(( el + SAMPLE_S ))
      printf '  %-8s %-8s %-7s %-6s %-6s %-8s %-8s %-6s\n' "${el}s" "$d" "$c" "$o1" "$o2" "$mx" "${pg}Mi" "$gr"
      restart_check
    done
    cleanup
    ;;
  static|edges)
    LIST="${2:-}"
    if [[ "$MODE" == "static" ]]; then
      LIST="${LIST:-120 200 280 360 440}"
      echo "  sweeping static= : $LIST   (edges fixed at $EDGES_FIXED)"
      COL="static"
    else
      LIST="${LIST:-40 85 130 175}"
      echo "  sweeping edges=  : $LIST   (static fixed at ${STATIC_FIXED}MiB)"
      COL="edges"
    fi
    echo "  ~$(( RAMP_MAX_S/3 + SAMPLE_S + DRAIN_S ))s+ per point"
    echo ""
    printf '%-7s %-9s %-8s %-9s %-7s %-7s %-7s %-7s %-6s\n' \
      "$COL" "postGC" "headrm" "DUTY%" "pauses" ">1s" ">2s" "maxMs" "govRel"
    echo "  ----------------------------------------------------------------------------"
    for V in $LIST; do
      if [[ "$MODE" == "static" ]]; then
        send_cmd "GRAPH $SLOTS_K $WRITES_K $EDGES_FIXED static=$V ttl=900" >/dev/null; WANT=$V
      else
        send_cmd "GRAPH $SLOTS_K $WRITES_K $V static=$STATIC_FIXED ttl=900" >/dev/null; WANT=$STATIC_FIXED
      fi
      if ! ramp "$WANT"; then
        printf '%-7s %s\n' "$V" "-- skipped"; send_cmd "RELEASE" >/dev/null; sleep "$DRAIN_S"; continue
      fi
      read -r d c o1 o2 mx pg hr gr <<<"$(measure)"
      printf '%-7s %-9s %-8s %-9s %-7s %-7s %-7s %-7s %-6s\n' \
        "$V" "${pg}Mi" "${hr}Mi" "$d" "$c" "$o1" "$o2" "$mx" "$gr"
      restart_check
      send_cmd "RELEASE" >/dev/null; sleep "$DRAIN_S"
    done
    cleanup
    echo ""
    echo "  DUTY% = fraction of wall-clock shipping is frozen. '>1s' = what a user"
    echo "  can feel. A high DUTY% built from sub-second pauses is invisible --"
    echo "  pick on BOTH columns. Felt-pause rate = ${SAMPLE_S}s / '>1s'."
    ;;
  *)
    echo "unknown mode '$MODE' -- use: static <list> | edges <list> | hold <static> <edges> <secs>" >&2
    exit 2 ;;
esac
