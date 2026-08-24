#!/usr/bin/env bash
# arm64 rebuild -- user (Go service #3 of 3, 2026-08-2x, Point 2 of the
# deployment checklist). Real, structural difference from catalogue/payment,
# flagged so it isn't silently treated as the same case:
#
# - catalogue/payment vendor their dependencies into a committed vendor/
#   directory (gvt) -- fully offline, self-contained builds.
# - user uses glide instead, and its vendor/ directory is NOT committed
#   (confirmed via a live GitHub tree check on this exact tag -- no
#   vendor/ present at the repo root). The real upstream build fetches
#   dependencies over the network at build time via `glide install`.
#   This means this rebuild depends on those 2016-era dependency repos
#   (github.com/Masterminds/glide itself, plus whatever user's glide.yaml
#   pins -- mgo, gorilla/mux-class packages) still being reachable and
#   API-compatible today. Real, higher risk than catalogue/payment --
#   confirm the smoke test at the end actually passes before trusting
#   this image, more so than for the other 6.
#
# Real source: https://github.com/microservices-demo/user, tag 0.4.7 --
# confirmed live to exist, archived, matches the deployed image tag
# (weaveworksdemos/user:0.4.7). Real Dockerfile at the repo ROOT (unlike
# catalogue/payment's docker/<svc>/Dockerfile layout), confirmed via the
# live GitHub tree at this tag.
#
# Real CGO check done BEFORE writing this script: the real upstream
# Dockerfile never sets CGO_ENABLED explicitly, unlike catalogue/payment --
# but user's only DB driver (mgo, a pure-Go MongoDB driver) needs no CGO,
# and golang:1.7-alpine (musl, no gcc installed by the original Dockerfile)
# has no C compiler available to it either way, so the existing amd64
# image is already implicitly CGO-free. CGO_ENABLED=0 is set explicitly
# below anyway, to remove any ambiguity for the cross-compile.
#
# Same golang:1.7-alpine multi-arch gap as the others -- same fix
# direction (modern multi-arch Go builder), but GO111MODULE=off's legacy
# `go get` codepath (needed to install glide itself, matching the real
# upstream build step) is less certain to still work cleanly under a
# modern Go toolchain than catalogue/payment's simpler vendor-only build.

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.4.7"
SRC_REPO="https://github.com/microservices-demo/user.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/user"

cd "${WORKDIR}/user"

# Real fix (2026-08-24): the upstream Href struct's field is an
# anonymous, unexported "string" -- confirmed via a live WSL2-vs-Oracle
# comparison that the arm64 rebuild (golang:1.21) always serializes
# _links as empty {} while WSL2's original image (built with the
# original old Go toolchain) does not. Renaming to an exported field
# name fixes this deterministically on any Go version -- see
# wardence_buildlog.md's network-latency validation session for the
# full investigation.
echo "Patching users/links.go: Href.string -> Href.Href (exported field)..."
python3 - <<'INNER'
import re
with open("users/links.go") as f:
    src = f.read()
new_src = src.replace(
    'type Href struct {\n\tstring `json:"href"`\n}',
    'type Href struct {\n\tHref string `json:"href"`\n}'
)
if new_src == src:
    raise SystemExit("PATCH FAILED: exact string not found in links.go -- inspect file manually before continuing")
with open("users/links.go", "w") as f:
    f.write(new_src)
print("Patched users/links.go successfully.")
INNER

echo "Real upstream Dockerfile (repo root) for reference:"
cat Dockerfile

echo "Writing a new, arm64-cross-compiling 2-stage Dockerfile..."
cat > Dockerfile.arm64 <<'EOF'
FROM --platform=$BUILDPLATFORM golang:1.21-alpine AS build
ENV GO111MODULE=off
ENV GOPATH=/go
ENV PATH=$GOPATH/bin:$PATH
RUN apk add --no-cache git
WORKDIR /go/src/github.com/microservices-demo/user
COPY . .
RUN go get -v github.com/Masterminds/glide && glide install
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -o /app/user .

FROM alpine:3.19
ENV MONGO_HOST=mytestdb:27017
ENV HATEAOS=user
ENV USER_DATABASE=mongodb
COPY --from=build /app/user /app/user
ENTRYPOINT ["/app/user", "-port=80"]
EXPOSE 80
EOF

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/user:${IMAGE_TAG}-arm64 ..."
docker buildx build \
  --builder wardence-arm64-builder \
  --platform linux/arm64 \
  -f Dockerfile.arm64 \
  -t "ghcr.io/${GHCR_USER}/user:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/user:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual smoke test -- pay extra attention here per the risk note above):"
echo "  docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/user:${IMAGE_TAG}-arm64 &"
echo "  (expect: service starts, logs a bind on :8084; a standalone mongo DNS failure is expected, resolves in-cluster)"
echo "  If the glide install step fails (network/dependency-repo issues), report the exact error --"
echo "  do not silently retry or work around it without checking what broke first."
