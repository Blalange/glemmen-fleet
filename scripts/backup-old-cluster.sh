#!/usr/bin/env bash
# Backup the old K3s cluster to glemmen60 before the Talos migration.
#
# Run this from the workstation. It uses SSH to reach the old nodes (sshpass with
# the hostname as password by default, or your SSH keys if SSH_PASSWORD is empty).
#
# What it does:
#   1. fetches the k3s admin kubeconfig from glemmen130
#   2. switches the Longhorn backup target to the NFS export on glemmen60
#   3. dumps Kubernetes resources + secrets, PVs, Longhorn volume metadata
#   4. attaches every Longhorn volume and creates a full backup of it
#   5. verifies all backups and writes a manifest
#   6. copies the dumps and the k3s etcd snapshots to glemmen60
#
# Safe to re-run: existing dumps are overwritten, volumes that already have a
# completed backup from this run are skipped.
#
# Prerequisites:
#   - the NFS server on glemmen60 (see the migration plan, step 3):
#       sudo apt install nfs-kernel-server
#       sudo mkdir -p /mnt/hdd1/longhorn-backups /mnt/hdd1/migration-backup
#       echo '/mnt/hdd1 192.168.6.0/24(rw,sync,no_subtree_check,no_root_squash)' | sudo tee /etc/exports
#       sudo exportfs -ra && sudo systemctl enable --now nfs-server
#   - Longhorn is healthy and the workload pods are scaled to 0 (step 1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${SCRIPT_DIR}/migration-backup}"

NFS_SERVER="${NFS_SERVER:-192.168.6.60}"
NFS_SERVER_HOST="${NFS_SERVER_HOST:-glemmen60.blalange.intra}"
NFS_EXPORT="${NFS_EXPORT:-/mnt/hdd1}"
LH_BACKUP_PATH="${LH_BACKUP_PATH:-${NFS_EXPORT}/longhorn-backups}"

SSH_USER="${SSH_USER:-user}"
SSH_PASSWORD="${SSH_PASSWORD-}"   # unset -> per-node password (= hostname); set -> used for all nodes
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)

KUBECTL="${KUBECTL:-}"
K3S_NODE="${K3S_NODE:-glemmen130.blalange.intra}"

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- ssh helpers --------------------------------------------------------------
# With sshpass available, connect with the given/default password; otherwise
# fall back to SSH key auth.
host_pass() { # host_pass <host>
    if [[ -n "${SSH_PASSWORD}" ]]; then
        echo "${SSH_PASSWORD}"
    else
        echo "${1%%.*}"
    fi
}
ssh_cmd() { # ssh_cmd <host> <command...>
    local host="$1"; shift
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "$(host_pass "${host}")" ssh "${SSH_OPTS[@]}" "${SSH_USER}@${host}" "$@"
    else
        ssh "${SSH_OPTS[@]}" "${SSH_USER}@${host}" "$@"
    fi
}
ssh_sudo() { # ssh_sudo <host> <command...> (sudo with the node's password)
    local host="$1"; shift
    ssh_cmd "${host}" "echo '$(host_pass "${host}")' | sudo -S bash -c \"$*\" 2>/dev/null"
}
scp_to() { # scp_to <host> <remote-dir> <local...>
    local host="$1"; shift
    local remote="$1"; shift
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "$(host_pass "${host}")" scp "${SSH_OPTS[@]}" -r "$@" "${SSH_USER}@${host}:${remote}"
    else
        scp "${SSH_OPTS[@]}" -r "$@" "${SSH_USER}@${host}:${remote}"
    fi
}

# --- kubectl -------------------------------------------------------------------
if [[ -z "${KUBECTL}" ]]; then
    if command -v kubectl >/dev/null 2>&1; then
        KUBECTL="$(command -v kubectl)"
    elif [[ -x "${SCRIPT_DIR}/bin/kubectl" ]]; then
        KUBECTL="${SCRIPT_DIR}/bin/kubectl"
    else
        mkdir -p "${SCRIPT_DIR}/bin"
        log "Downloading kubectl v1.36.5"
        curl -fL -o "${SCRIPT_DIR}/bin/kubectl" "https://dl.k8s.io/release/v1.36.5/bin/linux/amd64/kubectl"
        chmod +x "${SCRIPT_DIR}/bin/kubectl"
        KUBECTL="${SCRIPT_DIR}/bin/kubectl"
    fi
