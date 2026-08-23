#!/usr/bin/env bash
# arm64 rebuild -- orders, image #3 of 7 (2026-08-23, Point 2 of the
# deployment checklist). Same cross-compile pattern proven on carts
# (image #2): Java bytecode is architecture-independent, so the Maven
# build runs at native $BUILDPLATFORM speed, only the final runtime
# stage targets arm64, and that stage has no RUN commands so buildx
# doesn't need QEMU to assemble it.
#
# Real source: https://github.com/microservices-demo/orders, tag 0.4.7 --
# confirmed live 2026-08-23 to exist and match this project's exact
# deployed image tag (weaveworksdemos/orders:0.4.7, confirmed via a real
# live cluster dump: p2_readonly_loop/cluster_dump/sock-shop-deployments.yaml).
#
# Real, DIFFERENT situation from carts, found this session, not assumed:
# orders' real Dockerfile (docker/orders/Dockerfile in the source repo,
# NOT the repo root -- a different layout than carts/front-end) does
# not build from a generic JDK base at all:
#
#   FROM weaveworksdemos/msd-java:latest
#   WORKDIR /usr/src/app
#   COPY *.jar ./app.jar
#   RUN chown -R ${SERVICE_USER}:${SERVICE_GROUP} ./app.jar
#   USER ${SERVICE_USER}
#   ENV JAVA_OPTS "-Djava.security.egd=file:/dev/urandom"
#   ENTRYPOINT ["/usr/local/bin/java.sh","-jar","./app.jar", "--port=80"]
#
# weaveworksdemos/msd-java:latest is a shared, project-internal base
# image (defines SERVICE_USER/SERVICE_GROUP and the /usr/local/bin/java.sh
# wrapper) -- checked directly against Docker Hub's registry API and
# confirmed AMD64-ONLY, last pushed 2017-08-10, source Dockerfile not
# locatable in any live microservices-demo repo (the project that built
# it is long defunct). Not worth reverse-engineering or reproducing --
# its only real jobs (a non-root user, and a java.sh wrapper that almost
# certainly just does `exec java "$@"` for clean signal handling) are
# not required for this app to run correctly, and every other rebuilt
# service in this project (front-end, carts) already runs as root in
# its own upstream Dockerfile with no issue. Locked: skip msd-java
# entirely, build directly against the same amazoncorretto:8-alpine
# base already proven multi-arch on carts, same ENTRYPOINT shape as
# carts's own real Dockerfile (java -jar directly, no wrapper script).
#
# Requires: docker with buildx (already set up from the front-end/carts
# rebuilds), `docker login ghcr.io` already done.
#
# Usage: bash rebuild_arm64_orders.sh

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.4.7"
SRC_REPO="https://github.com/microservices-demo/orders.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/orders"

cd "${WORKDIR}/orders"

echo "Real original Dockerfile (docker/orders/Dockerfile, for reference -- NOT used as-is, see script header):"
cat docker/orders/Dockerfile

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

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/orders:${IMAGE_TAG}-arm64 ..."
docker buildx inspect wardence-arm64-builder >/dev/null 2>&1 || \
  docker buildx create --use --name wardence-arm64-builder --driver-opt network=host
docker buildx use wardence-arm64-builder

docker buildx build \
  --platform linux/arm64 \
  -t "ghcr.io/${GHCR_USER}/orders:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/orders:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual, on WSL2 -- do NOT run automatically):"
echo "  1. Make the GHCR package public (or add an imagePullSecret) so k3s can pull it without auth."
echo "  2. Smoke test under arm64 emulation (expect a real Mongo/orders-db DNS failure standalone, same as carts's smoke test -- that's fine, it confirms the JVM/Spring stack itself boots):"
echo "     docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/orders:${IMAGE_TAG}-arm64"
echo "  3. kubectl set image deployment/orders orders=ghcr.io/${GHCR_USER}/orders:${IMAGE_TAG}-arm64 -n sock-shop --dry-run=client -o yaml"
echo "     (dry-run first -- this is amd64-cluster-only right now, the real arm64 target is Oracle, not WSL2)"
