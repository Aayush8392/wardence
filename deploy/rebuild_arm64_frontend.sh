#!/usr/bin/env bash
# Canary arm64 rebuild -- front-end only (2026-08-23, Point 2 of the
# deployment checklist). Per review 60 + review 63 (Kimi/Qwen), rebuild
# and validate ONE service first, not all 6-7 custom weaveworksdemos/*
# images at once.
#
# Real source: https://github.com/microservices-demo/front-end, tag
# 0.3.12 -- confirmed live 2026-08-23 to exist, be archived (read-only,
# fine for a one-time clone), and have a Dockerfile matching this
# project's exact deployed image tag (weaveworksdemos/front-end:0.3.12).
#
# Requires: docker with buildx (Docker Desktop on Windows/WSL2 has this
# built in), and `docker login ghcr.io` already done with a GitHub PAT
# that has `write:packages` scope (classic PAT, or fine-grained with
# Packages: write).
#
# Usage: bash rebuild_arm64_frontend.sh

set -euo pipefail

GHCR_USER="aayush8392"   # lowercase -- GHCR/Docker registries require it
IMAGE_TAG="0.3.12"
SRC_REPO="https://github.com/microservices-demo/front-end.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/front-end"

cd "${WORKDIR}/front-end"

echo "Real Dockerfile found:"
ls -la Dockerfile

# Real base-image swap (2026-08-23, locked decision from review 60):
# node:4-alpine (2016) predates Docker's multi-arch manifest-list support
# and has no arm64 variant -- confirmed by its absence from Docker Hub's
# current tag listing for the `node` image. Swapping to node:18-alpine
# (real, current, multi-arch LTS tag) per the already-locked "swap base
# image to a multi-arch Node Alpine tag" decision. This is a genuine,
# unverified compatibility jump (Node 4 -> 18, ~10 major versions) --
# the whole point of canarying front-end first is to surface exactly
# this kind of gap now, cheaply, rather than during the full rebuild.
echo "Swapping base image node:4-alpine -> node:18-alpine (real multi-arch tag, review 60's locked fix)..."
sed -i 's/^FROM node:4-alpine/FROM node:18-alpine/' Dockerfile
echo "Patched Dockerfile FROM line:"
head -1 Dockerfile

# Real fix (2026-08-23): yarn install timed out under QEMU arm64
# emulation (ESOCKETTIMEDOUT after ~30s per retry, 4 retries) -- this is
# emulation CPU/IO overhead, not a genuine network problem (the same
# registry pulled fine via plain `docker pull` on the host). Bumping
# yarn's network timeout to 10 minutes to absorb the emulation slowdown.
sed -i 's/RUN yarn install/RUN yarn install --network-timeout 600000/' Dockerfile
echo "Patched yarn install line:"
grep 'yarn install' Dockerfile

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/front-end:${IMAGE_TAG}-arm64 ..."
# Real fix (2026-08-23): the buildx builder's own isolated bridge
# network hit repeated TLS handshake timeouts resolving Docker Hub
# metadata, while plain `docker pull` on the host (different network
# path) worked fine every time -- a known WSL2 Docker networking quirk
# for nested-container network namespaces, not a genuine registry
# outage. --driver-opt network=host makes the builder share the host's
# network stack directly instead of its own bridge.
docker buildx rm wardence-arm64-builder 2>/dev/null || true
docker buildx create --use --name wardence-arm64-builder --driver-opt network=host
docker buildx build \
  --platform linux/arm64 \
  -t "ghcr.io/${GHCR_USER}/front-end:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/front-end:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual, on WSL2 -- do NOT run automatically):"
echo "  1. Make the GHCR package public (or add an imagePullSecret) so k3s can pull it without auth."
echo "  2. kubectl set image deployment/front-end front-end=ghcr.io/${GHCR_USER}/front-end:${IMAGE_TAG}-arm64 -n sock-shop --dry-run=client -o yaml"
echo "     (dry-run first -- this is amd64-cluster-only right now, the real arm64 target is Oracle, not WSL2)"
echo "  3. Confirm the image at least starts under QEMU emulation locally as a smoke test:"
echo "     docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/front-end:${IMAGE_TAG}-arm64 node --version"