fi

KUBECONFIG_FILE="${BACKUP_DIR}/k3s.yaml"
mkdir -p "${BACKUP_DIR}"/{resources,secrets,longhorn/volumes,etcd,node-configs}
export KUBECTL

# 1. kubeconfig -----------------------------------------------------------------
if [[ ! -f "${KUBECONFIG_FILE}" ]]; then
    log "Fetching k3s kubeconfig from ${K3S_NODE}"
    ssh_sudo "${K3S_NODE}" "cat /etc/rancher/k3s/k3s.yaml" > "${KUBECONFIG_FILE}.tmp"
    [[ -s "${KUBECONFIG_FILE}.tmp" ]] || die "could not read /etc/rancher/k3s/k3s.yaml"
    # Point the server at the node and keep TLS verification working: the API
    # certificate only contains the bare hostnames (no .blalange.intra, no IPs),
    # so tell kubectl which name to validate against.
    python3 - "${KUBECONFIG_FILE}.tmp" "${KUBECONFIG_FILE}" "${K3S_NODE}" <<'PYEOF'
import re, sys

src, dst, node = sys.argv[1:4]
data = open(src).read()
data = data.replace("https://127.0.0.1:6443", f"https://{node}:6443")
match = re.search(rf"^(?P<indent>\s*)server: https://{re.escape(node)}:6443\s*$", data, re.M)
if match:
    insert = f"{match.group('indent')}tls-server-name: {node.split('.')[0]}\n"
    data = data[:match.end() + 1] + insert + data[match.end() + 1:]
open(dst, "w").write(data)
PYEOF
    rm -f "${KUBECONFIG_FILE}.tmp"
    chmod 600 "${KUBECONFIG_FILE}"
fi
export KUBECONFIG="${KUBECONFIG_FILE}"
"${KUBECTL}" get nodes >/dev/null || die "kubectl cannot reach the old cluster"

log "Old cluster reachable:"
"${KUBECTL}" get nodes

# 2. Longhorn backup target to the NFS export ------------------------------------
log "Pointing Longhorn's backup target at nfs://${NFS_SERVER}:${LH_BACKUP_PATH}"
"${KUBECTL}" -n longhorn-system patch backuptarget default --type merge -p "{
  \"spec\": {
    \"backupTargetURL\": \"nfs://${NFS_SERVER}:${LH_BACKUP_PATH}\",
    \"credentialSecret\": \"\",
    \"syncRequestedAt\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
  }
}"
for _ in $(seq 1 60); do
    available="$("${KUBECTL}" -n longhorn-system get backuptarget default -o jsonpath='{.status.available}' 2>/dev/null || true)"
    [[ "${available}" == "true" ]] && break
    sleep 5
done
[[ "${available:-}" == "true" ]] || die "Longhorn backup target is not available (is the NFS server up and exported?)"
log "Backup target is available"

# 3. dumps -----------------------------------------------------------------------
log "Dumping cluster resources"
"${KUBECTL}" get configmaps,deployments,daemonsets,statefulsets,services,ingresses,endpointslices,serviceaccounts,secrets,roles,rolebindings,cronjobs,jobs,hpa,networkpolicies,priorityclasses,storageclasses,persistentvolumes,persistentvolumeclaims -A -o yaml > "${BACKUP_DIR}/resources/all.yaml"
"${KUBECTL}" get clusterroles,clusterrolebindings -o yaml > "${BACKUP_DIR}/resources/cluster-rbac.yaml"
"${KUBECTL}" -n longhorn-system get volumes.longhorn.io,nodes.longhorn.io,replicas.longhorn.io,backups.longhorn.io,snapshots.longhorn.io -o yaml > "${BACKUP_DIR}/resources/longhorn-crs.yaml"

python3 - "${BACKUP_DIR}" <<'PYEOF'
import os, sys, yaml

backup_dir = sys.argv[1]

def clean(obj):
    if isinstance(obj, dict):
        obj.pop("status", None)
        md = obj.get("metadata")
        if isinstance(md, dict):
            for key in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "selfLink", "ownerReferences"):
                md.pop(key, None)
    return obj

