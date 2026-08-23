#!/usr/bin/env bash
# arm64 rebuild -- user-db, the custom weaveworksdemos MongoDB+seed-data
# image, missed by the original "7 custom images" rebuild pass for the
# same reason as catalogue-db (DB image, not an app service) -- found
# live on wardence-prod hitting "exec format error".
#
# Real, checked-not-assumed finding: unlike catalogue-db, this image's
# real base (mongo:3, confirmed via docker/user-db/Dockerfile in the user
# repo) ALREADY has a genuine arm64 build on Docker Hub (verified
# directly against the registry API's manifest list) -- no base-image
# swap needed here. The weaveworksdemos-published image is just an old,
# amd64-only BUILD of an otherwise-portable Dockerfile; a straight
# buildx rebuild targeting arm64 should be sufficient.
#
# Real, load-bearing quirk in this Dockerfile, worth knowing before
# running this: its RUN step actually STARTS a real mongod process
# during the build itself (mongod --fork ...), runs the seed script
# against it, then shuts it down -- the seeded data ends up baked into
# the image layer. Because this needs a genuinely running mongod, not
# just file copies, and we're building linux/arm64 on an amd64 WSL2 host,
# this RUN step executes under QEMU emulation (real, not
# BUILDPLATFORM-avoidable the way the Go/Java cross-compiles were,
# since there's no separate "compile natively, only assemble under
# emulation" split possible when the thing that needs emulating IS the
# database server itself). Expect this to be noticeably slower than the
# Go/Java rebuilds, and if it hangs or produces empty seeded data,
# QEMU's occasional flakiness with process forking is the first thing to
# suspect, not a Dockerfile bug.
#
# Real source: https://github.com/microservices-demo/user.git --
# user-db's Dockerfile+seed scripts live at docker/user-db/ inside the
# SAME repo as the user service.
#
# Requires: docker with buildx + the wardence-arm64-builder created in
# the front-end rebuild session (network=host), and `docker login
# ghcr.io` already done with a GitHub PAT.
#
# Usage: bash rebuild_arm64_user_db.sh

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.3.0"
SRC_REPO="https://github.com/microservices-demo/user.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} (user-db lives at docker/user-db/ inside this repo) into ${WORKDIR}..."
git clone --depth 1 "${SRC_REPO}" "${WORKDIR}/user"

cd "${WORKDIR}/user/docker/user-db"

echo "Real upstream Dockerfile for reference:"
cat Dockerfile

echo "Building linux/arm64 image (unmodified Dockerfile -- base already multi-arch, only the build target changes) and pushing to ghcr.io/${GHCR_USER}/user-db:${IMAGE_TAG}-arm64 ..."
echo "NOTE: this RUN-a-real-mongod-during-build step under QEMU emulation can take several minutes -- this is expected, not a hang, unless it exceeds ~10 minutes with no output change."
docker buildx build \
  --builder wardence-arm64-builder \
  --platform linux/arm64 \
  --no-cache \
  --provenance=false \
  --sbom=false \
  -t "ghcr.io/${GHCR_USER}/user-db:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/user-db:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual smoke test):"
echo "  docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/user-db:${IMAGE_TAG}-arm64 &"
echo "  (expect: mongod starts clean under /etc/mongodb.conf; no crash)"
echo "  Real, separate verification worth doing once this is live in the cluster (not just"
echo "  a standalone smoke test): confirm the seeded accounts/address/card data actually"
echo "  landed -- a QEMU-emulation hiccup during the build's seed step could plausibly"
echo "  produce a mongod that starts fine but has an empty/partial database, a silent"
echo "  failure mode a bare 'does it boot' smoke test would not catch."
