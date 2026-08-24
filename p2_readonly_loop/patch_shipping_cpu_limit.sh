#!/bin/bash
# shipping's CPU limit -- real, load-bearing value, not the WSL2-era default.
# History: raised 300m -> 1000m (memory-leak session) because LeakAgent's reqsync
# thread (20ms MBean poll, runs the pod's ENTIRE lifetime, not just during episodes)
# was starving shipping's own /health at 300m.
# Lowered 1000m -> 500m (UPR re-validation session) after confirming shipping's own
# /health/metrics stay clean at 500m (0.07-0.11s, 200) AND the real memory-leak
# episode mechanism still works cleanly at 500m (governor/reqsync/ramp all confirmed
# live) -- 500m was found necessary because reqsync's permanent ~950m@1000m-limit
# CPU footprint was starving the WHOLE 2-vCPU Oracle node, not just shipping itself,
# which was silently corrupting under-provisioned-replicas' own diagnosis signal
# (catalogue's real p95 became dominated by node-wide contention, not replica count).
set -euo pipefail
kubectl patch deployment shipping -n sock-shop --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"500m"}]'
kubectl rollout status deployment/shipping -n sock-shop
echo "shipping CPU limit set to 500m."
