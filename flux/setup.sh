#!/usr/bin/env bash
# Bootstrap Flux on the new Talos cluster and connect it to this repository.
#
# Prerequisites:
#   - the new cluster is up and KUBECONFIG points at it
#     (default: talos/generated/kubeconfig, created with ./talos/manage.sh kubeconfig)
#   - GITHUB_TOKEN is exported: a fine-grained PAT for Blalange/glemmen-fleet with
#       Contents: Read and write, Metadata: Read-only
#     (Administration: Read-only is additionally required when bootstrapping in
#      an organization). The token is stored in the cluster secret
#      flux-system/flux-system for the controllers. Prefer a repo-scoped token
#      and rotate it after the migration; a read/write deploy key works too.
#   - the repository already contains flux/clusters/glemmen (this tree).
#
# Usage:
#   export KUBECONFIG=talos/generated/kubeconfig
#   export GITHUB_TOKEN=github_pat_...
#   ./flux/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

FLUX_VERSION="${FLUX_VERSION:-v2.9.6}"
OWNER="${OWNER:-Blalange}"
REPOSITORY="${REPOSITORY:-glemmen-fleet}"
BRANCH="${BRANCH:-main}"
BOOTSTRAP_PATH="${BOOTSTRAP_PATH:-flux/clusters/glemmen}"
KUBECONFIG="${KUBECONFIG:-${REPO_ROOT}/talos/generated/kubeconfig}"
export KUBECONFIG

# --- flux CLI ----------------------------------------------------------------
FLUX="${FLUX:-}"
if [[ -z "${FLUX}" ]]; then
    if command -v flux >/dev/null 2>&1; then
        FLUX="$(command -v flux)"
    elif [[ -x "${SCRIPT_DIR}/bin/flux" ]]; then
        FLUX="${SCRIPT_DIR}/bin/flux"
    else
        mkdir -p "${SCRIPT_DIR}/bin"
        echo "==> Downloading flux ${FLUX_VERSION}"
        tmp="$(mktemp -d)"
        curl -fL -o "${tmp}/flux.tar.gz" \
            "https://github.com/fluxcd/flux2/releases/download/${FLUX_VERSION}/flux_${FLUX_VERSION#v}_linux_amd64.tar.gz"
        tar -xzf "${tmp}/flux.tar.gz" -C "${tmp}" flux
        mv "${tmp}/flux" "${SCRIPT_DIR}/bin/flux"
        rm -rf "${tmp}"
        FLUX="${SCRIPT_DIR}/bin/flux"
    fi
fi

: "${GITHUB_TOKEN:?export GITHUB_TOKEN=<fine-grained PAT for ${OWNER}/${REPOSITORY}>}"
command -v kubectl >/dev/null || { echo "kubectl is required in PATH" >&2; exit 1; }

echo "==> Checking cluster access"
kubectl get nodes >/dev/null

# Personal account or organization?
ACCOUNT_TYPE="$(curl -sf -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    "https://api.github.com/users/${OWNER}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["type"])' || echo Organization)"
PERSONAL_ARGS=()
if [[ "${ACCOUNT_TYPE}" == "User" ]]; then
    PERSONAL_ARGS+=(--personal)
fi
echo "==> GitHub account ${OWNER} is a ${ACCOUNT_TYPE}"

echo "==> Bootstrapping Flux (path ${BOOTSTRAP_PATH})"
"${FLUX}" bootstrap github \
    --owner="${OWNER}" \
    --repository="${REPOSITORY}" \
    --branch="${BRANCH}" \
    --path="${BOOTSTRAP_PATH}" \
    --token-auth \
    "${PERSONAL_ARGS[@]}"

echo "==> Waiting for the root Kustomization"
"${FLUX}" -n flux-system reconcile kustomization flux-system --with-source --timeout=5m || true

echo
echo "Flux is installed. The cluster tree under ${BOOTSTRAP_PATH} is now applied,"
echo "starting with flux/infrastructure. Use ./flux/manage.sh status to watch."
echo
echo "TIP: before restoring the Longhorn volumes/secrets, freeze the workloads:"
echo "  ./flux/manage.sh freeze        # suspend all Kustomizations"
echo "  ./flux/manage.sh status"
