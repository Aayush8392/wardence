#!/usr/bin/env bash
# arm64 rebuild -- catalogue (Go service #1 of 3, 2026-08-2x, Point 2 of the
# deployment checklist). Same host-networked buildx builder set up during
# the front-end/Java rebuilds -- no repeat setup needed.
#
# Real source: https://github.com/microservices-demo/catalogue, tag 0.3.5 --
# confirmed live to exist, archived (read-only, fine for a one-time clone),
# and match this project's exact deployed image tag
# (weaveworksdemos/catalogue:0.3.5). Real Dockerfile lives at
# docker/catalogue/Dockerfile (NOT the repo root), confirmed via the live
# GitHub tree listing at this tag, not assumed.
#
# Real CGO check done BEFORE writing this script (per Kimi review 60's
# flagged, previously-unverified risk): the real upstream Dockerfile
# already builds with `CGO_ENABLED=0 GOOS=linux go build -a -installsuffix
# cgo ...` -- statically linked, genuinely CGO-free. No CGO risk here.
#
# CORRECTED (first real run of this script): vendor/ is NOT a committed,
# self-contained tree -- it's a gvt manifest only, populated by fetching
# real dependency source over the network at build time via `gvt
# restore` (confirmed by a real build failure: every import in
# cmd/cataloguesvc/main.go, transport.go, endpoints.go came back "cannot
# find package"). Same risk category as user's `glide install`, not the
# lower-risk case this file originally assumed -- fixed below by
# installing gvt and running the real upstream `gvt restore` step before
# building, matching the original Dockerfile's own real behavior.
#
# Real base-image problem: `golang:1.7` (2016, Debian-based) predates
# Docker's multi-arch manifest-list support -- same class of gap as
# front-end's node:4-alpine and the Java services' java:openjdk-8-alpine.
# Fix: modernize the BUILDER stage only (a real, current multi-arch Go
# image), cross-compiling GOARCH=arm64 at native host speed via
# --platform=$BUILDPLATFORM (same 2-stage pattern proven on the 4 Java
# services -- no QEMU needed for the build itself, only for the final
# smoke test). GO111MODULE=off is required: this repo predates go.mod
# entirely (dependencies are vendored via gvt into vendor/, no go.mod
# file exists) -- modern Go defaults to module mode and would otherwise
# refuse to build without one.
#
# Final stage: alpine:3.19 (real, current, confirmed multi-arch) --
# genuinely multi-arch, unlike golang:1.7's own final stage would be.
#
# Requires: docker with buindx + the wardence-arm64-builder created in the
# front-end rebuild session (network=host), and `docker login ghcr.io`
# already done with a GitHub PAT (write:packages scope).
#
# Usage: bash rebuild_arm64_catalogue.sh

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.3.5"
SRC_REPO="https://github.com/microservices-demo/catalogue.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/catalogue"

cd "${WORKDIR}/catalogue"

echo "Real upstream Dockerfile (docker/catalogue/Dockerfile) for reference:"
cat docker/catalogue/Dockerfile

echo "Writing a new, arm64-cross-compiling 2-stage Dockerfile (replaces the ancient golang:1.7 single-stage build)..."
cat > Dockerfile.arm64 <<'EOF'
FROM --platform=$BUILDPLATFORM golang:1.21-alpine AS build
ENV GO111MODULE=off
ENV GOPATH=/go
ENV PATH=$GOPATH/bin:$PATH
RUN apk add --no-cache git
WORKDIR /go/src/github.com/microservices-demo/catalogue
COPY . .
RUN go get -u github.com/FiloSottile/gvt && gvt restore
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -a -installsuffix cgo \
    -o /app/main github.com/microservices-demo/catalogue/cmd/cataloguesvc

FROM alpine:3.19
COPY --from=build /app/main /app/main
COPY --from=build /go/src/github.com/microservices-demo/catalogue/images/ /images/
ENTRYPOINT ["/app/main", "-port=80"]
EXPOSE 80
EOF

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/catalogue:${IMAGE_TAG}-arm64 ..."
docker buildx build \
  --builder wardence-arm64-builder \
  --platform linux/arm64 \
  -f Dockerfile.arm64 \
  -t "ghcr.io/${GHCR_USER}/catalogue:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/catalogue:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual smoke test, matching the Java services' pattern):"
echo "  docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/catalogue:${IMAGE_TAG}-arm64 &"
echo "  (expect: service starts, logs a bind on :80; catalogue-db DNS failure standalone is expected, resolves in-cluster)"
