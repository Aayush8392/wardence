#!/bin/sh
# Adds a mysqld_exporter sidecar to catalogue-db so Prometheus has a real
# signal (mysql_global_status_threads_connected /
# mysql_global_variables_max_connections) for the connection-pool-
# exhaustion fault class -- confirmed via direct query (2026-07-21) that
# NO mysql_* metrics existed in Prometheus before this, since Sock Shop
# ships no MySQL exporter by default. Root password reused from
# catalogue-db's own MYSQL_ROOT_PASSWORD env var (plaintext, matching
# how that value is already stored -- this is a disposable local lab,
# not a case needing a Secret).
#
# Three steps: (1) add the sidecar container, sharing catalogue-db's
# pod network namespace so it can reach MySQL on localhost:3306,
# (2) add a named "metrics" port to catalogue-db's existing Service
# (k8s requires ALL ports named once there's more than one, so the
# existing unnamed 3306 port gets named "mysql" in the same patch),
# (3) a ServiceMonitor labeled release=monitoring, confirmed to be
# what this cluster's Prometheus CR actually selects on
# (serviceMonitorSelector.matchLabels.release=monitoring, checked
# directly, not assumed).
#
# Found the hard way (2026-07-21): mysqld_exporter v0.15.x dropped
# DATA_SOURCE_NAME support -- it now requires --mysqld.username /
# --mysqld.address as args plus MYSQLD_EXPORTER_PASSWORD as an env var
# (confirmed via the exporter's own startup log: "no user specified in
# section or parent" / "Error parsing host config"). The version
# number is pinned (v0.15.1) specifically so this doesn't silently
# break again on a future pull.
#
# Also found the hard way: --mysqld.address=localhost:3306 resolved to
# IPv6 [::1] inside this container, but MySQL only listens on IPv4 --
# confirmed via the exporter's own log ("dial tcp [::1]:3306: connect:
# connection refused"). Using 127.0.0.1 explicitly avoids the ambiguity.

set -e

echo "1/3: adding mysqld-exporter sidecar to catalogue-db..."
kubectl patch deployment catalogue-db -n sock-shop --type=json -p='[
  {
    "op": "add",
    "path": "/spec/template/spec/containers/-",
    "value": {
      "name": "mysqld-exporter",
      "image": "prom/mysqld-exporter:v0.15.1",
      "args": [
        "--mysqld.username=root",
        "--mysqld.address=127.0.0.1:3306"
      ],
      "env": [
        {"name": "MYSQLD_EXPORTER_PASSWORD", "value": "fake_password"}
      ],
      "ports": [
        {"containerPort": 9104, "name": "metrics"}
      ]
    }
  }
]'

echo "2/3: naming catalogue-db's existing port + adding the metrics port to its Service..."
kubectl patch svc catalogue-db -n sock-shop --type=merge -p='{
  "spec": {
    "ports": [
      {"name": "mysql", "port": 3306, "protocol": "TCP"},
      {"name": "metrics", "port": 9104, "protocol": "TCP"}
    ]
  }
}'

echo "3/3: applying the ServiceMonitor..."
kubectl apply -f - <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: catalogue-db-mysqld-exporter
  namespace: sock-shop
  labels:
    release: monitoring
spec:
  namespaceSelector:
    matchNames:
      - sock-shop
  selector:
    matchLabels:
      name: catalogue-db
  endpoints:
    - port: metrics
      interval: 15s
EOF

echo "Done. Verify with:"
echo "  kubectl rollout status deployment/catalogue-db -n sock-shop"
echo "  curl -s -G 'http://localhost:9090/api/v1/query' --data-urlencode 'query={__name__=~\"mysql.*\"}'"
