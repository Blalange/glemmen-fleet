#!/usr/bin/env bash
# Restore the migration backups into the new Talos cluster.
#
# Run this from the workstation with KUBECONFIG pointing at the new cluster
# (default: talos/generated/kubeconfig). The metadata produced by
# scripts/backup-old-cluster.sh must be present in scripts/migration-backup/.
#
# What it does:
#   1. restores the Kubernetes Secrets dumped from the old cluster
#   2. creates the Longhorn Volume + PersistentVolume objects for every backed-up
#      volume (restore from the NFS backupstore), bound to the original PVCs
#   3. waits for the Longhorn volumes to appear
#
# The application Kustomizations must be suspended while restoring (Flux must not
# create empty volumes for the PVCs first):
#   ./flux/manage.sh freeze
#   ./scripts/restore-to-new-cluster.sh
#   ./flux/manage.sh unfreeze
#
# After unfreezing, Flux applies the app manifests, the PVCs bind to the restored
# PVs and the pods start with the old data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${SCRIPT_DIR}/migration-backup}"
export KUBECONFIG="${KUBECONFIG:-${REPO_ROOT}/talos/generated/kubeconfig}"

KUBECTL="${KUBECTL:-}"
if [[ -z "${KUBECTL}" ]]; then
    if command -v kubectl >/dev/null 2>&1; then
        KUBECTL="$(command -v kubectl)"
    elif [[ -x "${SCRIPT_DIR}/bin/kubectl" ]]; then
        KUBECTL="${SCRIPT_DIR}/bin/kubectl"
    else
        echo "ERROR: kubectl not found (run scripts/backup-old-cluster.sh once, or install it)" >&2
        exit 1
    fi
fi

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "${KUBECONFIG}" ]] || die "KUBECONFIG ${KUBECONFIG} not found"
"${KUBECTL}" get nodes >/dev/null || die "kubectl cannot reach the new cluster"

# 1. Secrets ---------------------------------------------------------------------
if [[ -f "${BACKUP_DIR}/secrets/secrets.yaml" ]]; then
    log "Restoring secrets"
    # Namespaces are created by Flux (flux/infrastructure/namespaces.yaml). Wait
    # for them so the secret apply does not fail.
    for _ in $(seq 1 60); do
        missing=0
        for ns in cert-manager colibri energisjappa frigate homepage monitoring snipe-it unifi panel longhorn-system; do
            "${KUBECTL}" get namespace "${ns}" >/dev/null 2>&1 || missing=1
        done
        [[ "${missing}" -eq 0 ]] && break
        sleep 5
    done
    # Skip Rancher/Fleet namespaces (cattle-*): the Rancher import recreates its
    # own credentials, and those namespaces do not exist before the import.
    python3 - "${BACKUP_DIR}/secrets/secrets.yaml" > "${BACKUP_DIR}/secrets/secrets.filtered.yaml" <<'PYEOF'
import sys, yaml

for doc in yaml.safe_load_all(open(sys.argv[1])):
    if not doc:
        continue
    ns = doc.get("metadata", {}).get("namespace", "")
    if ns.startswith("cattle-"):
        continue
    print("---")
    yaml.safe_dump(doc, sys.stdout, default_flow_style=False)
PYEOF
    "${KUBECTL}" apply -f "${BACKUP_DIR}/secrets/secrets.filtered.yaml"
else
    echo "WARNING: ${BACKUP_DIR}/secrets/secrets.yaml not found, skipping secrets"
fi

# 2. Longhorn volumes --------------------------------------------------------------
shopt -s nullglob
VOLUME_FILES=("${BACKUP_DIR}"/longhorn/volumes/*.json)
shopt -u nullglob
[[ ${#VOLUME_FILES[@]} -gt 0 ]] || die "no longhorn volume metadata found in ${BACKUP_DIR}/longhorn/volumes"

log "Restoring ${#VOLUME_FILES[@]} Longhorn volumes"
for meta in "${VOLUME_FILES[@]}"; do
    vol="$(basename "${meta}" .json)"
    url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["backupURL"])' "${meta}")"
    [[ -n "${url}" ]] || { echo "    ${vol}: no backup URL, skipping"; continue; }

    if "${KUBECTL}" -n longhorn-system get volumes.longhorn.io "${vol}" >/dev/null 2>&1; then
        echo "    ${vol}: volume already exists, skipping"
    else
        echo "    ${vol}: creating volume from backup"
        python3 - "${meta}" <<'PYEOF' | "${KUBECTL}" apply -f - >/dev/null
import json, sys

meta = json.load(open(sys.argv[1]))
spec = {
    "size": meta["size"],
    "numberOfReplicas": meta.get("numberOfReplicas") or 1,
    "frontend": "blockdev",
    "accessMode": "rwo",
    "fromBackup": meta["backupURL"],
    "backupTargetName": "default",
}
if meta.get("dataLocality"):
    spec["dataLocality"] = meta["dataLocality"]

print(json.dumps({
    "apiVersion": "longhorn.io/v1beta2",
    "kind": "Volume",
    "metadata": {"name": meta["volumeName"], "namespace": "longhorn-system"},
    "spec": spec,
}))
PYEOF
    fi

    # PersistentVolume (same name as the original) bound to the original claim.
    pv_name="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print((d.get("pv") or {}).get("name",""))' "${meta}")"
    [[ -n "${pv_name}" ]] || { echo "    ${vol}: no PV metadata, skipping PV"; continue; }
    if "${KUBECTL}" get pv "${pv_name}" >/dev/null 2>&1; then
        echo "    ${pv_name}: PV already exists, skipping"
    else
        echo "    ${pv_name}: creating PV"
        python3 - "${meta}" <<'PYEOF' | "${KUBECTL}" apply -f - >/dev/null
import json, sys

meta = json.load(open(sys.argv[1]))
pv = meta["pv"]

print(json.dumps({
    "apiVersion": "v1",
    "kind": "PersistentVolume",
    "metadata": {"name": pv["name"]},
    "spec": {
        "capacity": {"storage": pv["capacity"]},
        "accessModes": pv.get("accessModes") or ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": pv.get("reclaimPolicy", "Retain"),
        "storageClassName": pv.get("storageClassName", ""),
        "volumeMode": pv.get("volumeMode", "Filesystem"),
        "claimRef": pv.get("claimRef"),
        "csi": {
            "driver": "driver.longhorn.io",
            "fsType": pv.get("fsType", "ext4"),
            "volumeHandle": meta["volumeName"],
            "volumeAttributes": pv.get("volumeAttributes") or {},
        },
    },
}))
PYEOF
    fi
done

log "Waiting for Longhorn volumes to be registered"
for _ in $(seq 1 60); do
    count="$("${KUBECTL}" -n longhorn-system get volumes.longhorn.io --no-headers 2>/dev/null | wc -l)"
    [[ "${count}" -ge "${#VOLUME_FILES[@]}" ]] && break
    sleep 5
done
"${KUBECTL}" -n longhorn-system get volumes.longhorn.io

echo
echo "Active Kustomizations are suspended? Resume them now:"
echo "  ./flux/manage.sh unfreeze"
echo "  ./flux/manage.sh status"
echo
echo "Then verify the application PVCs bind to the restored PVs and the pods start."
echo "For the load balancer cutover see flux/README.md (migration notes)."
