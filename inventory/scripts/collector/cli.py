"""Collector orchestration: collect sources, resolve devices, render YAML."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from inventory_util import InventoryError

from .build_devices import build_nodes, build_servers
from .config import (
    DEFAULT_IDRAC_PASS,
    DEFAULT_IDRAC_USER,
    DEFAULT_MANUAL_DIR,
    DEFAULT_OUTPUT_DIR,
)
from .identity import DeviceResolver, MappingError
from .manual import load_manual
from .process import read_cluster_secret
from .render import write_yaml
from .sources.redfish import read_redfish
from .sources.talos import node_system_info, read_nodes
from .sources.unifi import build_unifi, unifi_get


def _refs_for_talos(node_name: str, system_info: dict[str, Any]) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {
        "name": {node_name},
        "talos_node": {node_name},
    }
    serial = str(system_info.get("serialnumber") or "").strip()
    uuid = str(system_info.get("uuid") or "").strip()
    if serial:
        refs["service_tag"] = {serial}
    if uuid:
        refs["system_uuid"] = {uuid}
    return refs


def _resolve_talos(
    resolver: DeviceResolver,
    nodes: list[dict],
    *,
    talosctl: str | None,
    talosconfig: str | None,
) -> dict[str, dict[str, Any]]:
    manual_by_node: dict[str, dict[str, Any]] = {}
    for node in nodes:
        name = str((node.get("metadata") or {}).get("name") or "")
        if not name:
            continue
        system_info = (
            node_system_info(talosctl, talosconfig, name) if talosctl else {}
        )
        refs = _refs_for_talos(name, system_info)
        manual_device = resolver.resolve(refs, source="talos", label=name)
        if manual_device is None:
            print(
                f"WARNING: Talos node {name} has no manual device entry",
                file=sys.stderr,
            )
            continue
        manual_by_node[name] = manual_device
        resolver.add_observation_claims(
            manual_device,
            {
                "service_tag": {system_info.get("serialnumber")},
                "system_uuid": {system_info.get("uuid")},
            },
        )
    return manual_by_node


def _collect_redfish(
    manual: dict[str, Any],
    resolver: DeviceResolver,
    *,
    username: str | None,
    password: str | None,
) -> dict[str, dict[str, Any]]:
    if not username or not password:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for device in manual["devices"]:
        address = device.get("idrac_ip")
        if not address:
            continue
        data = read_redfish(f"https://{address}", username, password)
        if not data:
            continue
        system = data.get("system") or {}
        service_tag = str(system.get("SKU") or system.get("SerialNumber") or "").strip()
        system_uuid = str(system.get("UUID") or "").strip()
        refs: dict[str, set[str]] = {"idrac_ip": {str(address)}}
        if service_tag:
            refs["service_tag"] = {service_tag}
        if system_uuid:
            refs["system_uuid"] = {system_uuid}
        try:
            resolved = resolver.resolve(
                refs, source="redfish", label=str(address)
            )
        except MappingError:
            raise
        if resolved is None:
            print(
                f"WARNING: iDRAC {address} is not mapped to a manual device",
                file=sys.stderr,
            )
            continue
        name = str(resolved.get("name") or "")
        if name != str(device.get("name") or ""):
            print(
                f"WARNING: iDRAC {address} resolves to {name!r}, not "
                f"{device.get('name')!r}; using {name!r}",
                file=sys.stderr,
            )
        resolver.add_observation_claims(
            resolved,
            {
                "service_tag": {service_tag},
                "system_uuid": {system_uuid},
            },
        )
        if name in result:
            raise InventoryError(f"multiple iDRACs resolve to {name!r}")
        result[name] = data
        print(f"Redfish: {name} ({address})", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual-dir", default=DEFAULT_MANUAL_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--talos-config-dir", default="talos/nodes")
    parser.add_argument("--kubectl", default=os.environ.get("KUBECTL", "kubectl"))
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    parser.add_argument("--nodes-json", help="use a saved kubectl get nodes -o json file")
    parser.add_argument(
        "--talosctl",
        default=os.environ.get("TALOSCTL"),
        help="talosctl binary used to discover hardware (omit to skip)",
    )
    parser.add_argument(
        "--talosconfig",
        default=os.environ.get("TALOSCONFIG", "talos/generated/talosconfig"),
        help="talosconfig for the talosctl lookups",
    )
    parser.add_argument("--from-cluster", action="store_true", help="read UniFi credentials from unpoller-unifi")
    parser.add_argument("--unifi-url", default=os.environ.get("UNIFI_URL", "https://unifi.glem.men"))
    parser.add_argument(
        "--redfish-user",
        default=os.environ.get("IDRAC_USER", DEFAULT_IDRAC_USER),
        help="iDRAC username for Redfish (default: IDRAC_USER or readonly)",
    )
    parser.add_argument(
        "--redfish-pass",
        default=os.environ.get("IDRAC_PASS", DEFAULT_IDRAC_PASS),
        help="iDRAC password for Redfish (default: IDRAC_PASS or readonly)",
    )
    args = parser.parse_args(argv)

    manual = load_manual(Path(args.manual_dir))
    resolver = DeviceResolver(manual)

    if args.from_cluster:
        username, password = read_cluster_secret(args.kubectl, args.kubeconfig)
    else:
        username = os.environ.get("UNIFI_USER", "")
        password = os.environ.get("UNIFI_PASS", "")
    if not username or not password:
        raise InventoryError("set UNIFI_USER/UNIFI_PASS or use --from-cluster")

    devices, networks, clients = unifi_get(args.unifi_url.rstrip("/"), username, password)
    unifi_devices, cables, vlans, prefixes, _names = build_unifi(devices, networks, manual)
    switch_name = next(
        (
            device["name"]
            for device in unifi_devices
            if device.get("device_type") == "ubiquiti-usw-pro-24-poe"
        ),
        "USW Pro 24 PoE",
    )

    nodes = read_nodes(args.kubectl, args.kubeconfig, args.nodes_json)
    manual_by_node = _resolve_talos(
        resolver,
        nodes,
        talosctl=args.talosctl,
        talosconfig=args.talosconfig,
    )
    redfish_by_name = _collect_redfish(
        manual,
        resolver,
        username=args.redfish_user,
        password=args.redfish_pass,
    )

    module_types: dict[str, dict[str, Any]] = {}
    servers = build_servers(
        clients,
        manual,
        switch_name,
        module_types=module_types,
        redfish_by_name=redfish_by_name,
    )
    compute_nodes = build_nodes(
        nodes,
        Path(args.talos_config_dir),
        manual,
        talosctl=args.talosctl,
        talosconfig=args.talosconfig,
        module_types=module_types,
        redfish_by_name=redfish_by_name,
        manual_by_node=manual_by_node,
    )

    output_dir = Path(args.output_dir)
    write_yaml(
        output_dir / "unifi.yaml",
        {"devices": unifi_devices, "vlans": vlans, "prefixes": prefixes, "cables": cables},
        "UniFi devices, VLANs, prefixes and uplink cables.",
    )
    write_yaml(
        output_dir / "compute.yaml",
        {"devices": [*servers, *compute_nodes]},
        "Canonical servers and Talos nodes with discovered components.",
    )
    write_yaml(
        output_dir / "module-types.yaml",
        {"module_types": list(module_types.values())},
        "Module types discovered across all compute sources.",
    )
    for legacy in ("servers.yaml", "talos.yaml"):
        legacy_path = output_dir / legacy
        if legacy_path.exists():
            legacy_path.unlink()

    print(f"Wrote {output_dir}/unifi.yaml, compute.yaml, module-types.yaml")
    print(
        f"  unifi_devices={len(unifi_devices)}, servers={len(servers)}, nodes={len(compute_nodes)}, "
        f"cables={len(cables)}, vlans={len(vlans)}, prefixes={len(prefixes)}, "
        f"module_types={len(module_types)}, "
        f"modules={sum(len(device.get('modules', [])) for device in [*servers, *compute_nodes])}"
    )
    return 0
