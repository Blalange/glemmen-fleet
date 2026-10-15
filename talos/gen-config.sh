#!/usr/bin/env bash
# Generate the Talos machine configs for every node.
#
# Steps:
#   1. render the Netbird extension config from talos/secrets/netbird.env
#   2. create the cluster secrets bundle (talos/generated/secrets.yaml) if missing
#   3. generate the base control-plane config (talos/generated/base/) with the common patch
#   4. merge each talos/nodes/<host>.yaml on top and validate the result
#
# Everything lands in talos/generated/ (gitignored). The secrets bundle is the
# cluster PKI: back it up, losing it means losing talosctl access.
#
# Usage: ./gen-config.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=settings.env
source "${SCRIPT_DIR}/settings.env"

GENERATED="${SCRIPT_DIR}/generated"
PATCHES="${SCRIPT_DIR}/patches"
NODES_DIR="${SCRIPT_DIR}/nodes"
SECRETS="${SCRIPT_DIR}/secrets"

# --- locate talosctl ---------------------------------------------------------
TALOSCTL="${TALOSCTL:-}"
if [[ -z "${TALOSCTL}" ]]; then
    if command -v talosctl >/dev/null 2>&1; then
        TALOSCTL="$(command -v talosctl)"
    elif [[ -x "${SCRIPT_DIR}/bin/talosctl" ]]; then
        TALOSCTL="${SCRIPT_DIR}/bin/talosctl"
    else
        mkdir -p "${SCRIPT_DIR}/bin"
        echo "==> Downloading talosctl ${TALOS_VERSION}"
        curl -fL -o "${SCRIPT_DIR}/bin/talosctl" \
            "https://github.com/siderolabs/talos/releases/download/${TALOS_VERSION}/talosctl-linux-amd64"
        chmod +x "${SCRIPT_DIR}/bin/talosctl"
        TALOSCTL="${SCRIPT_DIR}/bin/talosctl"
    fi
fi
echo "==> Using talosctl: ${TALOSCTL} ($("${TALOSCTL}" version --client --short 2>/dev/null || true))"

mkdir -p "${GENERATED}/patches"

# --- schematic -----------------------------------------------------------------
if [[ ! -f "${GENERATED}/schematic-id" ]]; then
    echo "==> No schematic ID yet, running gen-iso.sh"
    "${SCRIPT_DIR}/gen-iso.sh" >/dev/null
fi
SCHEMATIC_ID="$(cat "${GENERATED}/schematic-id")"
INSTALLER_IMAGE="factory.talos.dev/metal-installer/${SCHEMATIC_ID}:${TALOS_VERSION}"
echo "==> Schematic: ${SCHEMATIC_ID}"

# --- netbird config ------------------------------------------------------------
if [[ ! -f "${SECRETS}/netbird.env" ]]; then
    echo "ERROR: ${SECRETS}/netbird.env is missing (see talos/secrets/README.md)" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "${SECRETS}/netbird.env"
if [[ -z "${NB_SETUP_KEY:-}" ]]; then
    echo "ERROR: NB_SETUP_KEY is empty in ${SECRETS}/netbird.env" >&2
    exit 1
fi
sed "s|@NB_SETUP_KEY@|${NB_SETUP_KEY}|" "${PATCHES}/netbird.yaml.tpl" > "${GENERATED}/patches/netbird.yaml"
echo "==> Rendered ${GENERATED}/patches/netbird.yaml"

# --- unattended install --------------------------------------------------------
# Rendered here because it embeds the schematic-specific installer image, and
# because `provisioning` is replaced (not merged) when patched.
cat > "${GENERATED}/patches/install.yaml" <<EOF
apiVersion: v1alpha1
kind: UnattendedInstallConfig
reboot: true
installer:
    image: ${INSTALLER_IMAGE}
provisioning:
    diskSelector:
        match: '${INSTALL_DISK_SELECTOR}'
    wipe: true
EOF
echo "==> Rendered ${GENERATED}/patches/install.yaml"

# --- secrets bundle ------------------------------------------------------------
if [[ ! -f "${GENERATED}/secrets.yaml" ]]; then
    echo "==> Creating new cluster secrets bundle (PKI)"
    "${TALOSCTL}" gen secrets -o "${GENERATED}/secrets.yaml"
else
    echo "==> Reusing existing secrets bundle ${GENERATED}/secrets.yaml"
fi

# --- base config ---------------------------------------------------------------
echo "==> Generating base config"
rm -rf "${GENERATED}/base"
"${TALOSCTL}" gen config "${CLUSTER_NAME}" "${CLUSTER_ENDPOINT}" \
    --with-secrets "${GENERATED}/secrets.yaml" \
    --kubernetes-version "${KUBERNETES_VERSION}" \
    --install-image "${INSTALLER_IMAGE}" \
    --install-disk /dev/sda \
    --output-dir "${GENERATED}/base" \
    --config-patch @"${PATCHES}/common.yaml" \
    --config-patch @"${GENERATED}/patches/install.yaml" \
    --config-patch @"${GENERATED}/patches/netbird.yaml" \
    --with-docs=false \
    --with-examples=false

# --- per-node configs -----------------------------------------------------------
echo "==> Merging node configs"
for node_file in "${NODES_DIR}"/*.yaml; do
    node="$(basename "${node_file}" .yaml)"
    "${TALOSCTL}" machineconfig patch "${GENERATED}/base/controlplane.yaml" \
        --patch @"${node_file}" \
        -o "${GENERATED}/${node}.yaml"
    "${TALOSCTL}" validate --config "${GENERATED}/${node}.yaml" --mode metal >/dev/null
    echo "    ${node}.yaml OK"
done

# --- talosconfig ----------------------------------------------------------------
cp "${GENERATED}/base/talosconfig" "${GENERATED}/talosconfig"
ENDPOINTS=""
NODE_IPS=""
for entry in ${TALOS_NODES}; do
    NODE_IPS="${NODE_IPS} ${entry##*=}"
done
# shellcheck disable=SC2086
"${TALOSCTL}" --talosconfig "${GENERATED}/talosconfig" config endpoints ${NODE_IPS} >/dev/null
FIRST_IP="${BOOTSTRAP_NODE##*=}"
"${TALOSCTL}" --talosconfig "${GENERATED}/talosconfig" config nodes "${FIRST_IP}" >/dev/null

echo
echo "Done. Configs in ${GENERATED}/:"
ls -1 "${GENERATED}"/*.yaml | sed 's|.*/|    |'
echo
echo "Next: boot the ISO and run ./manage.sh apply <node>"
