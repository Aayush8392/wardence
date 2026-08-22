#!/usr/bin/env bash
# crash-loop's demo-visibility fix (session 2026-08-1x, full design in
# wardence_crash_loop_warm_standby_LOCKED_SPEC.md -- not committed,
# private working notes). A permanent second copy of carts ("carts-warm"),
# identical pod spec, running continuously alongside the real "carts"
# deployment it doesn't replace. Backs the real Operator-side warm-standby
# cooldown mechanism (`crash_loop_ready` in operator_api.py) -- NOT part
# of the upstream Sock Shop manifest, purely additive.
#
# Reconstructed 2026-08-23 from the live cluster's real current spec
# (deployment-readiness effort). This is a full new Deployment, applied
# via `kubectl apply`, not a patch -- safe to rerun (idempotent).
#
# Usage: bash deploy_carts_warm_standby.sh

set -euo pipefail

NAMESPACE="sock-shop"

echo "Applying carts-warm (permanent warm-standby copy of carts)..."
kubectl apply -n "$NAMESPACE" -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: carts-warm
  namespace: sock-shop
  labels:
    name: carts-warm
spec:
  replicas: 1
  selector:
    matchLabels:
      name: carts-warm
  strategy:
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 25%
    type: RollingUpdate
  template:
    metadata:
      labels:
        name: carts-warm
    spec:
      nodeSelector:
        beta.kubernetes.io/os: linux
      containers:
        - name: carts
          image: weaveworksdemos/carts:0.4.8
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 80
              protocol: TCP
          env:
            - name: JAVA_OPTS
              value: "-Xms64m -Xmx128m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=true -Dspring.zipkin.baseUrl=http://jaeger.monitoring.svc.cluster.local:9411"
            - name: ZIPKIN
              value: "jaeger.monitoring.svc.cluster.local"
            - name: JAVA_TOOL_OPTIONS
              value: "-Djava.security.egd=file:/dev/./urandom -Dspring.jmx.enabled=false"
            - name: SPRING_MAIN_LAZY_INITIALIZATION
              value: "true"
          resources:
            limits:
              cpu: 300m
              memory: 500Mi
            requests:
              cpu: 100m
              memory: 200Mi
          securityContext:
            capabilities:
              add: ["NET_BIND_SERVICE"]
              drop: ["all"]
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            runAsUser: 10001
          readinessProbe:
            httpGet: {path: /health, port: 80, scheme: HTTP}
            initialDelaySeconds: 60
            periodSeconds: 10
            timeoutSeconds: 1
            successThreshold: 1
            failureThreshold: 30
          livenessProbe:
            httpGet: {path: /health, port: 80, scheme: HTTP}
            initialDelaySeconds: 60
            periodSeconds: 10
            timeoutSeconds: 1
            successThreshold: 1
            failureThreshold: 60
          volumeMounts:
            - mountPath: /tmp
              name: tmp-volume
      volumes:
        - name: tmp-volume
          emptyDir:
            medium: Memory
EOF

echo "Waiting for rollout..."
kubectl rollout status deployment/carts-warm -n "$NAMESPACE" --timeout=300s

echo
echo "Done. Note: this is real, LOAD-BEARING infra for crash-loop's demo-"
echo "visibility mechanism (Operator's crash_loop_ready field), not"
echo "decoration -- see the private wardence_crash_loop_warm_standby_LOCKED_SPEC.md"
echo "for the full label-swap/cooldown design this backs."
