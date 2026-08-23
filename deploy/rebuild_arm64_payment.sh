#!/usr/bin/env bash
# arm64 rebuild -- payment (Go service #2 of 3, 2026-08-2x, Point 2 of the
# deployment checklist). Same pattern as catalogue's rebuild, same
# already-set-up host-networked buildx builder.
#
# Real source: https://github.com/microservices-demo/payment, tag 0.4.3 --
# confirmed live to exist, archived, and match this project's exact
# deployed image tag (weaveworksdemos/payment:0.4.3). Real Dockerfile at
# docker/payment/Dockerfile, confirmed via the live GitHub tree at this
# tag, not assumed.
#
# Real CGO check done BEFORE writing this script: the real upstream
# Dockerfile already builds with `CGO_ENABLED=0 GOOS=linux go build -a
# -installsuffix cgo ...` -- statically linked, genuinely CGO-free, same
# as catalogue. No CGO risk here.
#
# CORRECTED (per catalogue's real first-run finding): vendor/ is a gvt
# manifest only, not committed source -- real deps are fetched over the
# network at build time via `gvt restore`, same as catalogue. Fixed
# below by installing gvt and running the real restore step first.
#
# Same golang:1.7 multi-arch gap as catalogue -- same fix (modern
# multi-arch Go builder, GOARCH cross-compile, GO111MODULE=off since this
# repo also predates go.mod, dependencies vendored via gvt).

set -euo pipefail

GHCR_USER="aayush8392"
IMAGE_TAG="0.4.3"
SRC_REPO="https://github.com/microservices-demo/payment.git"
WORKDIR="$(mktemp -d)"

echo "Cloning ${SRC_REPO} @ tag ${IMAGE_TAG} into ${WORKDIR}..."
git clone --branch "${IMAGE_TAG}" --depth 1 "${SRC_REPO}" "${WORKDIR}/payment"

cd "${WORKDIR}/payment"

echo "Real upstream Dockerfile (docker/payment/Dockerfile) for reference:"
cat docker/payment/Dockerfile

echo "Writing a new, arm64-cross-compiling 2-stage Dockerfile..."
cat > Dockerfile.arm64 <<'EOF'
FROM --platform=$BUILDPLATFORM golang:1.21-alpine AS build
ENV GO111MODULE=off
ENV GOPATH=/go
ENV PATH=$GOPATH/bin:$PATH
RUN apk add --no-cache git
WORKDIR /go/src/github.com/microservices-demo/payment
COPY . .
RUN go get -u github.com/FiloSottile/gvt && gvt restore
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -a -installsuffix cgo \
    -o /app/main github.com/microservices-demo/payment/cmd/paymentsvc

FROM alpine:3.19
COPY --from=build /app/main /app/main
ENTRYPOINT ["/app/main", "-port=80"]
EXPOSE 80
EOF

echo "Building linux/arm64 image and pushing to ghcr.io/${GHCR_USER}/payment:${IMAGE_TAG}-arm64 ..."
docker buildx build \
  --builder wardence-arm64-builder \
  --platform linux/arm64 \
  -f Dockerfile.arm64 \
  -t "ghcr.io/${GHCR_USER}/payment:${IMAGE_TAG}-arm64" \
  --push \
  .

echo ""
echo "Done. Real pushed image: ghcr.io/${GHCR_USER}/payment:${IMAGE_TAG}-arm64"
echo "Cleaning up ${WORKDIR}..."
cd - >/dev/null
rm -rf "${WORKDIR}"

echo ""
echo "NEXT (manual smoke test):"
echo "  docker run --rm --platform linux/arm64 ghcr.io/${GHCR_USER}/payment:${IMAGE_TAG}-arm64 &"
echo "  (expect: service starts, logs a bind on :80, no crash/stack trace)"
