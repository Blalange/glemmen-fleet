#!/usr/bin/env bash
# Build and push the ARTEX image from the pinned upstream release.
#
# ARTEX does not publish a prebuilt image: the upstream Dockerfile builds the
# Go binary with the embedded Next.js UI and installs the scanner toolchain.
# The cluster therefore pulls an image built here from unmodified upstream
# source. No local patches are applied.
#
# Usage:
#   ./scripts/artex/build-image.sh
#   ARTEX_VERSION=v0.6.0 IMAGE=ghcr.io/blalange/artex:v0.6.0 \
#     ./scripts/artex/build-image.sh
#
# Requires Docker with buildx and a logged-in push account:
#   docker login ghcr.io
set -euo pipefail

ARTEX_VERSION="${ARTEX_VERSION:-v0.6.0}"
IMAGE="${IMAGE:-ghcr.io/blalange/artex:${ARTEX_VERSION}}"
ARTEX_REPO="${ARTEX_REPO:-https://github.com/BaaRai/ARTEX.git}"
PLATFORM="${PLATFORM:-linux/amd64}"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

git clone --quiet --depth 1 --branch "${ARTEX_VERSION}" "${ARTEX_REPO}" "${tmp}"
commit="$(git -C "${tmp}" rev-parse --short HEAD)"

echo "Building ARTEX ${ARTEX_VERSION} (${commit}) -> ${IMAGE} (${PLATFORM})"

docker buildx build \
  --platform "${PLATFORM}" \
  --build-arg "ARTEX_VERSION=${ARTEX_VERSION}" \
  --tag "${IMAGE}" \
  --push \
  "${tmp}"

echo "Pushed ${IMAGE}"
