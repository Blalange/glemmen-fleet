#!/usr/bin/env bash
# Build the Talos Factory schematic from schematic.yaml and print/download the
# boot assets for the cluster.
#
# Usage:
#   ./gen-iso.sh              # create schematic, print ISO + installer URLs
#   ./gen-iso.sh --download   # ... and download metal-amd64.iso
#
# The schematic ID is cached in talos/generated/schematic-id.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=settings.env
source "${SCRIPT_DIR}/settings.env"

FACTORY="https://factory.talos.dev"
GENERATED_DIR="${SCRIPT_DIR}/generated"
SCHEMATIC_FILE="${SCRIPT_DIR}/schematic.yaml"
SCHEMATIC_ID_FILE="${GENERATED_DIR}/schematic-id"

mkdir -p "${GENERATED_DIR}"

command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import yaml' 2>/dev/null || { echo "python3-yaml (PyYAML) is required to convert schematic.yaml to JSON" >&2; exit 1; }

echo "==> Creating schematic from ${SCHEMATIC_FILE}"
SCHEMATIC_JSON="$(python3 -c 'import json,sys,yaml; json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)' "${SCHEMATIC_FILE}")"
SCHEMATIC_ID="$(curl -sfX POST "${FACTORY}/schematics" \
    -H 'Content-Type: application/json' \
    --data "${SCHEMATIC_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

echo "${SCHEMATIC_ID}" > "${SCHEMATIC_ID_FILE}"
echo "    schematic ID: ${SCHEMATIC_ID}"

ISO_URL="${FACTORY}/image/${SCHEMATIC_ID}/${TALOS_VERSION}/metal-amd64.iso"
RAW_URL="${FACTORY}/image/${SCHEMATIC_ID}/${TALOS_VERSION}/metal-amd64.raw.xz"
INSTALLER_IMAGE="factory.talos.dev/metal-installer/${SCHEMATIC_ID}:${TALOS_VERSION}"

echo
echo "Talos        : ${TALOS_VERSION}"
echo "Kubernetes   : ${KUBERNETES_VERSION}"
echo "ISO          : ${ISO_URL}"
echo "Disk image   : ${RAW_URL}"
echo "Installer    : ${INSTALLER_IMAGE}"
echo "Kernel args  : bond=... ip=... (see README.md#maintenance-mode-network)"

if [[ "${1:-}" == "--download" ]]; then
    mkdir -p "${GENERATED_DIR}/iso"
    echo
    echo "==> Downloading ISO to ${GENERATED_DIR}/iso/metal-amd64.iso"
    curl -fL --progress-bar -o "${GENERATED_DIR}/iso/metal-amd64.iso" "${ISO_URL}"
    echo "==> Done. Flash it to a USB stick or mount it as virtual media."
fi