# resources/all.yaml: expand List, remove the secret entries (dumped separately), scrub
with open(os.path.join(backup_dir, "resources", "all.yaml")) as f:
    docs = [d for d in yaml.safe_load_all(f) if d]
expanded = []
for d in docs:
    if d.get("kind") == "List":
        expanded.extend(d.get("items") or [])
    else:
        expanded.append(d)
docs = expanded

keep = [clean(d) for d in docs if d.get("kind") != "Secret"]
with open(os.path.join(backup_dir, "resources", "all.yaml"), "w") as f:
    yaml.safe_dump_all(keep, f, default_flow_style=False)

# secrets: keep everything except helm release state and service account tokens
secret_docs, skipped = [], []
for d in docs:
    if d.get("kind") != "Secret":
        continue
    name, ns, typ = d["metadata"].get("name"), d["metadata"].get("namespace"), d.get("type")
    if typ == "helm.sh/release.v1" or typ == "kubernetes.io/service-account-token":
        skipped.append(f"{ns}/{name}")
        continue
    secret_docs.append(clean(d))
with open(os.path.join(backup_dir, "secrets", "secrets.yaml"), "w") as f:
    yaml.safe_dump_all(secret_docs, f, default_flow_style=False)
print(f"    secrets: {len(secret_docs)} dumped, {len(skipped)} skipped (helm/token)")

# cluster-rbac.yaml scrub
with open(os.path.join(backup_dir, "resources", "cluster-rbac.yaml")) as f:
    docs = [clean(d) for d in yaml.safe_load_all(f) if d]
with open(os.path.join(backup_dir, "resources", "cluster-rbac.yaml"), "w") as f:
    yaml.safe_dump_all(docs, f, default_flow_style=False)
PYEOF

# 4. Longhorn backups --------------------------------------------------------------
log "Creating Longhorn backups"
mapfile -t VOLUMES < <("${KUBECTL}" -n longhorn-system get volumes.longhorn.io -o json | python3 -c '
import json, sys
vols = json.load(sys.stdin)["items"]
for v in vols:
    state = v.get("status", {}).get("state", "")
    if v["metadata"].get("deletionTimestamp") or state in ("deleting",):
        continue
    print(v["metadata"]["name"])
')

TS="$(date -u +%Y%m%d-%H%M%S)"
MANIFEST="${BACKUP_DIR}/longhorn/manifest-${TS}.txt"
: > "${MANIFEST}"

# Volumes this script attaches (Longhorn 1.12 attaches via VolumeAttachment
# tickets, not by setting spec.nodeID). The tickets are removed on exit.
ATTACHED_VOLS=()
cleanup_attachments() {
    for vol in "${ATTACHED_VOLS[@]}"; do
        "${KUBECTL}" -n longhorn-system patch volumeattachments.longhorn.io "${vol}" \
            --type merge -p '{"spec":{"attachmentTickets":{"migration-backup":null}}}' >/dev/null 2>&1 || true
    done
}
trap cleanup_attachments EXIT

for vol in "${VOLUMES[@]}"; do
    echo "--- ${vol}" | tee -a "${MANIFEST}"
    backup_name="backup-${vol}-${TS}"

    # attach the volume to a node that already holds one of its replicas
    node="$("${KUBECTL}" -n longhorn-system get replicas.longhorn.io -o json | python3 -c "
import json, sys
vol = sys.argv[1]
for r in json.load(sys.stdin)['items']:
    if r['spec'].get('volumeName') == vol and r['spec'].get('nodeID'):
        print(r['spec']['nodeID'])
        break
" "${vol}")"
    [[ -n "${node}" ]] || { warn "${vol}: no replica found, skipping"; continue; }

    current_state="$("${KUBECTL}" -n longhorn-system get volumes.longhorn.io "${vol}" -o jsonpath='{.status.state}')"
    if [[ "${current_state}" != "attached" ]]; then
        echo "    attaching to ${node}" | tee -a "${MANIFEST}"
        "${KUBECTL}" -n longhorn-system patch volumeattachments.longhorn.io "${vol}" --type merge -p "{
  \"spec\": {
    \"attachmentTickets\": {
      \"migration-backup\": {
        \"id\": \"migration-backup\",
        \"type\": \"migration-backup\",
        \"nodeID\": \"${node}\",
        \"generation\": 0,
        \"parameters\": {\"disableFrontend\": \"any\"}
      }
    }
  }
}"
        ATTACHED_VOLS+=("${vol}")
        for _ in $(seq 1 60); do
            current_state="$("${KUBECTL}" -n longhorn-system get volumes.longhorn.io "${vol}" -o jsonpath='{.status.state}')"
            [[ "${current_state}" == "attached" ]] && break
            sleep 5
        done
    fi
    [[ "${current_state}" == "attached" ]] || { warn "${vol}: could not attach"; continue; }

    # skip if a completed backup from a previous run is already present
    existing="$("${KUBECTL}" -n longhorn-system get backups.longhorn.io -o json | python3 -c "
