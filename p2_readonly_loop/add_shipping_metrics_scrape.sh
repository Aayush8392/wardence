#!/bin/bash
# One-off, gitignored: REAL, PERMANENT production infra change -- adds a
# Prometheus scrape target for shipping's existing /metrics endpoint.
# Does NOT touch shipping's Deployment/pods in any way (no rollout, no
# restart, no downtime) -- Services and ServiceMonitors are independent
# objects from the pods they select; only shipping's Service gets a port
# NAME added (port 80 stays port 80, same targetPort, same behavior for
# every existing caller), and a new ServiceMonitor object is created.
#
# Real, answered question this closes (review 59, both Kimi AND Qwen
# independently flagged this as the blocking gate before a new memory-leak
# diagnosis threshold could be picked): check_shipping_prometheus_jvm_scrape.sh
# confirmed live that shipping's real JVM heap metrics (heap_used,
# jvm_memory_bytes_used{area="heap"}, etc. -- already confirmed present in
# shipping's own /metrics response) are NOT currently scraped into
# Prometheus at all. This is Option A from that review: add a real scrape
# target, the same architecture every other class's diagnosis already uses
# (agent.py's one tool, query_prometheus), rather than Option B (a new
# active-Actuator-probe capability bolted onto agent.py just for this one
# class).
#
# Real, proven precedent reused directly, not re-derived: this exact
# ServiceMonitor shape (labels: release=monitoring) was already built and
# confirmed working for catalogue-db's mysqld_exporter sidecar
# (patch_catalogue_db_add_mysqld_exporter.sh, 2026-07-21) -- confirmed
# directly against this cluster's real Prometheus CR
# (serviceMonitorSelector.matchLabels.release=monitoring), not assumed.
# shipping's case is simpler: it already serves real Prometheus-shaped JVM
# metrics on its EXISTING app port (80, /metrics) -- no new sidecar
# container, no new port, unlike catalogue-db's case (MySQL has no native
# Prometheus metrics, needed a whole new exporter container + port 9104).
#
# Real Service port shape confirmed live before writing this (not assumed):
#   kubectl get svc shipping -n sock-shop -o jsonpath='{.spec.ports}'
#   -> [{"port":80,"protocol":"TCP","targetPort":80}]
# Unnamed, same situation catalogue-db's port 3306 was in before its own
# patch -- Prometheus Operator's ServiceMonitor endpoints[].port field
# references a NAMED port on the Service object, so the port needs naming
# first. Named "http" here (not "metrics"), since this single port serves
# BOTH real app traffic AND /metrics -- "metrics" would be misleading for a
# port that's actually the app's main listener.
#
# Real, deliberate scope note: this script ONLY adds scraping capability.
# It does NOT touch the memory-leak agent, injector.py, agent.py, or
# shipping's JAVA_OPTS in any way -- those are separate, still-unbuilt
# production-build steps (review 59's file-by-file scope). Running this
# script is safe and independently useful on its own, whether or not the
# rest of the production build ever lands.
#
# Run: bash add_shipping_metrics_scrape.sh

set -uo pipefail
NS=sock-shop

echo "1/2: naming shipping's existing port 80 as 'http' (real Service metadata patch only --"
echo "     same port number/targetPort, zero behavior change for any existing caller, no pod"
echo "     restart triggered -- Services are independent objects from the Deployment/pods)..."
kubectl patch svc shipping -n "$NS" --type=merge -p='{
  "spec": {
    "ports": [
      {"name": "http", "port": 80, "protocol": "TCP", "targetPort": 80}
    ]
  }
}'

echo ""
echo "2/2: applying the ServiceMonitor (release=monitoring, the real label this cluster's"
echo "     Prometheus CR selects on -- confirmed directly for catalogue-db's own ServiceMonitor,"
echo "     reused here rather than re-derived)..."
kubectl apply -f - <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: shipping-jvm-metrics
  namespace: sock-shop
  labels:
    release: monitoring
spec:
  namespaceSelector:
    matchNames:
      - sock-shop
  selector:
    matchLabels:
      name: shipping
  endpoints:
    - port: http
      path: /metrics
      interval: 15s
EOF

echo ""
echo "=== Done. Real verification (run separately, give Prometheus ~15-30s to complete its first scrape cycle): ==="
echo "  bash p2_readonly_loop/check_shipping_prometheus_jvm_scrape.sh"
echo "Expect the 'app-level JVM/heap-related series' section to now be non-empty."
