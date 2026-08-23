#!/usr/bin/env bash
# Section 1 (Helm portion) of deploy/README.md -- Prometheus/Grafana, Chaos Mesh,
# Loki/promtail/Jaeger, on wardence-prod. Run AFTER `kubectl apply -n sock-shop -f
# .../complete-demo.yaml` and the RBAC cage (rbac.yaml) from section 1's kubectl block.
#
# Re-adds the Helm repos first since chart versions were flagged "not yet verified"
# in the README -- `helm repo update` pulls whatever's current, not a pinned version.
# If a chart has since introduced a breaking change, this will surface it loudly at
# install time rather than silently deploying something stale.
set -euo pipefail

echo "==> Adding Helm repos"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add jaegertracing https://jaegertracing.github.io/helm-charts
helm repo update

echo "==> Installing Prometheus/Grafana (kube-prometheus-stack)"
echo "    remote-write receiver enabled -- required for traffic_gen's k6 metrics"
helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --set prometheus.prometheusSpec.enableRemoteWriteReceiver=true

echo "==> Installing Chaos Mesh (k3s containerd socket path)"
helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock

echo "==> Installing Loki"
helm install loki grafana/loki -n monitoring -f p5_dl_hardening/manifests/loki-values.yaml

echo "==> Wiring Loki into the Grafana datasource"
kubectl apply -n monitoring -f p5_dl_hardening/manifests/loki-grafana-datasource.yaml

echo "==> Installing promtail"
helm install promtail grafana/promtail -n monitoring -f p5_dl_hardening/manifests/promtail-values.yaml

echo "==> Installing Jaeger"
helm install jaeger jaegertracing/jaeger -n monitoring -f p5_dl_hardening/manifests/jaeger-values.yaml

echo "==> Done. Verify with:"
echo "    kubectl get pods -n monitoring"
echo "    kubectl get pods -n chaos-mesh"
