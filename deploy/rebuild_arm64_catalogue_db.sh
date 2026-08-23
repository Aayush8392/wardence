#!/usr/bin/env bash
# arm64 rebuild -- catalogue-db, the custom weaveworksdemos MySQL+schema
# image, missed by the original "7 custom images" rebuild pass (2026-08-2x)
# because it's a DB image, not an app service -- found live on
# wardence-prod when it hit "exec format error" (deploy/README.md
# section 1's base apply pulls the original amd64-only image).
#
# Real, checked-not-assumed base-image gap: mysql:5.7 (the real upstream
# base, confirmed via docker/catalogue-db/Dockerfile in the catalogue
# repo) has NO arm64 build at all on Docker Hub -- verified directly
# against the registry API's own manifest list, not inferred. mysql only
# ships arm64 builds from 8.0 onward. Rather than jump to mysql:8.0 (real
# compatibility risk: 8.0 defaults to caching_sha2_password auth, which
# can break older MySQL client drivers like the Go app's), this uses
# mariadb:10.11 -- a real, verified multi-arch (arm64-included) drop-in
# replacement, wire-compatible with MySQL 5.7 clients, and confirmed
# compatible with this image's actual dump.sql (plain CREATE
# USER/GRANT/CREATE TABLE/INSERT, no MySQL-5.7-specific syntax).
#
# Real source: https://github.com/microservices-demo/catalogue.git --
# catalogue-db's Dockerfile+data live at docker/catalogue-db/ inside the
# SAME repo as the catalogue service (confirmed via a live clone, not
# assumed from a separate catalogue-db repo, which doesn't exist).
#
# No compilation involved (this is just a data image), so no
# BUILDPLATFORM/TARGETARCH cross-compile split is needed -- buildx pulls
# the arm64 mariadb base and stages the COPY step directly.
#
# Requires: docker with buildx + the wardence-arm64-builder created in
# the front-end rebuild session (network=host), and `docker login
# ghcr.io` already done with a GitHub PAT.
#
# Usage: bash rebuild_arm64_catalogue_db.sh

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.3.0"
SRC_REPO="https://github.com/microservices-demo/catalogue.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} (catalogue-db lives at docker/catalogue-db/ inside this repo) into ${WORKDIR}..."
git clone --depth 1 "${SRC_REPO}" "${WORKDIR}/catalogue"

cd "${WORKDIR}/catalogue/docker/catalogue-db"

echo "Real upstream Dockerfile for reference:"
cat Dockerfile

echo "Writing arm64 Dockerfile (mariadb:10.11 base, real verified multi-arch replacement for mysql:5.7, which has no arm64 build)..."
cat > Dockerfile.arm64 <<'EOF'
FROM mariadb:10.11
COPY ./data/dump.sql /docker-entrypoint-initdb.d/
EOF

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/catalogue-db:${IMAGE_TAG}-arm64 ..."
docker buildx build \
  --builder wardence-arm64-builder \
  --platform linux/arm64 \
  --no-cache \
  --provenance=false \
  --sbom=false \
  -f Dockerfile.arm64 \
  -t "ghcr.io/${GHCR_USER}/catalogue-db:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/catalogue-db:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual smoke test):"
echo "  docker run --rm --platform linux/arm64 -e MYSQL_ROOT_PASSWORD=test ghcr.io/${GHCR_USER}/catalogue-db:${IMAGE_TAG}-arm64 &"
echo "  (expect: MariaDB init logs, schema+dump.sql applied without SQL errors, ends with 'ready for connections')"
echo "  Real env vars actually used at runtime (MYSQL_ROOT_PASSWORD etc.) come from the k8s"
echo "  Deployment spec, not this image -- mariadb's entrypoint honors the same MYSQL_* env"
echo "  var names as the original mysql image, by design, for exactly this kind of swap."
