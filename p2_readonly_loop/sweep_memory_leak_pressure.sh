#!/bin/bash
# sweep_memory_leak_pressure.sh -- MEASUREMENT ONLY. Not part of the fault mechanism.
#
# Answers one question with DATA instead of prediction: how does shipping's
# stop-the-world duty cycle vary with the GRAPH mechanism's live-set size?
#
# Why this exists (2026-09-02): the memory-leak felt-effect arc was being tuned
# one ~15-minute prod rollout at a time, with the gaps between data points filled
# by prediction rather than measurement -- which is why several "this should
# work" changes each moved the numbers sideways. The live-set size (`static=`)
# is sent over the agent's control file and needs NO JVM restart, so the entire
# curve is measurable in ~2 minutes per point.
#
# METHOD: for each static size -- ramp the agent, then take two /agent-ctl/status
# samples SAMPLE_S apart and compute (delta stw_pause_ms / delta gc_sampled_at_ms).
# Those two fields are a monotonic STW counter stamped with the JVM's OWN clock
# (LeakAgent.java's review-57 additions, added for exactly this purpose), so the
# result is immune to bash-loop delay and to a status write that stalls on disk.
# gc.log is also diffed over the same window for the pause-size distribution,
# because duty cycle alone doesn't say whether pauses are felt: 20 x 100ms and
# 2 x 1000ms are the same duty cycle and only one of them is visible to a user.
#
# TOUCHES NOTHING ELSE: no injector, no scorer, no episode, no DB row, no ground
# truth label, no trust-state change. Writes only /agent-ctl/cmd, byte-identical
# to how injector.py does it, and always RELEASEs afterwards (including Ctrl-C).
#
# REQUIRES: no live episode in flight. Refuses to start otherwise.
#
#   bash p2_readonly_loop/sweep_memory_leak_pressure.sh
#   bash p2_readonly_loop/sweep_memory_leak_pressure.sh "150 250 350 450"

set -uo pipefail
NS="sock-shop"
DEP="shipping"
CTR="shipping"

# Live-set sizes (MiB) to sweep. Default spans well below and well above the
# current 200, so the curve's SHAPE is visible rather than one point on it.
STATICS="${1:-120 200 280 360 440}"

SLOTS_K=200      # must match injector.py's MEMORY_LEAK_GRAPH_SLOTS_K
WRITES_K=100     # must match MEMORY_LEAK_GRAPH_WRITES_K
EDGES=85         # must match MEMORY_LEAK_GRAPH_EDGES
RAMP_MAX_S=150   # cap on waiting for the companion to reach target
SAMPLE_S=45      # measurement window per point
DRAIN_S=45       # post-RELEASE settle before the next point

exec_sh() { kubectl exec -n "$NS" deploy/"$DEP" -c "$CTR" -- sh -c "$1" 2>/dev/null; }
send_cmd() {
  exec_sh "printf '%s\n' '$1' > /agent-ctl/cmd.tmp && mv /agent-ctl/cmd.tmp /agent-ctl/cmd"
}
status() { exec_sh 'cat /agent-ctl/status'; }
field()  { printf '%s\n' "$1" | grep "^$2=" | head -1 | cut -d= -f2-; }
gclines() { exec_sh 'wc -l < /tmp/gc.log' | tr -d ' \r'; }

cleanup() {
  echo ""
  echo "  releasing agent..."
  send_cmd "RELEASE" >/dev/null 2>&1
  sleep 3
  local s; s=$(status)
  echo "  final: state=$(field "$s" state) allocated_mb=$(field "$s" allocated_mb) graph_slots=$(field "$s" graph_slots)"
}
trap 'cleanup; exit 130' INT TERM

echo "=== sweep_memory_leak_pressure.sh ==="
S0=$(status)
if [[ -z "$S0" ]]; then echo "FAILED: cannot read /agent-ctl/status -- is the leak agent loaded?" >&2; exit 1; fi
ST=$(field "$S0" state)
if [[ "$ST" != "READY" && "$ST" != "IDLE" ]]; then
  echo "FAILED: agent state is '$ST', not READY/IDLE -- an episode or a prior run is still active." >&2
  echo "        Wait for it to finish (or send RELEASE) before sweeping." >&2
  exit 1
