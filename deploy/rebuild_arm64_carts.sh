#!/usr/bin/env bash
# arm64 rebuild -- carts, image #2 of 7 (2026-08-23, Point 2 of the
# deployment checklist). Front-end (image #1) is done -- this reuses its
# proven builder setup (host-networked buildx builder, GHCR, QEMU
# already installed on this WSL2 box), same pattern the user asked for.
#
# Real source: https://github.com/microservices-demo/carts, tag 0.4.8 --
# confirmed live 2026-08-23 to exist and match this project's exact
# deployed image tag (weaveworksdemos/carts:0.4.8, see
# p2_readonly_loop/deploy_carts_warm_standby.sh /
# patch_carts_db_mongo_pin.sh).
#
# Real, DIFFERENT situation from front-end: carts' Dockerfile only COPYs
# a prebuilt target/*.jar -- it does not build from source. The real
# upstream build command (confirmed live via its own README) is
# `mvn -DskipTests package`. Two ways to get that jar into an arm64
# image were considered:
#   (a) run `mvn package` under QEMU arm64 emulation inside the final
#       stage, same shape as front-end's `yarn install` -- works, but
#       Java bytecode compilation under emulation is real, unnecessary
#       CPU cost, and the jar itself is architecture-independent bytecode.
#   (b) cross-compile: build the jar in a stage pinned to the NATIVE
#       build platform (--platform=$BUILDPLATFORM, runs at full native
#       speed, no emulation), then COPY the finished jar into the arm64
#       final stage. The final stage has no RUN commands left at all, so
#       buildx doesn't even need QEMU to assemble it.
# (b) is strictly better here and is what this script does -- Java's
# "compile once, run anywhere" bytecode is exactly the case cross-stage
# builds are for.
#
# Real base-image correction, checked directly against Docker Hub's own
# registry API 2026-08-23, NOT assumed from review 60's wording (review
# 60 offered eclipse-temurin:8-jre-alpine OR amazoncorretto:8-alpine as
# interchangeable -- they are not):
#   - eclipse-temurin:8-jre-alpine -> confirmed AMD64-ONLY (no arm64
#     manifest at all). Would silently produce an amd64 image under a
#     multi-arch tag name -- exactly the kind of stale-assumption bug
#     this project's standing "verify numbers before coding" rule exists
#     to catch.
#   - amazoncorretto:8-alpine -> confirmed genuinely multi-arch
#     (amd64 + arm64/v8, both present in the real manifest list).
# Locked: amazoncorretto:8-alpine for the final (runtime) stage.
# maven:3-eclipse-temurin-8 for the build stage is fine regardless of its
# own arch support, since --platform=$BUILDPLATFORM always pins it to
# the host's native architecture (amd64 on this WSL2 box).
#
# Requires: docker with buildx (already set up from the front-end
# rebuild), `docker login ghcr.io` already done with a GitHub PAT
# (write:packages scope).
#
# Usage: bash rebuild_arm64_carts.sh

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.4.8"
SRC_REPO="https://github.com/microservices-demo/carts.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/carts"

cd "${WORKDIR}/carts"

echo "Real original Dockerfile:"
cat Dockerfile

echo "Rewriting Dockerfile as a 2-stage build (native-platform Maven build -> arm64 Corretto runtime)..."
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
echo "Patched Dockerfile:"
cat Dockerfile

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/carts:${IMAGE_TAG}-arm64 ..."
# Reuses the host-networked builder created during the front-end rebuild
# (fixes the same WSL2 buildx-bridge-network TLS-timeout issue) -- create
# it fresh only if it doesn't already exist.
docker buildx inspect wardence-arm64-builder >/dev/null 2>&1 || \
  docker buildx create --use --name wardence-arm64-builder --driver-opt network=host
docker buildx use wardence-arm64-builder

docker buildx build \
  --platform linux/arm64 \
  -t "ghcr.io/${GHCR_USER}/carts:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/carts:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual, on WSL2 -- do NOT run automatically):"
echo "  1. Make the GHCR package public (or add an imagePullSecret) so k3s can pull it without auth."
echo "  2. Confirm the image at least starts under QEMU emulation locally as a smoke test:"
echo "     docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/carts:${IMAGE_TAG}-arm64 java -version"
echo "     (real jar startup needs MongoDB reachable -- a bare 'java -version' just confirms the JVM/base image itself runs under emulation, same smoke-test scope as front-end's 'node --version')"
echo "  3. kubectl set image deployment/carts carts=ghcr.io/${GHCR_USER}/carts:${IMAGE_TAG}-arm64 -n sock-shop --dry-run=client -o yaml"
echo "     (dry-run first -- this is amd64-cluster-only right now, the real arm64 target is Oracle, not WSL2)"
