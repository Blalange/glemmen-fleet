#!/usr/bin/env python3
"""Build per-node Talos setup ISOs with maintenance-mode network arguments.

The kernel arguments (bond + static IP + hostname) are derived from
talos/nodes/<host>.yaml, so they always match the machine configuration.
Extensions come from talos/schematic.yaml.

Output: talos/generated/iso/setup-<host>.iso + .sha256

Usage:
  ./scripts/talos/setup-images.py                 # every node in talos/nodes/
  ./scripts/talos/setup-images.py glemmen150      # only these nodes
  ./scripts/talos/setup-images.py --force         # re-download even if present
  ./scripts/talos/setup-images.py --print         # only show kernel arguments
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import common  # noqa: E402

SCHEMATIC_FILE = common.TALOS_DIR / "schematic.yaml"
ISO_DIR = common.GENERATED_DIR / "iso"


def netmask(prefix: str) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)


def find_document(documents: list[dict], kind: str) -> dict | None:
    return next(
        (document for document in documents if document.get("kind") == kind),
        None,
    )


def kernel_arguments(node_path: Path) -> list[str]:
    yaml = common.require_yaml()
    try:
        documents = [
            document
            for document in yaml.safe_load_all(node_path.read_text(encoding="utf-8"))
            if document
        ]
    except (OSError, ValueError) as exc:
        common.die(f"failed to read {node_path}: {exc}")

    hostname = find_document(documents, "HostnameConfig")
    if hostname is None:
        common.die(f"{node_path} has no HostnameConfig")

    bond = find_document(documents, "BondConfig")
    link = find_document(documents, "LinkConfig")

    arguments: list[str] = []
    if bond:
        parameters = [f"mode={bond['bondMode']}"]
        if bond.get("xmitHashPolicy"):
            parameters.append(f"xmit_hash_policy={bond['xmitHashPolicy']}")
        if bond.get("lacpRate"):
            parameters.append(f"lacp_rate={bond['lacpRate']}")
        if bond.get("miimon"):
            parameters.append(f"miimon={bond['miimon']}")
        arguments.append(
            f"bond={bond['name']}:{','.join(bond['links'])}:"
            f"{','.join(parameters)}"
        )
        network = bond
    else:
        network = link

    if network:
        address = network["addresses"][0]["address"]
        ip, prefix = address.split("/")
        gateway = network["routes"][0]["gateway"]
        # ip=<client-ip>:<server-ip>:<gw>:<netmask>:<hostname>:<device>:<autoconf>:<dns0>:<dns1>:<ntp0>
        arguments.append(
            f"ip={ip}::{gateway}:{netmask(prefix)}:{hostname['hostname']}:"
            f"{network['name']}:off:192.168.6.1:1.1.1.1:"
        )
    # No BondConfig/LinkConfig: the node relies on DHCP (e.g. glemmen60, whose
    # address is reserved on the Servere VLAN). Maintenance mode then uses DHCP
    # on all links by default and no kernel arguments are needed.

    return arguments


def customization(host: str, schematic: dict) -> dict:
    node_path = common.NODES_DIR / f"{host}.yaml"
    if not node_path.is_file():
        common.die(f"{node_path} not found")

    values = dict((schematic or {}).get("customization") or {})
    values["extraKernelArgs"] = kernel_arguments(node_path)
    return {"customization": values}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup-images.py",
        description="Build/download per-node Talos setup ISOs.",
    )
    parser.add_argument("hosts", nargs="*", help="nodes to build (default: all)")
    parser.add_argument(
        "--force", action="store_true", help="re-download even if an ISO exists"
    )
    parser.add_argument(
        "--print", dest="print_only", action="store_true",
        help="only print the maintenance-mode kernel arguments",
    )
    args = parser.parse_args(argv)

    settings = common.load_talos_settings()
    talos_version = settings.get("TALOS_VERSION", "")
    if not talos_version:
        common.die("TALOS_VERSION is not set in talos/settings.env")
    if not SCHEMATIC_FILE.is_file():
        common.die(f"{SCHEMATIC_FILE} not found")

    yaml = common.require_yaml()
    try:
        schematic = yaml.safe_load(SCHEMATIC_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        common.die(f"failed to read {SCHEMATIC_FILE}: {exc}")

    hosts = args.hosts or sorted(path.stem for path in common.NODES_DIR.glob("*.yaml"))
    if not hosts:
        common.die(f"no node configs found in {common.NODES_DIR}")

    if not args.print_only:
        ISO_DIR.mkdir(parents=True, exist_ok=True)

    for host in hosts:
        body = customization(host, schematic)
        arguments = body["customization"].get("extraKernelArgs", [])

        if args.print_only:
            print(f"==> {host}:")
            if arguments:
                for argument in arguments:
                    print(f"    {argument}")
            else:
                print("    (no kernel arguments: DHCP by default)")
            continue

        output = ISO_DIR / f"setup-{host}.iso"
        if output.is_file() and not args.force:
            print(
                f"==> {host}: {output} already exists "
                "(use --force to rebuild)"
            )
            continue

        print(f"==> {host}: creating schematic")
        schematic_id = common.create_schematic(body)
        print(f"    schematic: {schematic_id}")

        print(f"==> {host}: downloading setup ISO")
        image_url = (
            f"{common.FACTORY_URL}/image/{schematic_id}/{talos_version}"
            "/metal-amd64.iso"
        )
        common.download_file(image_url, output)

        digest = common.sha256_file(output)
        checksum = f"{digest}  {output}\n"
        output.with_name(output.name + ".sha256").write_text(
            checksum, encoding="utf-8"
        )
        print(f"    {checksum.rstrip()}")

    if args.print_only:
        return 0

    print()
    print(f"Setup ISOs in {ISO_DIR}:")
    for path in sorted(ISO_DIR.glob("setup-*.iso")):
        print(f"    {path.name}  {common.human_size(path.stat().st_size)}")
    print()
    print("Boot one on the matching machine (UEFI, USB or virtual media); the")
    print("maintenance network comes up automatically - no GRUB editing needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
