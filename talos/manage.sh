#!/usr/bin/env bash
# Manage the Talos cluster.
#
# Usage: ./manage.sh <command> [options]
#
#   status                     Version + cluster health
#   apply <node> [address]     Apply the generated config to a node booted from the ISO
#                              (maintenance mode, --insecure). <node> is a hostname or IP;
#                              [address] overrides the node's configured IP (e.g. the
#                              DHCP address the node got in maintenance mode).
#   apply-all                  Apply configs to all nodes in TALOS_NODES
#   bootstrap                  Bootstrap etcd on the first node
#   kubeconfig [path]          Write the admin kubeconfig (default talos/generated/kubeconfig)
#   health                     Run talosctl health
#   services <node>            List Talos system services on a node
#   links <node>               List network links (names, MACs) on a node
#   disks <node>               List disks (for disk selectors)
#   roles                      Label nodes: glemmen1xx control-plane+etcd+worker, others worker
#   patch <node> <file>        Apply a machine config patch to a running node
#   netbird                    Re-render and apply the Netbird config to all nodes
#   vip on|off <nodes...>      Add/remove the Layer2 VIP config (192.168.6.98)
#   wipe <node> <disk>         Wipe a disk (e.g. /dev/sdb), removing old partitions
#   reset <node>               Reset a node (graceful shutdown + wipe of the boot disk)
#   upgrade-k8s <version>      Upgrade Kubernetes (e.g. 1.36.5)
#   upgrade-talos [version]    Upgrade Talos (default: TALOS_VERSION)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=settings.env
source "${SCRIPT_DIR}/settings.env"

GENERATED="${SCRIPT_DIR}/generated"
SECRETS="${SCRIPT_DIR}/secrets"
export TALOSCONFIG="${GENERATED}/talosconfig"

TALOSCTL="${TALOSCTL:-}"
if [[ -z "${TALOSCTL}" ]]; then
    if command -v talosctl >/dev/null 2>&1; then
        TALOSCTL="$(command -v talosctl)"
    elif [[ -x "${SCRIPT_DIR}/bin/talosctl" ]]; then
        TALOSCTL="${SCRIPT_DIR}/bin/talosctl"
    else
        echo "ERROR: talosctl not found (run ./gen-config.sh first)" >&2
        exit 1
    fi
fi

node_ip() {
    local name="$1" entry
    for entry in ${TALOS_NODES}; do
        if [[ "${entry%%=*}" == "${name}" || "${entry##*=}" == "${name}" ]]; then
            echo "${entry##*=}"
            return 0
        fi
    done
    # not a known node name: assume it is an IP/hostname
    echo "${name}"
}

require_config() {
    local node="$1"
    if [[ ! -f "${GENERATED}/${node}.yaml" ]]; then
        echo "ERROR: ${GENERATED}/${node}.yaml not found (run ./gen-config.sh)" >&2
        exit 1
    fi
}

cmd="${1:-}"
shift || true

