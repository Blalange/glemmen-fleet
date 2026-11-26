#!/usr/bin/env bash
# Build and push the RAIDian RAID-management image.
#
# RAIDian is published to GHCR:
#   ghcr.io/blalange/raidian:latest
#
# The image bundles proprietary vendor CLIs; review their licenses before
# publishing it to a public registry.
#
# Usage:
#   docker login ghcr.io
#   ./scripts/raidian/build-image.sh
#   IMAGE=ghcr.io/blalange/raidian:2026-11-26 ./scripts/raidian/build-image.sh
#
# Requires Docker with buildx. The vendor binaries are linux/amd64 only.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

IMAGE="${IMAGE:-ghcr.io/blalange/raidian:latest}"
PLATFORM="${PLATFORM:-linux/amd64}"
DOCKERFILE="${DOCKERFILE:-${repo_root}/dockerfiles/raidian/Dockerfile}"
CONTEXT="${CONTEXT:-${repo_root}/dockerfiles/raidian}"

if [[ ! -f "${DOCKERFILE}" ]]; then
  echo "Dockerfile not found: ${DOCKERFILE}" >&2
  exit 1
fi

echo "Building RAIDian -> ${IMAGE} (${PLATFORM})"
echo "Dockerfile: ${DOCKERFILE}"

docker buildx build \
  --platform "${PLATFORM}" \
  --file "${DOCKERFILE}" \
  --tag "${IMAGE}" \
  --push \
  "${CONTEXT}"

echo "Pushed ${IMAGE}"
