#!/usr/bin/env bash
# arm64 rebuild -- queue-master, image #4 of 7 (2026-08-23, Point 2 of
# the deployment checklist). Same cross-compile pattern as carts/orders
# (images #2/#3): Java bytecode is architecture-independent, so the
# Maven build runs at native $BUILDPLATFORM speed, only the final
# runtime stage targets arm64, no QEMU needed to assemble it.
#
# Real source: https://github.com/microservices-demo/queue-master, tag
# 0.3.1 -- confirmed live 2026-08-23 to exist and match this project's
# exact deployed image tag (weaveworksdemos/queue-master:0.3.1,
# confirmed via a real live cluster dump:
# p2_readonly_loop/cluster_dump/sock-shop-deployments.yaml).
#
# Same real gap as orders (image #3): queue-master's own upstream
# Dockerfile (docker/queue-master/Dockerfile) is:
#   FROM weaveworksdemos/msd-java:latest
#   WORKDIR /usr/src/app
#   COPY *.jar ./app.jar
#   ENV JAVA_OPTS "-Djava.security.egd=file:/dev/urandom"
#   ENTRYPOINT ["/usr/local/bin/java.sh","-jar","./app.jar", "--port=80"]
# msd-java is amd64-only (confirmed via Docker Hub's registry API,
# last pushed 2017, source unrecoverable) -- same fix as orders: build
# directly against amazoncorretto:8-alpine, no msd-java dependency.
#
# UNLIKE shipping (image #5, not this one): queue-master is not a
# target of any live JAVA_OPTS-patching mechanism (that's shipping's
# memory-leak -javaagent injection specifically) -- a plain fixed-array
# ENTRYPOINT, same shape as carts/orders, is safe here. No JAVA_OPTS
# shell-expansion needed.
#
# Requires: docker with buildx (already set up from prior rebuilds),
# `docker login ghcr.io` already done.
#
# Usage: bash rebuild_arm64_queue_master.sh

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.3.1"
SRC_REPO="https://github.com/microservices-demo/queue-master.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/queue-master"

cd "${WORKDIR}/queue-master"

echo "Real original Dockerfile (docker/queue-master/Dockerfile, for reference -- NOT used as-is, see script header):"
cat docker/queue-master/Dockerfile

echo "Writing a new repo-root Dockerfile as a 2-stage build (native-platform Maven build -> arm64 Corretto runtime, no msd-java dependency)..."
cat > Dockerfile <<'EOF'
FROM --platform=$BUILDPLATFORM maven:3-eclipse-temurin-8 AS build
WORKDIR /build
COPY . .
RUN mvn -DskipTests package

FROM amazoncorretto:8-alpine
WORKDIR /usr/src/app
COPY --from=build /build/target/*.jar ./app.jar

ENTRYPOINT ["java","-Djava.security.egd=file:/dev/urandom","-jar","./app.jar", "--port=80"]
EOF
echo "New Dockerfile:"
cat Dockerfile

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/queue-master:${IMAGE_TAG}-arm64 ..."
docker buildx inspect wardence-arm64-builder >/dev/null 2>&1 || \
  docker buildx create --use --name wardence-arm64-builder --driver-opt network=host
docker buildx use wardence-arm64-builder

docker buildx build \
  --platform linux/arm64 \
  -t "ghcr.io/${GHCR_USER}/queue-master:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/queue-master:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual, on WSL2 -- do NOT run automatically):"
echo "  1. Make the GHCR package public (or add an imagePullSecret) so k3s can pull it without auth."
echo "  2. Smoke test under arm64 emulation (expect a real RabbitMQ connection failure standalone -- that's fine, it confirms the JVM/Spring stack itself boots):"
echo "     docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/queue-master:${IMAGE_TAG}-arm64"
echo "  3. kubectl set image deployment/queue-master queue-master=ghcr.io/${GHCR_USER}/queue-master:${IMAGE_TAG}-arm64 -n sock-shop --dry-run=client -o yaml"
echo "     (dry-run first -- this is amd64-cluster-only right now, the real arm64 target is Oracle, not WSL2)"