import json, sys
vol = sys.argv[1]
for b in json.load(sys.stdin)['items']:
    if b['spec'].get('snapshotName', '').startswith('backup-' + vol) and b.get('status', {}).get('state') == 'Completed':
        print(b['metadata']['name'])
        break
" "${vol}")"
    if [[ -n "${existing}" ]]; then
        echo "    already backed up as ${existing}" | tee -a "${MANIFEST}"
        backup_name="${existing}"
    else
        echo "    snapshot + backup ${backup_name}" | tee -a "${MANIFEST}"
        cat <<EOF | "${KUBECTL}" apply -f - >/dev/null
apiVersion: longhorn.io/v1beta2
kind: Snapshot
metadata:
  name: ${backup_name}
  namespace: longhorn-system
  labels:
    migration-backup: "true"
spec:
  volume: ${vol}
  createSnapshot: true
EOF
        for _ in $(seq 1 120); do
            ready="$("${KUBECTL}" -n longhorn-system get snapshots.longhorn.io "${backup_name}" -o jsonpath='{.status.readyToUse}' 2>/dev/null || true)"
            [[ "${ready}" == "true" ]] && break
            sleep 5
        done
        [[ "${ready:-}" == "true" ]] || { warn "${vol}: snapshot not ready"; continue; }

        cat <<EOF | "${KUBECTL}" apply -f - >/dev/null
apiVersion: longhorn.io/v1beta2
kind: Backup
metadata:
  name: ${backup_name}
  namespace: longhorn-system
  labels:
    migration-backup: "true"
spec:
  snapshotName: ${backup_name}
  backupMode: full
  labels:
    migration-backup: "true"
EOF
    fi

    # wait for completion and collect the metadata the restore step needs
    for _ in $(seq 1 360); do
        state="$("${KUBECTL}" -n longhorn-system get backups.longhorn.io "${backup_name}" -o jsonpath='{.status.state}' 2>/dev/null || true)"
        [[ "${state}" == "Completed" ]] && break
        sleep 5
    done
    [[ "${state:-}" == "Completed" ]] || { warn "${vol}: backup did not complete (state=${state:-unknown})"; continue; }

    python3 - "${BACKUP_DIR}" "${vol}" "${backup_name}" <<'PYEOF'
import json, os, subprocess, sys, yaml

backup_dir, vol, backup_name = sys.argv[1:4]
KUBECTL = os.environ.get("KUBECTL", "kubectl")

def kget(*args):
    return json.loads(subprocess.check_output([KUBECTL, *args]))

volume = kget("-n", "longhorn-system", "get", "volumes.longhorn.io", vol, "-o", "json")
backup = kget("-n", "longhorn-system", "get", "backups.longhorn.io", backup_name, "-o", "json")
pv = None
pvc = None
pv_name = volume.get("status", {}).get("kubernetesStatus", {}).get("pvName")
if pv_name:
    try:
        pv = kget("get", "pv", pv_name, "-o", "json")
    except subprocess.CalledProcessError:
        pv = None
    if pv and pv["spec"].get("claimRef"):
        ref = pv["spec"]["claimRef"]
        try:
            pvc = kget("-n", ref["namespace"], "get", "pvc", ref["name"], "-o", "json")
        except subprocess.CalledProcessError:
            pvc = None