fi
R0=$(kubectl get pod -n "$NS" -l name="$DEP" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
echo "  heap_max_mib=$(field "$S0" heap_max_mib)  governor_ceiling=$(field "$S0" governor_abs_ceiling_mib)  reqsync=$(field "$S0" reqsync_enabled)"
echo "  baseline post_gc_heap_mib=$(field "$S0" post_gc_heap_mib)  restartCount=$R0"
echo "  graph: slots=${SLOTS_K}k writes=${WRITES_K}k/s edges=${EDGES}"
echo "  sweeping static= : $STATICS   (~$(( (RAMP_MAX_S/3 + SAMPLE_S + DRAIN_S) ))s+ per point)"
echo ""
printf '%-7s %-9s %-8s %-9s %-7s %-7s %-7s %-7s %-6s\n' \
  "static" "postGC" "headrm" "DUTY%" "pauses" ">1s" ">2s" "maxMs" "govRel"
echo "  ----------------------------------------------------------------------------"

for S in $STATICS; do
  send_cmd "GRAPH $SLOTS_K $WRITES_K $EDGES static=$S ttl=900" >/dev/null

  # Ramp: wait until the companion is near target AND the backbone is built.
  ramped=0
  for ((i=0; i<RAMP_MAX_S; i+=3)); do
    sleep 3
    st=$(status)
    am=$(field "$st" allocated_mb); gs=$(field "$st" graph_slots)
    [[ -z "$am" ]] && am=0; [[ -z "$gs" ]] && gs=0
    if (( am * 100 >= S * 85 )) && (( gs > 0 )); then ramped=1; break; fi
  done
  if (( ramped == 0 )); then
    printf '%-7s %s\n' "$S" "RAMP TIMEOUT (allocated_mb=$am graph_slots=$gs) -- skipping"
    send_cmd "RELEASE" >/dev/null; sleep "$DRAIN_S"; continue
  fi

  # Measurement window.
  GL0=$(gclines)
  A=$(status); sleep "$SAMPLE_S"; B=$(status)
  GL1=$(gclines)

  stw0=$(field "$A" stw_pause_ms); ts0=$(field "$A" gc_sampled_at_ms)
  stw1=$(field "$B" stw_pause_ms); ts1=$(field "$B" gc_sampled_at_ms)
  postgc=$(field "$B" post_gc_heap_mib); hmax=$(field "$B" heap_max_mib)
  govrel=$(field "$B" governor_release_events)

  dstw=$(( stw1 - stw0 )); dts=$(( ts1 - ts0 ))
  duty="n/a"; (( dts > 0 )) && duty=$(awk "BEGIN{printf \"%.1f\", $dstw*100/$dts}")
  headroom=$(( hmax - postgc ))

  # Pause distribution over the same window, straight from gc.log.
  n=$(( GL1 - GL0 )); (( n < 1 )) && n=1
  paus=$(exec_sh "tail -n $n /tmp/gc.log | grep -o 'threads were stopped: [0-9.]*' | awk '{print \$4}'")
  cnt=$(printf '%s\n' "$paus" | grep -c '[0-9]')
  o1=$(printf '%s\n' "$paus" | awk '$1>1.0' | grep -c '[0-9]')
  o2=$(printf '%s\n' "$paus" | awk '$1>2.0' | grep -c '[0-9]')
  mx=$(printf '%s\n' "$paus" | sort -g | tail -1 | awk '{printf "%d", $1*1000}')

  printf '%-7s %-9s %-8s %-9s %-7s %-7s %-7s %-7s %-6s\n' \
    "$S" "${postgc}Mi" "${headroom}Mi" "$duty" "$cnt" "$o1" "$o2" "${mx:-0}" "$govrel"

  RN=$(kubectl get pod -n "$NS" -l name="$DEP" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null)
  if [[ -n "$RN" && "$RN" != "$R0" ]]; then
    echo "  >>> shipping RESTARTED at static=$S (OOM?) -- stopping sweep here. This size is the ceiling."
    cleanup; exit 1
  fi

  send_cmd "RELEASE" >/dev/null
  sleep "$DRAIN_S"
done

cleanup
echo ""
echo "  Read the table: DUTY% is the fraction of wall-clock shipping is frozen."
echo "  '>1s' is what a user can actually feel -- a high DUTY% built from many"
echo "  sub-second pauses is invisible, so pick on BOTH columns, not duty alone."
