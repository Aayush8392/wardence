#!/usr/bin/env bash
# One-off, read-only cluster-state capture for the deployment-readiness
# effort (2026-08-23 session) -- Point 3 of the deployment checklist:
# "bake every live kubectl patch into a real, reproducible manifest."
#
# This script makes NO changes to the cluster. It only reads and writes
# local files, so it's safe to run any time. Output goes to
# p2_readonly_loop/cluster_dump/ (gitignored below -- add the line to
# .gitignore if it isn't already there).
#
# Usage: bash dump_cluster_state.sh

set -euo pipefail

OUT="cluster_dump"
mkdir -p "$OUT"

echo "Dumping sock-shop deployments (full spec, for diffing against upstream)..."
kubectl get deployments -n sock-shop -o yaml > "$OUT/sock-shop-deployments.yaml"

echo "Dumping sock-shop services..."
kubectl get services -n sock-shop -o yaml > "$OUT/sock-shop-services.yaml"

echo "Dumping sock-shop configmaps (excluding any with 'secret' in the name, just in case)..."
kubectl get configmaps -n sock-shop -o yaml | grep -v -i secret > "$OUT/sock-shop-configmaps.yaml" || \
  kubectl get configmaps -n sock-shop -o yaml > "$OUT/sock-shop-configmaps.yaml"

echo "Dumping sock-shop pods (for image tags actually running, not just declared)..."
kubectl get pods -n sock-shop -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].image}{"\n"}{end}' \
  > "$OUT/sock-shop-running-images.txt"

echo "Dumping ServiceMonitors in sock-shop + monitoring namespaces..."
kubectl get servicemonitors -A -o yaml > "$OUT/servicemonitors.yaml" 2>/dev/null || \
  echo "(no ServiceMonitors found or CRD not present)" > "$OUT/servicemonitors.yaml"

echo "Dumping the wardence-agent RBAC objects (for cross-check against p3_trust_action/manifests/rbac.yaml)..."
kubectl get serviceaccount,role,rolebinding -n sock-shop -l app!=sock-shop -o yaml \
  > "$OUT/rbac-live.yaml" 2>/dev/null || true
kubectl get serviceaccount wardence-agent -n sock-shop -o yaml > "$OUT/rbac-sa-wardence-agent.yaml" 2>/dev/null || true
kubectl get role -n sock-shop -o yaml > "$OUT/rbac-roles.yaml" 2>/dev/null || true
kubectl get rolebinding -n sock-shop -o yaml > "$OUT/rbac-rolebindings.yaml" 2>/dev/null || true

echo "Dumping traffic_gen deployment (for cross-check against traffic_gen/manifest.yaml)..."
kubectl get deployment wardence-traffic-gen -n sock-shop -o yaml > "$OUT/traffic-gen-live.yaml" 2>/dev/null || \
  echo "(traffic-gen deployment not found under that name -- check actual name with: kubectl get deploy -n sock-shop | grep -i traffic)" > "$OUT/traffic-gen-live.yaml"

echo "Dumping the Prometheus CR + Helm release values (for the remote-write-receiver flag)..."
kubectl get prometheus -n monitoring -o yaml > "$OUT/prometheus-cr.yaml" 2>/dev/null || true
helm get values monitoring -n monitoring > "$OUT/prometheus-helm-values.yaml" 2>/dev/null || \
  echo "(helm release name may differ -- run 'helm list -n monitoring' to find it, then: helm get values <name> -n monitoring)" > "$OUT/prometheus-helm-values.yaml"

echo "Dumping chaos-mesh namespace summary (just resource kinds/names, not full spec -- for a sanity check, not a full capture, since Chaos Mesh's own Helm install already covers redeployment)..."
kubectl get all -n chaos-mesh > "$OUT/chaos-mesh-summary.txt" 2>/dev/null || true

echo
echo "Done. Everything written to $OUT/ -- share the contents back so the"
echo "real patch scripts can be built from live values, not buildlog memory."
echo
echo "If $OUT/ isn't already gitignored, add this line to .gitignore:"
echo "  p2_readonly_loop/cluster_dump/"
