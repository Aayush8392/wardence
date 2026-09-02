#!/bin/bash
# probe_shipping_latency.sh -- times N direct POST /shipping calls from inside
# the front-end pod and prints a summary, not just a wall of numbers.
#
# Companion to sweep_memory_leak_pressure.sh's `hold` mode: run this in a second
# terminal while a leak is armed, to measure what a request actually experiences.
#
# MEASUREMENT ONLY -- reads nothing, writes nothing, touches no episode state.
#
# IMPORTANT (why the numbers here run HIGHER than a real checkout): each call
# opens a COLD connection, so a request landing mid-freeze eats the ENTIRE
# remaining stop-the-world pause plus connect/dispatch. A real checkout reuses a
# warm keep-alive connection from `orders`, so its bytes sit in the socket
# receive buffer during the freeze and it only eats the REMAINDER -- about half
# a pause on average. Treat this as an upper bound on the felt effect, and the
# manual storefront click as the real one.
#
#   bash p2_readonly_loop/probe_shipping_latency.sh 30
#   bash p2_readonly_loop/probe_shipping_latency.sh 30 0.5    # tighter spacing

set -uo pipefail
N="${1:-30}"
GAP="${2:-1}"
NS="sock-shop"

JS='var t=Date.now(),h=require("http"),r=h.request({host:"shipping",port:80,path:"/shipping",method:"POST",headers:{"Content-Type":"application/json","Content-Length":2}},function(x){x.resume();x.on("end",function(){console.log(Date.now()-t)})});r.on("error",function(){console.log("ERR")});r.end("{}")'

echo "=== probe_shipping_latency.sh: $N calls, ${GAP}s apart ==="
TMP=$(mktemp)
for ((i=1; i<=N; i++)); do
  ms=$(kubectl exec -n "$NS" deploy/front-end -- node -e "$JS" 2>/dev/null | tr -d '\r')
  [[ -z "$ms" ]] && ms="ERR"
  printf '%s\n' "$ms" | tee -a "$TMP"
  sleep "$GAP"
done

echo ""
awk -v n="$N" '
  /^[0-9]+$/ { v[c++]=$1; s+=$1; if ($1>mx) mx=$1;
               if ($1>800) f8++; if ($1>1500) f15++; if ($1>3000) f30++; next }
  { err++ }
  END {
    if (c==0) { print "  no successful samples"; exit }
    asort(v)
    printf "  samples=%d  errors=%d\n", c, err+0
    printf "  p50=%dms  p95=%dms  max=%dms  mean=%dms\n", v[int(c*0.5)+1], v[int(c*0.95)], mx, s/c
    printf "  >800ms : %d/%d (%.0f%%)   <- roughly the felt threshold\n", f8+0, c, (f8*100.0)/c
    printf "  >1500ms: %d/%d (%.0f%%)\n", f15+0, c, (f15*100.0)/c
    printf "  >3000ms: %d/%d (%.0f%%)   <- near orders 5s Future.get cliff\n", f30+0, c, (f30*100.0)/c
  }
' "$TMP" 2>/dev/null || {
  echo "  (gawk asort unavailable -- raw counts only)"
  echo "  >800ms : $(awk '$1>800' "$TMP" | grep -c '[0-9]') / $(grep -c '^[0-9]' "$TMP")"
  echo "  max    : $(sort -n "$TMP" | tail -1)ms"
}
rm -f "$TMP"
