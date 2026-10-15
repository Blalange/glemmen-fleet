#!/usr/bin/env bash
# Build per-node Talos setup ISOs with the maintenance-mode network arguments
# (bond + static IP + hostname) baked into the bootloader, so no GRUB editing is
# needed when booting a machine.
#
# The kernel arguments are derived from talos/nodes/<host>.yaml, so they always
# match the machine configuration. The extensions come from talos/schematic.yaml.
#
# Output: talos/generated/iso/setup-<host>.iso + .sha256
#
# Usage:
#   ./setup-images.sh                 # build/download for every node in nodes/
#   ./setup-images.sh glemmen150      # only these nodes
#   ./setup-images.sh --force         # re-download even if present
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=settings.env
source "${SCRIPT_DIR}/settings.env"

FACTORY="https://factory.talos.dev"
NODES_DIR="${SCRIPT_DIR}/nodes"
SCHEMATIC_FILE="${SCRIPT_DIR}/schematic.yaml"
ISO_DIR="${SCRIPT_DIR}/generated/iso"

FORCE=0
PRINT=0
HOSTS=()
for arg in "$@"; do
    case "${arg}" in
        --force) FORCE=1 ;;
        --print) PRINT=1 ;;
        *) HOSTS+=("${arg}") ;;
    esac
done
if [[ ${#HOSTS[@]} -eq 0 ]]; then
    for f in "${NODES_DIR}"/*.yaml; do
        HOSTS+=("$(basename "${f}" .yaml)")
    done
fi

command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
python3 -c 'import yaml' 2>/dev/null || { echo "python3-yaml (PyYAML) is required" >&2; exit 1; }

mkdir -p "${ISO_DIR}"

# Emit "<host>\t<base64 json customization>" for every requested node.
mapfile -t SPECS < <(python3 - "${SCHEMATIC_FILE}" "${NODES_DIR}" "${HOSTS[@]}" <<'PYEOF'
import base64, glob, ipaddress, json, sys, yaml

schematic_file, nodes_dir = sys.argv[1], sys.argv[2]
hosts = sys.argv[3:]
base = yaml.safe_load(open(schematic_file))

def netmask(prefix):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)

for host in hosts:
    path = f"{nodes_dir}/{host}.yaml"
    docs = [d for d in yaml.safe_load_all(open(path)) if d]
    hostname = next(d["hostname"] for d in docs if d.get("kind") == "HostnameConfig")

    bond = next((d for d in docs if d.get("kind") == "BondConfig"), None)
    link = next((d for d in docs if d.get("kind") == "LinkConfig"), None)

    kernel_args = []
    if bond:
        params = [f"mode={bond['bondMode']}"]
        if bond.get("xmitHashPolicy"):
            params.append(f"xmit_hash_policy={bond['xmitHashPolicy']}")
        if bond.get("lacpRate"):
            params.append(f"lacp_rate={bond['lacpRate']}")
        if bond.get("miimon"):
            params.append(f"miimon={bond['miimon']}")
        kernel_args.append(f"bond={bond['name']}:{','.join(bond['links'])}:{','.join(params)}")
        network = bond
    else:
        network = link

    address = network["addresses"][0]["address"]      # primary address, CIDR
    ip, prefix = address.split("/")
    gateway = network["routes"][0]["gateway"]
    # ip=<client-ip>:<server-ip>:<gw>:<netmask>:<hostname>:<device>:<autoconf>:<dns0>:<dns1>:<ntp0>
    kernel_args.append(f"ip={ip}::{gateway}:{netmask(prefix)}:{hostname}:{network['name']}:off:192.168.6.1:1.1.1.1:")

    customization = dict(base.get("customization", {}))
    customization["extraKernelArgs"] = kernel_args
    body = {"customization": customization}
    print(f"{host}\t{base64.b64encode(json.dumps(body).encode()).decode()}")
PYEOF
)

for spec in "${SPECS[@]}"; do
    host="${spec%%$'\t'*}"
    payload="${spec#*$'\t'}"

    if [[ "${PRINT}" -eq 1 ]]; then
        echo "==> ${host}:"
        echo "${payload}" | base64 -d | python3 -c 'import json,sys; [print("    " + a) for a in json.load(sys.stdin)["extraKernelArgs"]]'
        continue
    fi

    out="${ISO_DIR}/setup-${host}.iso"

    if [[ -f "${out}" && "${FORCE}" -eq 0 ]]; then
        echo "==> ${host}: ${out} already exists (use --force to rebuild)"
        continue
    fi

    echo "==> ${host}: creating schematic"
    schematic_id="$(echo "${payload}" | base64 -d | curl -sfX POST "${FACTORY}/schematics" \
        -H 'Content-Type: application/json' --data @- | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
    echo "    schematic: ${schematic_id}"

    echo "==> ${host}: downloading setup ISO"
    curl -fL --progress-bar -o "${out}" \
        "${FACTORY}/image/${schematic_id}/${TALOS_VERSION}/metal-amd64.iso"
    sha256sum "${out}" > "${out}.sha256"
    echo "    $(cat "${out}.sha256")"
done

if [[ "${PRINT}" -eq 1 ]]; then
    exit 0
fi

echo
echo "Setup ISOs in ${ISO_DIR}:"
ls -lh "${ISO_DIR}"/setup-*.iso
echo
echo "Boot one on the matching machine (UEFI, USB or virtual media); the"
echo "maintenance network comes up automatically - no GRUB editing needed."
