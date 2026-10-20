#!/usr/bin/env bash
# Manage the Flux installation and the GitOps reconciliation.
#
# Usage: ./manage.sh <command> [args]
#
#   status                 Show all Flux resources
#   sync                   Fetch the latest revision from Git and reconcile
#   reconcile [name...]    Reconcile Kustomizations (default: everything, with source)
#   suspend <name...>      Suspend Kustomizations
#   resume <name...>       Resume Kustomizations
#   freeze                 Suspend everything (all Kustomizations)
#   unfreeze               Resume everything
#   apps-off               Suspend just the application Kustomizations
#   apps-on                Resume the application Kustomizations
#   logs [controller]      Flux controller logs (default: kustomize-controller)
#   tree [name]            Show the resource tree of a Kustomization
#   helm [name...]         Show HelmReleases / a specific one
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-${REPO_ROOT}/talos/generated/kubeconfig}"

APP_KUSTOMIZATIONS=(colibri energisjappa frigate homepage homepage-assets panel snipe-it unifi volunteer warrior)

FLUX="${FLUX:-}"
if [[ -z "${FLUX}" ]]; then
    if command -v flux >/dev/null 2>&1; then
        FLUX="$(command -v flux)"
    elif [[ -x "${SCRIPT_DIR}/bin/flux" ]]; then
        FLUX="${SCRIPT_DIR}/bin/flux"
    else
        echo "ERROR: flux CLI not found (run ./flux/setup.sh first)" >&2
        exit 1
    fi
fi

cmd="${1:-status}"
shift || true

case "${cmd}" in
    status)
        "${FLUX}" get sources
        echo
        "${FLUX}" get kustomizations
        echo
        "${FLUX}" get helmreleases -A
        ;;
    sync)
        "${FLUX}" reconcile source git flux-system -n flux-system
        "${FLUX}" reconcile kustomization flux-system -n flux-system --with-source
        ;;
    reconcile)
        if [[ $# -eq 0 ]]; then
            "${FLUX}" reconcile kustomization --all -n flux-system --with-source
        else
            for name in "$@"; do
                "${FLUX}" reconcile kustomization "${name}" -n flux-system --with-source
            done
        fi
        ;;
    suspend)
        : "${1:?usage: manage.sh suspend <name...>}"
        for name in "$@"; do
            "${FLUX}" suspend kustomization "${name}" -n flux-system
        done
        ;;
    resume)
        : "${1:?usage: manage.sh resume <name...>}"
        for name in "$@"; do
            "${FLUX}" resume kustomization "${name}" -n flux-system
        done
        ;;
    freeze)
        "${FLUX}" suspend kustomization --all -n flux-system
        ;;
    unfreeze)
        "${FLUX}" resume kustomization --all -n flux-system
        ;;
    apps-off)
        for name in "${APP_KUSTOMIZATIONS[@]}"; do
            "${FLUX}" suspend kustomization "${name}" -n flux-system 2>/dev/null || true
        done
        ;;
    apps-on)
        for name in "${APP_KUSTOMIZATIONS[@]}"; do
            "${FLUX}" resume kustomization "${name}" -n flux-system 2>/dev/null || true
        done
        ;;
    logs)
        controller="${1:-kustomize-controller}"
        kubectl -n flux-system logs "deploy/${controller}" --tail=200 -f
        ;;
    tree)
        name="${1:?usage: manage.sh tree <kustomization-name>}"
        "${FLUX}" tree kustomization "${name}" -n flux-system
        ;;
    helm)
        if [[ $# -eq 0 ]]; then
            "${FLUX}" get helmreleases -A
        else
            "${FLUX}" get helmrelease "$@" -A
        fi
        ;;
    *)
        sed -n 's/^#   /   /p' "$0"
        exit 1
        ;;
esac