case "${cmd}" in
    status)
        "${TALOSCTL}" version
        echo
        "${TALOSCTL}" -n "${BOOTSTRAP_NODE##*=}" get members 2>/dev/null || true
        "${TALOSCTL}" -n "${BOOTSTRAP_NODE##*=}" get machinestatus 2>/dev/null || true
        ;;
    apply)
        node="${1:?usage: manage.sh apply <node> [address]}"
        require_config "${node}"
        ip="${2:-$(node_ip "${node}")}"
        echo "==> Applying ${GENERATED}/${node}.yaml to ${ip} (maintenance mode)"
        "${TALOSCTL}" apply-config --insecure --nodes "${ip}" --file "${GENERATED}/${node}.yaml"
        ;;
    apply-all)
        for entry in ${TALOS_NODES}; do
            name="${entry%%=*}"
            require_config "${name}"
            echo "==> Applying ${GENERATED}/${name}.yaml to ${entry##*=}"
            "${TALOSCTL}" apply-config --insecure --nodes "${entry##*=}" --file "${GENERATED}/${name}.yaml"
        done
        ;;
    bootstrap)
        echo "==> Bootstrapping etcd on ${BOOTSTRAP_NODE%%=*}"
        "${TALOSCTL}" bootstrap --nodes "$(node_ip "${BOOTSTRAP_NODE%%=*}")"
        ;;
    kubeconfig)
        out="${1:-${GENERATED}/kubeconfig}"
        "${TALOSCTL}" kubeconfig "${out}" --merge=false
        echo "==> Wrote ${out}"
        ;;
    health)
        "${TALOSCTL}" health --server=false
        ;;
    services)
        node="${1:?usage: manage.sh services <node>}"
        "${TALOSCTL}" -n "$(node_ip "${node}")" service
        ;;
    links)
        node="${1:?usage: manage.sh links <node>}"
        "${TALOSCTL}" -n "$(node_ip "${node}")" get links
        ;;
    roles)
        # Label the nodes following the role convention: the glemmen1xx
        # machines carry all roles (control plane + etcd + worker, i.e. they
        # are fully schedulable), every other node is a worker. Talos cannot
        # set the node-role.kubernetes.io/* labels itself (the kubelet identity
        # is restricted by NodeRestriction), so they are applied here
        # (idempotent).
        KUBECTL="${KUBECTL:-}"
        if [[ -z "${KUBECTL}" ]]; then
            if [[ -x "${SCRIPT_DIR}/../scripts/bin/kubectl" ]]; then
                KUBECTL="${SCRIPT_DIR}/../scripts/bin/kubectl"
            else
                KUBECTL="$(command -v kubectl || true)"
            fi
        fi
        if [[ -z "${KUBECTL}" ]]; then
            echo "ERROR: kubectl not found (set KUBECTL)" >&2
            exit 1
        fi
        export KUBECONFIG="${KUBECONFIG:-${GENERATED}/kubeconfig}"
        cp_nodes=()
        worker_nodes=()
        for entry in ${TALOS_NODES}; do
            name="${entry%%=*}"
            case "${name}" in
                glemmen1[0-9][0-9]) cp_nodes+=("${name}") ;;
                *) worker_nodes+=("${name}") ;;
            esac
        done
        # Nodes that have not joined yet are skipped (e.g. glemmen60 before its
        # first install).
        existing="$("${KUBECTL}" get nodes -o name | sed 's|^node/||')"
        has_node() { grep -qx "$1" <<<"${existing}"; }
        cp_present=()
        for name in "${cp_nodes[@]}"; do
            has_node "${name}" && cp_present+=("${name}") || echo "==> ${name}: not in the cluster yet, skipping"
        done
        echo "==> control-plane nodes (all roles): ${cp_present[*]}"
        if [[ ${#cp_present[@]} -gt 0 ]]; then
            "${KUBECTL}" label node "${cp_present[@]}" \
                node-role.kubernetes.io/worker= node-role.kubernetes.io/etcd= --overwrite
        fi
        echo "==> worker nodes (worker role only)"
        for name in "${worker_nodes[@]}"; do
            if ! has_node "${name}"; then
                echo "    ${name}: not in the cluster yet, skipping"
                continue
            fi
            "${KUBECTL}" label node "${name}" node-role.kubernetes.io/worker= --overwrite
            # make sure stray roles never linger on the workers
            "${KUBECTL}" label node "${name}" \
                node-role.kubernetes.io/etcd- node-role.kubernetes.io/control-plane- >/dev/null 2>&1 || true
        done
        ;;
    disks)
        node="${1:?usage: manage.sh disks <node>}"
        "${TALOSCTL}" -n "$(node_ip "${node}")" get disks
        ;;
    patch)
        node="${1:?usage: manage.sh patch <node> <patchfile>}"
        file="${2:?usage: manage.sh patch <node> <patchfile>}"
        "${TALOSCTL}" -n "$(node_ip "${node}")" patch mc --patch @"${file}"
        ;;
    netbird)
        NB_SETUP_KEY="$(grep -E '^NB_SETUP_KEY=' "${SECRETS}/netbird.env" | cut -d= -f2-)"
        tmp="$(mktemp)"
        sed "s|@NB_SETUP_KEY@|${NB_SETUP_KEY}|" "${SCRIPT_DIR}/patches/netbird.yaml.tpl" > "${tmp}"
        for entry in ${TALOS_NODES}; do
            echo "==> Netbird config -> ${entry%%=*}"
            "${TALOSCTL}" -n "${entry##*=}" patch mc --patch @"${tmp}"
        done
        rm -f "${tmp}"
        ;;
    vip)
        action="${1:?usage: manage.sh vip on|off <nodes...>}"
        shift || true
        nodes=("$@")
        if [[ ${#nodes[@]} -eq 0 ]]; then
            nodes=(glemmen130 glemmen150 glemmen160)
        fi
        tmp="$(mktemp)"
        if [[ "${action}" == "on" ]]; then
            cat > "${tmp}" <<EOF
apiVersion: v1alpha1
kind: Layer2VIPConfig
name: ${CLUSTER_VIP}
link: bond0
EOF
        else
            cat > "${tmp}" <<EOF
apiVersion: v1alpha1
kind: Layer2VIPConfig
name: ${CLUSTER_VIP}
\$patch: delete
EOF
        fi
        for node in "${nodes[@]}"; do
            echo "==> VIP ${action}: ${node}"
            "${TALOSCTL}" -n "$(node_ip "${node}")" patch mc --patch @"${tmp}"
        done
        rm -f "${tmp}"
        ;;
    wipe)
        node="${1:?usage: manage.sh wipe <node> <disk e.g. /dev/sdb>}"
        disk="${2:?usage: manage.sh wipe <node> <disk e.g. /dev/sdb>}"
        "${TALOSCTL}" -n "$(node_ip "${node}")" wipe disk "${disk}" --drop-partition
        ;;
    reset)
        node="${1:?usage: manage.sh reset <node>}"
        "${TALOSCTL}" reset --nodes "$(node_ip "${node}")" --graceful
        ;;
    upgrade-k8s)
        version="${1:?usage: manage.sh upgrade-k8s <version e.g. 1.36.5>}"
        "${TALOSCTL}" upgrade-k8s --to "${version}"
        ;;
    upgrade-talos)
        version="${1:-${TALOS_VERSION}}"
        image="factory.talos.dev/metal-installer/$(cat "${GENERATED}/schematic-id"):${version}"
        for entry in ${TALOS_NODES}; do
            echo "==> Upgrading ${entry%%=*} to ${version}"
            "${TALOSCTL}" upgrade --image "${image}" --nodes "${entry##*=}"
            # wait for the node to come back before touching the next one
            "${TALOSCTL}" -n "${entry##*=}" health --server=false --wait-timeout=10m
        done
        ;;
    *)
        sed -n 's/^#   /   /p' "$0"
        exit 1
        ;;
esac