meta = {
    "volumeName": vol,
    "backupName": backup_name,
    "backupURL": backup["status"].get("url", ""),
    "size": volume["spec"].get("size", ""),
    "numberOfReplicas": volume["spec"].get("numberOfReplicas"),
    "dataLocality": volume["spec"].get("dataLocality", ""),
    "staleReplicaTimeout": volume["spec"].get("staleReplicaTimeout"),
    "kubernetesStatus": volume.get("status", {}).get("kubernetesStatus", {}),
    "pv": None,
    "pvc": None,
}
if pv:
    meta["pv"] = {
        "name": pv["metadata"]["name"],
        "capacity": pv["spec"]["capacity"]["storage"],
        "storageClassName": pv["spec"].get("storageClassName", ""),
        "accessModes": pv["spec"].get("accessModes", []),
        "volumeMode": pv["spec"].get("volumeMode", "Filesystem"),
        "fsType": pv["spec"].get("csi", {}).get("fsType", "ext4"),
        "volumeAttributes": pv["spec"].get("csi", {}).get("volumeAttributes", {}),
        "reclaimPolicy": pv["spec"].get("persistentVolumeReclaimPolicy", "Retain"),
        "claimRef": pv["spec"].get("claimRef"),
    }
if pvc:
    meta["pvc"] = {
        "name": pvc["metadata"]["name"],
        "namespace": pvc["metadata"]["namespace"],
        "storageClassName": pvc["spec"].get("storageClassName", ""),
        "volumeName": pvc["spec"].get("volumeName", ""),
    }

out = os.path.join(backup_dir, "longhorn", "volumes", f"{vol}.json")
with open(out, "w") as f:
    json.dump(meta, f, indent=2)
print(f"    metadata -> {out}")
PYEOF
    echo "    URL: $("${KUBECTL}" -n longhorn-system get backups.longhorn.io "${backup_name}" -o jsonpath='{.status.url}')" | tee -a "${MANIFEST}"
done

log "Backup summary:"
"${KUBECTL}" -n longhorn-system get backups.longhorn.io
cp "${BACKUP_DIR}/longhorn/manifest-${TS}.txt" "${BACKUP_DIR}/longhorn/manifest-latest.txt"

# 5. etcd snapshots + node configs ---------------------------------------------------
log "Copying k3s etcd snapshots and node configs"
ssh_sudo "${K3S_NODE}" "tar -C /var/lib/rancher/k3s/server/db/snapshots -cf - ." > "${BACKUP_DIR}/etcd/k3s-etcd-snapshots.tar" || warn "etcd snapshot copy failed"
for node in glemmen130 glemmen150 glemmen160 glemmen170 glemmen180 glemmen80; do
    echo "    ${node}"
    ssh_sudo "${node}.blalange.intra" "tar -C /etc -cf - netplan keepalived netbird hosts 2>/dev/null" > "${BACKUP_DIR}/node-configs/${node}.tar" || warn "node config copy failed for ${node}"
done

# 6. copy everything to glemmen60 ------------------------------------------------------
log "Copying dumps to ${NFS_SERVER_HOST}:${NFS_EXPORT}/migration-backup/"
ssh_sudo "${NFS_SERVER_HOST}" "mkdir -p ${NFS_EXPORT}/migration-backup && chown ${SSH_USER}:${SSH_USER} ${NFS_EXPORT}/migration-backup"
scp_to "${NFS_SERVER_HOST}" "${NFS_EXPORT}/migration-backup/" \
    "${BACKUP_DIR}/resources" "${BACKUP_DIR}/secrets" "${BACKUP_DIR}/longhorn" \
    "${BACKUP_DIR}/etcd" "${BACKUP_DIR}/node-configs" "${BACKUP_DIR}/k3s.yaml"

log "Done."
echo
echo "Backups on glemmen60   : ${NFS_EXPORT}/longhorn-backups (Longhorn backupstore)"
echo "Dumps/metadata locally : ${BACKUP_DIR}"
echo "Dumps/metadata on 60   : ${NFS_EXPORT}/migration-backup"
echo
echo "Verify with:"
echo "  kubectl -n longhorn-system get backups.longhorn.io"
echo "  kubectl -n longhorn-system get backuptarget default -o yaml | grep -A3 status"
