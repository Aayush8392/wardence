#!/usr/bin/env bash
# Wardence-patched front-end rebuild -- BOTH amd64 (WSL2) and arm64 (Oracle).
#
# Why this exists (2026-08-26): sock-shop's front-end crashes the whole
# Node process whenever a backend service becomes unreachable. Real,
# confirmed root cause from a live episode's `kubectl logs --previous`:
#
#   SyntaxError: Unexpected token u
#       at Object.parse (native)
#       at Request._callback (/usr/src/app/api/cart/index.js:79:34)
#       at Request.onRequestError (...request.js:884:8)
#
# api/cart/index.js:79 was `callback(error, JSON.parse(body))` -- JSON.parse
# evaluated as an ARGUMENT, so it ran before callback could inspect `error`.
# With catalogue's endpoint gone, body === undefined, JSON.parse(undefined)
# throws from inside an EventEmitter (where Express's error middleware
# cannot reach), Node exits(1), and the ENTIRE storefront goes down --
# even though only catalogue was faulted. This made every catalogue-target
# fault class (oom, under-provisioned-replicas) look like a total site
# outage instead of a scoped one. See deploy/frontend_json_parse_fix.patch.
#
# Builds BOTH architectures deliberately: WSL2 (amd64) was still running
# the original weaveworksdemos/front-end:0.3.12 (node v4.8.0) while Oracle
# ran the node:18 arm64 rebuild -- so the two hosts were running different
# runtimes and we were validating different code on each.
#
# Requires: docker with buildx, and `docker login ghcr.io` already done
# with a PAT carrying write:packages.
#
# Usage: bash rebuild_wardence_frontend.sh

set -euo pipefail

GHCR_USER="aayush8392"   # lowercase -- registries require it
UPSTREAM_TAG="0.3.12"
# New tag, deliberately distinct from 0.3.12-arm64 so a rollback is just
# pointing FRONT_END_IMAGE_BASELINE back at the old tag.
WARDENCE_TAG="0.3.12-wardence1"
SRC_REPO="https://github.com/microservices-demo/front-end.git"
PATCH_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/frontend_json_parse_fix.patch"
WORKDIR="$(mktemp -d)"

if [ ! -f "${PATCH_FILE}" ]; then
  echo "ERROR: patch not found at ${PATCH_FILE}" >&2
  exit 1
fi

echo "Cloning ${SRC_REPO} @ tag ${UPSTREAM_TAG} into ${WORKDIR}..."
git clone --quiet --branch "${UPSTREAM_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/front-end"
cd "${WORKDIR}/front-end"

# Fail loudly if upstream ever drifts from what the patch was built against,
# rather than silently producing an unpatched image.
echo "Applying ${PATCH_FILE}..."
git apply --check "${PATCH_FILE}"
git apply "${PATCH_FILE}"
echo "Patch applied. Verifying no unguarded JSON.parse remains in api/:"
if grep -rn "JSON\.parse" api/ --include=*.js | grep -v "^\S*: *//" | grep -v "used to be"; then
  echo "WARNING: a raw JSON.parse survived the patch (see above) -- inspect before trusting this build." >&2
fi

# node:4-alpine (2016) has no arm64 variant and predates multi-arch
# manifest lists -- already proven in rebuild_arm64_frontend.sh (2026-08-23),
# where the node 4 -> 18 jump was runtime-verified booting clean under QEMU.
echo "Swapping base image node:4-alpine -> node:18-alpine..."
sed -i 's/^FROM node:4-alpine/FROM node:18-alpine/' Dockerfile
head -1 Dockerfile

# yarn install times out under QEMU arm64 emulation at the default timeout
# (ESOCKETTIMEDOUT after ~30s x4 retries) -- emulation CPU/IO overhead, not
# a real network fault. 10 minutes absorbs it.
sed -i 's/RUN yarn install/RUN yarn install --network-timeout 600000/' Dockerfile
grep 'yarn install' Dockerfile

# Shared host-networked builder -- the buildx builder's own bridge network
# hits TLS handshake timeouts resolving Docker Hub metadata under WSL2
# (a known nested-container netns quirk), while the host's path works fine.
docker buildx rm wardence-fe-builder 2>/dev/null || true
docker buildx create --use --name wardence-fe-builder --driver-opt network=host

# Single manifest-list image covering both arches -- k3s on each host then
# pulls the right one automatically, so both clusters reference ONE tag.
echo "Building linux/amd64 + linux/arm64 -> ghcr.io/${GHCR_USER}/front-end:${WARDENCE_TAG} ..."
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t "ghcr.io/${GHCR_USER}/front-end:${WARDENCE_TAG}" \
  --push \
  .

echo ""
echo "Done. Pushed: ghcr.io/${GHCR_USER}/front-end:${WARDENCE_TAG} (amd64 + arm64)"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

cat <<EOF

NEXT (manual -- do NOT run automatically):
  1. Make the GHCR package public, or k3s cannot pull it without a secret.
  2. Smoke-test both arches boot:
       docker run --rm --platform linux/amd64 ghcr.io/${GHCR_USER}/front-end:${WARDENCE_TAG} node --version
       docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/front-end:${WARDENCE_TAG} node --version
  3. IMPORTANT -- update FRONT_END_IMAGE_BASELINE everywhere, or
     check_all_baselines.py's drift-check will silently REVERT this image
     (it did exactly that on 2026-08-24):
       - p2_readonly_loop/injector.py            (default constant)
       - p2_readonly_loop/check_all_baselines.py (second copy, standalone by design)
       - deploy/operator-api.service             (Environment= line, + the live unit)
       - ~/.bashrc on BOTH hosts
     Target value: ghcr.io/${GHCR_USER}/front-end:${WARDENCE_TAG}
  4. Roll it out on each host:
       kubectl set image deployment/front-end front-end=ghcr.io/${GHCR_USER}/front-end:${WARDENCE_TAG} -n sock-shop
       kubectl rollout status deployment/front-end -n sock-shop --timeout=300s
  5. Verify the fix for real: trigger oom, and confirm front-end's restart
     count does NOT increase while catalogue is down.
EOF
