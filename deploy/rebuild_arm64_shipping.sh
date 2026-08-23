#!/usr/bin/env bash
# arm64 rebuild -- shipping, image #5 of 7 (2026-08-23, Point 2 of the
# deployment checklist). Same cross-compile pattern as
# carts/orders/queue-master (images #2-4): Java bytecode is
# architecture-independent, so the Maven build runs at native
# $BUILDPLATFORM speed, only the final runtime stage targets arm64.
#
# Real source: https://github.com/microservices-demo/shipping, tag
# 0.4.8 -- confirmed live 2026-08-23 to exist and match this project's
# exact deployed image tag (weaveworksdemos/shipping:0.4.8, confirmed
# via a real live cluster dump:
# p2_readonly_loop/cluster_dump/sock-shop-deployments.yaml).
#
# Same real msd-java gap as orders/queue-master (images #3/#4):
# shipping's own upstream Dockerfile (docker/shipping/Dockerfile) is
# `FROM weaveworksdemos/msd-java:latest` -- amd64-only, unrecoverable
# source, same fix (build directly against amazoncorretto:8-alpine).
#
# REAL, LOAD-BEARING DIFFERENCE FROM orders/queue-master, found and
# fixed before building, not after: shipping -- and ONLY shipping -- is
# the live target of install_shipping_leak_agent.py's real production
# memory-leak mechanism (the multi-session demo-visibility arc closed
# 2026-08-23, see wardence_buildlog.md). That script patches the
# Deployment's JAVA_OPTS env var to append `-javaagent:/agent/leak-agent.jar`,
# relying on the ORIGINAL msd-java base's /usr/local/bin/java.sh wrapper
# script actually reading $JAVA_OPTS and passing it through to the `java`
# invocation at container start.
#
# carts/orders/queue-master's rebuilt Dockerfiles all use a fixed-array
# ENTRYPOINT (["java", ..., "-jar", "./app.jar", ...]) -- this does NOT
# expand $JAVA_OPTS at all (Docker's exec-form ENTRYPOINT never invokes a
# shell). Using that same pattern here would let the image boot
# perfectly fine standalone, while silently making the -javaagent patch
# a complete no-op -- breaking the memory-leak mechanism with no visible
# error anywhere. Confirmed real, not hypothetical: install_shipping_leak_agent.py
# patches env var name `JAVA_OPTS` specifically (not the auto-picked-up
# JAVA_TOOL_OPTIONS), so it MUST go through a shell that expands it.
#
# Fix, THIS SCRIPT ONLY: shell-form ENTRYPOINT
# (`exec java $JAVA_OPTS -jar ./app.jar --port=80`) -- matches the real
# behavior of the original java.sh wrapper for this one specific field,
# without trying to reconstruct the rest of that defunct image.
#
# Requires: docker with buildx (already set up from prior rebuilds),
# `docker login ghcr.io` already done.
#
# Usage: bash rebuild_arm64_shipping.sh

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.4.8"
SRC_REPO="https://github.com/microservices-demo/shipping.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/shipping"

cd "${WORKDIR}/shipping"

echo "Real original Dockerfile (docker/shipping/Dockerfile, for reference -- NOT used as-is, see script header):"
cat docker/shipping/Dockerfile

echo "Writing a new repo-root Dockerfile as a 2-stage build (native-platform Maven build -> arm64 Corretto runtime, no msd-java dependency, JAVA_OPTS-aware shell entrypoint for the live -javaagent patch mechanism)..."
cat > Dockerfile <<'EOF'
FROM --platform=$BUILDPLATFORM maven:3-eclipse-temurin-8 AS build
WORKDIR /build
COPY . .
RUN mvn -DskipTests package

FROM amazoncorretto:8-alpine
WORKDIR /usr/src/app
COPY --from=build /build/target/*.jar ./app.jar

ENV JAVA_OPTS=""
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -Djava.security.egd=file:/dev/urandom -jar ./app.jar --port=80"]
EOF
echo "New Dockerfile:"
cat Dockerfile

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/shipping:${IMAGE_TAG}-arm64 ..."
docker buildx inspect wardence-arm64-builder >/dev/null 2>&1 || \
  docker buildx create --use --name wardence-arm64-builder --driver-opt network=host
docker buildx use wardence-arm64-builder

docker buildx build \
  --platform linux/arm64 \
  -t "ghcr.io/${GHCR_USER}/shipping:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/shipping:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual, on WSL2 -- do NOT run automatically):"
echo "  1. Make the GHCR package public (or add an imagePullSecret) so k3s can pull it without auth."
echo "  2. Smoke test under arm64 emulation (expect Tomcat to start clean, no downstream DB dependency for shipping -- unlike carts/orders/queue-master):"
echo "     docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/shipping:${IMAGE_TAG}-arm64"
echo "  3. REAL, IMPORTANT extra check beyond the standard smoke test -- verify JAVA_OPTS actually gets picked up (the whole reason this script differs from orders/queue-master):"
echo "     docker run -d --name shiptest --platform linux/arm64 -e JAVA_OPTS=\"-Dtest.marker=hello\" ghcr.io/${GHCR_USER}/shipping:${IMAGE_TAG}-arm64"
echo "     docker exec shiptest ps aux | grep java"
echo "     (the real running command line MUST show -Dtest.marker=hello -- if it's missing, JAVA_OPTS isn't being expanded and the -javaagent patch would silently do nothing on the real deploy)"
echo "     docker rm -f shiptest"
echo "  4. kubectl set image deployment/shipping shipping=ghcr.io/${GHCR_USER}/shipping:${IMAGE_TAG}-arm64 -n sock-shop --dry-run=client -o yaml"
echo "     (dry-run first -- this is amd64-cluster-only right now, the real arm64 target is Oracle, not WSL2)"
