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
# Fixed 2026-08-2x: the image and resource limits used to be a second,
# independently-hardcoded copy of carts' own spec -- real drift already
# happened once (this script still pointed at the x86 weaveworksdemos/*
# image + the pre-Oracle 500Mi memory limit after `carts` itself had
# already been patched to the arm64 GHCR image + 1Gi on wardence-prod,
# which crash-looped carts-warm on both counts). Fixed at the root: read
# carts' REAL, currently-live image + memory/cpu limits from the cluster
# at apply time and mirror them onto carts-warm, instead of a second
# maintained copy that can silently go stale. Works unmodified on both
# WSL2 dev (x86 image, whatever limits are set there) and wardence-prod
# (arm64 image, 1Gi) -- no per-environment values, no flags.
#
# imagePullPolicy fixed to Always 2026-08-2x (same reasoning as carts'
# own patch, patch_carts_readiness_and_jvm_tuning.sh -- carts-warm
# mirrors carts' image tag exactly, so it inherits the same same-tag
# staleness risk on any rebuild-and-repush).
#
# Usage: bash deploy_carts_warm_standby.sh

set -euo pipefail

NAMESPACE="sock-shop"

echo "Reading carts' REAL current image + resource limits from the live cluster..."
CARTS_IMAGE=$(kubectl get deployment carts -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].image}')
CARTS_MEM_LIMIT=$(kubectl get deployment carts -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')
CARTS_CPU_LIMIT=$(kubectl get deployment carts -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}')

if [[ -z "$CARTS_IMAGE" ]]; then
  echo "ERROR: could not read carts' current image -- has 'carts' been deployed yet?" >&2
  echo "       Run README.md section 1 (and 1a on arm64) first." >&2
  exit 1
fi

# Fallbacks only if carts somehow has no limits set at all (shouldn't
# happen given the upstream manifest + this project's own patches always
# set them, but fail toward a safe default rather than an empty resources
# block).
CARTS_MEM_LIMIT="${CARTS_MEM_LIMIT:-500Mi}"
CARTS_CPU_LIMIT="${CARTS_CPU_LIMIT:-300m}"

echo "  image:     $CARTS_IMAGE"
echo "  mem limit: $CARTS_MEM_LIMIT"
echo "  cpu limit: $CARTS_CPU_LIMIT"
echo
echo "Applying carts-warm (permanent warm-standby copy of carts, mirroring its real live image/limits)..."
kubectl apply -n "$NAMESPACE" -f - <<EOF
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
        kubernetes.io/os: linux
      containers:
        - name: carts
          image: ${CARTS_IMAGE}
          imagePullPolicy: Always
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
              cpu: ${CARTS_CPU_LIMIT}
              memory: ${CARTS_MEM_LIMIT}
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
