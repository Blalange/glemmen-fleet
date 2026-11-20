#!/usr/bin/env bash
# Build and push the ARTEX image from the pinned upstream release.
#
# ARTEX does not publish a prebuilt image: the upstream Dockerfile builds the
# Go binary with the embedded Next.js UI and installs the scanner toolchain.
# The cluster therefore pulls an image built here from unmodified upstream
# source. No local patches are applied.
#
# The Dockerfile is vendored in dockerfiles/artex/Dockerfile; the build context
# remains the cloned upstream checkout because the Dockerfile copies web/,
# go.mod, start.sh and skills/ from it.
#
# Usage:
#   ./scripts/artex/build-image.sh
#   ARTEX_VERSION=v0.6.0 IMAGE=ghcr.io/blalange/artex:v0.6.0 \
#     ./scripts/artex/build-image.sh
#
# Requires Docker with buildx and a logged-in push account:
#   docker login ghcr.io
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

ARTEX_VERSION="${ARTEX_VERSION:-v0.6.0}"
IMAGE="${IMAGE:-ghcr.io/blalange/artex:${ARTEX_VERSION}}"
ARTEX_REPO="${ARTEX_REPO:-https://github.com/BaaRai/ARTEX.git}"
DOCKERFILE="${DOCKERFILE:-${repo_root}/dockerfiles/artex/Dockerfile}"
PLATFORM="${PLATFORM:-linux/amd64}"

if [[ ! -f "${DOCKERFILE}" ]]; then
  echo "Dockerfile not found: ${DOCKERFILE}" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

git clone --quiet --depth 1 --branch "${ARTEX_VERSION}" "${ARTEX_REPO}" "${tmp}"
commit="$(git -C "${tmp}" rev-parse --short HEAD)"

echo "Building ARTEX ${ARTEX_VERSION} (${commit}) -> ${IMAGE} (${PLATFORM})"
echo "Dockerfile: ${DOCKERFILE}"

docker buildx build \
  --platform "${PLATFORM}" \
  --file "${DOCKERFILE}" \
  --build-arg "ARTEX_VERSION=${ARTEX_VERSION}" \
  --tag "${IMAGE}" \
  --push \
  "${tmp}"

echo "Pushed ${IMAGE}"
