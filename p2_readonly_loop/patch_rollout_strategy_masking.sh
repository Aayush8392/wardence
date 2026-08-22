#!/usr/bin/env bash
# Real fix for bad-rollout's and init-failure's demo-visibility "masking"
# problem (session 2026-08-1x): by default a RollingUpdate keeps the OLD,
# healthy pod serving 100% of traffic until the NEW (broken) pod passes
# readiness -- which for these two fault classes means it never does, so
# the storefront never visibly degrades even though the fault is real and
# correctly diagnosed. Real fix: force the old pod down BEFORE the new one
# is attempted (maxSurge: 0%, maxUnavailable: 100%), so the real outage
# window becomes visible on the storefront during the fault, same
# real-mechanism-not-fabricated-symptom discipline as every other class's
# demo-visibility fix in this project.
#
# Applied to front-end (bad-rollout's target) and payment (init-failure's
# target). Confirmed NOT to help cpu-throttling (a different problem --
# who serves traffic during a rollout, not how long a fixed
# initialDelaySeconds floor takes) -- do not apply this pattern there.
#
# Reconstructed 2026-08-23 from the live cluster's real current spec
# (deployment-readiness effort) -- not tracked in any committed manifest.
# Rerun after any full redeploy.
#
# Usage: bash patch_rollout_strategy_masking.sh

set -euo pipefail

NAMESPACE="sock-shop"

for d in front-end payment; do
  echo "Patching $d's rollout strategy (maxSurge: 0%, maxUnavailable: 100%)..."
  kubectl patch deployment "$d" -n "$NAMESPACE" --type=strategic -p '
{
  "spec": {
    "strategy": {
      "type": "RollingUpdate",
      "rollingUpdate": {
        "maxSurge": "0%",
        "maxUnavailable": "100%"
      }
    }
  }
}'
done

echo
echo "Done (no rollout triggered -- this only changes strategy for the NEXT"
echo "rollout, it doesn't restart anything now). Confirm with:"
echo "  kubectl get deployment front-end payment -n $NAMESPACE -o jsonpath='{.items[*].spec.strategy}'"
