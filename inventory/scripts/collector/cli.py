"""Collector orchestration: collect sources, resolve devices, render YAML."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path
from typing import Any

from inventory_util import InventoryError

from .build_devices import build_nodes, build_servers
from .config import (
    DEFAULT_IPMI_PASS,
    DEFAULT_IPMI_USER,
    DEFAULT_MANUAL_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_REDFISH_PASS,
    DEFAULT_REDFISH_USER,
)
from .identity import DeviceResolver
from .log import configure_logging, get_logger
from .manual import load_manual
from .process import read_cluster_secret
from .render import write_yaml
from .snmp_merge import merge_snmp_observations
from .sources.ipmi import read_ipmi
from .sources.redfish import read_redfish
from .sources.snmp import collect_snmp, write_snmp_report
from .sources.talos import node_system_info, read_nodes
from .sources.unifi import build_unifi, unifi_get

log = get_logger(__name__)


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
            log.warning("Talos node %s has no manual device entry", name)
            continue
        manual_by_node[name] = manual_device
        log.debug(
            "Talos node %s resolved to manual device %s%s",
            name,
            manual_device.get("name"),
            f" (service tag {system_info.get('serialnumber')})"
            if system_info.get("serialnumber")
            else "",
        )
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
        log.info("Redfish: disabled (no credentials)")
        return {}
    candidates = [device for device in manual["devices"]
                  if device.get("redfish_ip")]
    if not candidates:
        log.info("Redfish: no manual devices declare redfish_ip")
        return {}
    log.info("Redfish: polling %d iDRAC(s)", len(candidates))
    result: dict[str, dict[str, Any]] = {}
    for device in candidates:
        address = device.get("redfish_ip")
        log.debug(
            "Redfish: reading %s at %s",
            device.get("name") or "unknown device",
            address,
        )
        data = read_redfish(f"https://{address}", username, password)
        if not data:
            continue
        system = data.get("system") or {}
        service_tag = str(system.get("SKU") or system.get(
            "SerialNumber") or "").strip()
        system_uuid = str(system.get("UUID") or "").strip()
        refs: dict[str, set[str]] = {"redfish_ip": {str(address)}}
        if service_tag:
            refs["service_tag"] = {service_tag}
        if system_uuid:
            refs["system_uuid"] = {system_uuid}
        resolved = resolver.resolve(refs, source="redfish", label=str(address))
        if resolved is None:
            log.warning("iDRAC %s is not mapped to a manual device", address)
            continue
        name = str(resolved.get("name") or "")
        if name != str(device.get("name") or ""):
            log.warning(
                "iDRAC %s resolves to %r, not %r; using %r",
                address,
                name,
                device.get("name"),
                name,
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
        log.info(
            "Redfish: %s (%s) cpu=%d memory=%d disks=%d fans=%d psus=%d",
            name,
            address,
            len(data.get("cpu") or []),
            len(data.get("memory") or []),
            len(data.get("disks") or []),
            len(data.get("fans") or []),
            len(data.get("psus") or []),
        )
    log.info("Redfish: collected %d/%d iDRAC(s)", len(result), len(candidates))
    return result


def _collect_ipmi(
    manual: dict[str, Any],
    resolver: DeviceResolver,
    *,
    username: str | None,
    password: str | None,
) -> dict[str, dict[str, Any]]:
    if not username or not password:
        log.info("IPMI: disabled (no credentials)")
        return {}
    candidates = [device for device in manual["devices"]
                  if device.get("ipmi_ip")]
    if not candidates:
        log.info("IPMI: no manual devices declare ipmi_ip")
        return {}
    log.info("IPMI: polling %d BMC(s)", len(candidates))
    result: dict[str, dict[str, Any]] = {}
    for device in candidates:
        address = device.get("ipmi_ip")
        log.debug("IPMI: reading %s at %s", device.get(
            "name") or "unknown device", address)
        data = read_ipmi(str(address), username, password)
        if not data:
            continue
        system = data.get("system") or {}
        serial = str(system.get("SerialNumber")
                     or system.get("SKU") or "").strip()
        refs: dict[str, set[str]] = {"ipmi_ip": {str(address)}}
        if serial:
            refs["service_tag"] = {serial}
        resolved = resolver.resolve(refs, source="ipmi", label=str(address))
        if resolved is None:
            log.warning("IPMI %s is not mapped to a manual device", address)
            continue
        name = str(resolved.get("name") or "")
        if name != str(device.get("name") or ""):
            log.warning(
                "IPMI %s resolves to %r, not %r; using %r",
                address,
                name,
                device.get("name"),
                name,
            )
        if serial:
            resolver.add_observation_claims(
                resolved, {"service_tag": {serial}})
        if name in result:
            raise InventoryError(f"multiple IPMI BMCs resolve to {name!r}")
        result[name] = data
        log.info(
            "IPMI: %s (%s) model=%s serial=%s fans=%d psus=%d",
            name,
            address,
            system.get("Model") or "unknown",
            serial or "unknown",
            len(data.get("fans") or []),
            len(data.get("psus") or []),
        )
    log.info("IPMI: collected %d/%d BMC(s)", len(result), len(candidates))
    return result


SNMP_UNIFI_DEVICE_TYPES = {
    "ubiquiti-udm-pro",
    "ubiquiti-usw-pro-24-poe",
    "ubiquiti-us-8-poe-150w",
    "ubiquiti-u6-pro",
}


def _unifi_ip(device: dict[str, Any]) -> str:
    return str(
        device.get("lan_ip")
        or device.get("ip")
        or (device.get("config_network") or {}).get("ip")
        or ""
    ).strip()


def _register_unifi_devices(
    resolver: DeviceResolver,
    devices: list[dict[str, Any]],
    names: dict[str, str],
) -> None:
    for device in devices:
        mac = str(device.get("mac") or "").upper()
        name = names.get(mac)
        if not name:
            continue
        resolver.register_generated_device(
            name,
            {
                "snmp_mac": {mac},
                "snmp_serial": {str(device.get("serial") or "")},
                "snmp_ip": {_unifi_ip(device)},
                "snmp_name": {name},
            },
        )


def _snmp_targets(
    manual: dict[str, Any],
    devices: list[dict[str, Any]],
    names: dict[str, str],
    unifi_devices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    device_types = {
        str(device.get("name")): str(device.get("device_type") or "")
        for device in unifi_devices
    }
    for device in manual["devices"]:
        address = str(device.get("snmp_ip") or "").strip()
        if address:
            targets[address] = {
                "ip": address,
                "name": str(device.get("name") or ""),
                "source": "manual",
            }
    for device in devices:
        if device.get("state") != 1:
            continue
        address = _unifi_ip(device)
        if not address:
            continue
        mac = str(device.get("mac") or "").upper()
        name = names.get(mac, str(device.get("name") or ""))
        if device_types.get(name) not in SNMP_UNIFI_DEVICE_TYPES:
            continue
        targets.setdefault(
            address,
            {
                "ip": address,
                "name": name,
                "source": "unifi",
            },
        )
    return list(targets.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual-dir", default=DEFAULT_MANUAL_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--talos-config-dir", default="talos/nodes")
    parser.add_argument(
        "--kubectl", default=os.environ.get("KUBECTL", "kubectl"))
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    parser.add_argument(
        "--nodes-json", help="use a saved kubectl get nodes -o json file")
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
    parser.add_argument("--from-cluster", action="store_true",
                        help="read UniFi credentials from unpoller-unifi")
    parser.add_argument(
        "--unifi-url", default=os.environ.get("UNIFI_URL", "https://unifi.glem.men"))
    parser.add_argument(
        "--redfish-user",
        default=os.environ.get(
            "REDFISH_USER", os.environ.get("IDRAC_USER", DEFAULT_REDFISH_USER)
        ),
        help="BMC username for Redfish (default: REDFISH_USER, IDRAC_USER or readonly)",
    )
    parser.add_argument(
        "--redfish-pass",
        default=os.environ.get(
            "REDFISH_PASS", os.environ.get("IDRAC_PASS", DEFAULT_REDFISH_PASS)
        ),
        help="BMC password for Redfish (default: REDFISH_PASS, IDRAC_PASS or readonly)",
    )
    parser.add_argument(
        "--ipmi-user",
        default=os.environ.get("IPMI_USER", DEFAULT_IPMI_USER),
        help="BMC username for IPMI (default: IPMI_USER or readonly)",
    )
    parser.add_argument(
        "--ipmi-pass",
        default=os.environ.get("IPMI_PASS", DEFAULT_IPMI_PASS),
        help="BMC password for IPMI (default: IPMI_PASS or readonly)",
    )
    parser.add_argument(
        "--snmp-community",
        default=os.environ.get("SNMP_COMMUNITY", "public"),
        help="SNMP v2c community (default: SNMP_COMMUNITY or public)",
    )
    parser.add_argument(
        "--snmp-timeout",
        type=float,
        default=float(os.environ.get("SNMP_TIMEOUT", "2")),
        help="SNMP timeout in seconds (default: 2)",
    )
    parser.add_argument(
        "--snmp-retries",
        type=int,
        default=int(os.environ.get("SNMP_RETRIES", "1")),
        help="SNMP retry count (default: 1)",
    )
    parser.add_argument(
        "--no-snmp",
        action="store_true",
        help="disable SNMP collection",
    )
    parser.add_argument(
        "--snmp-report",
        help="write LLDP/FDB and interface observations to a JSON report",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable DEBUG logging (per-device source details)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("COLLECTOR_LOG_LEVEL", "INFO"),
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="logging threshold (default: COLLECTOR_LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("COLLECTOR_LOG_FILE"),
        help="also write logs to this file (default: COLLECTOR_LOG_FILE)",
    )
    args = parser.parse_args(argv)

    configure_logging(
        "DEBUG" if args.verbose else args.log_level, args.log_file)
    started = time.monotonic()
    log.info("Collector starting")
    log.info("Manual inventory: %s", Path(args.manual_dir).resolve())
    log.info("Output directory: %s", Path(args.output_dir).resolve())
    log.info("UniFi controller: %s", args.unifi_url)
    if args.no_snmp:
        log.info("SNMP: disabled")
    else:
        log.info(
            "SNMP: enabled (timeout=%.1fs, retries=%d)",
            args.snmp_timeout,
            args.snmp_retries,
        )
    log.info("Talos hardware discovery: %s", args.talosctl or "disabled")
    log.info("Kubernetes nodes: %s", args.nodes_json or "live cluster")
    if args.log_file:
        log.info("Log file: %s", Path(args.log_file).expanduser())

    manual = load_manual(Path(args.manual_dir))
    resolver = DeviceResolver(manual)

    if args.from_cluster:
        log.info("UniFi credentials: monitoring/unpoller-unifi Secret")
        username, password = read_cluster_secret(args.kubectl, args.kubeconfig)
    else:
        log.info("UniFi credentials: environment")
        username = os.environ.get("UNIFI_USER", "")
        password = os.environ.get("UNIFI_PASS", "")
    if not username or not password:
        raise InventoryError("set UNIFI_USER/UNIFI_PASS or use --from-cluster")

    log.info("UniFi: fetching inventory")
    devices, networks, clients = unifi_get(
        args.unifi_url.rstrip("/"), username, password)
    log.info(
        "UniFi: fetched %d device(s), %d network(s), %d client(s)",
        len(devices),
        len(networks),
        len(clients),
    )
    log.info("UniFi: building inventory")
    unifi_devices, cables, vlans, prefixes, _names = build_unifi(
        devices, networks, manual)
    log.info(
        "UniFi: built %d device(s), %d cable(s), %d VLAN(s), %d prefix(es)",
        len(unifi_devices),
        len(cables),
        len(vlans),
        len(prefixes),
    )
    _register_unifi_devices(resolver, devices, _names)
    snmp_matches: dict[str, str] = {}
    if not args.no_snmp:
        targets = _snmp_targets(manual, devices, _names, unifi_devices)
        if targets:
            log.info("SNMP: polling %d target(s)", len(targets))
            log.debug(
                "SNMP targets: %s",
                ", ".join(
                    f"{target.get('name')} ({target.get('ip')})" for target in targets
                ),
            )
            observations = asyncio.run(
                collect_snmp(
                    targets,
                    community=args.snmp_community,
                    timeout=args.snmp_timeout,
                    retries=args.snmp_retries,
                )
            )
            snmp_matches = merge_snmp_observations(
                unifi_devices, vlans, observations, resolver, manual
            )
            log.info(
                "SNMP: targets=%d, observed=%d, matched=%d",
                len(targets),
                len(observations),
                len(snmp_matches),
            )
            if args.snmp_report:
                report_path = Path(args.snmp_report)
                write_snmp_report(report_path, observations, snmp_matches)
                log.info("SNMP report: %s", report_path)
        else:
            log.info("SNMP: no targets discovered")
    switch_name = next(
        (
            device["name"]
            for device in unifi_devices
            if device.get("device_type") == "ubiquiti-usw-pro-24-poe"
        ),
        "USW Pro 24 PoE",
    )

    log.info("Talos: reading Kubernetes node list")
    nodes = read_nodes(args.kubectl, args.kubeconfig, args.nodes_json)
    log.info("Talos: %d node(s)", len(nodes))
    manual_by_node = _resolve_talos(
        resolver,
        nodes,
        talosctl=args.talosctl,
        talosconfig=args.talosconfig,
    )
    log.info(
        "Talos: resolved %d/%d node(s) to manual devices",
        len(manual_by_node),
        len(nodes),
    )
    redfish_by_name = _collect_redfish(
        manual,
        resolver,
        username=args.redfish_user,
        password=args.redfish_pass,
    )
    ipmi_by_name = _collect_ipmi(
        manual,
        resolver,
        username=args.ipmi_user,
        password=args.ipmi_pass,
    )
    # Poll Redfish and IPMI separately. If a device exposes both, Redfish data
    # wins because it provides richer component details.
    bmc_by_name = {**ipmi_by_name, **redfish_by_name}

    log.info("Compute: building servers and Talos nodes")
    module_types: dict[str, dict[str, Any]] = {}
    servers = build_servers(
        clients,
        manual,
        switch_name,
        module_types=module_types,
        bmc_by_name=bmc_by_name,
    )
    compute_nodes = build_nodes(
        nodes,
        Path(args.talos_config_dir),
        manual,
        talosctl=args.talosctl,
        talosconfig=args.talosconfig,
        module_types=module_types,
        bmc_by_name=bmc_by_name,
        manual_by_node=manual_by_node,
    )
    module_count = sum(
        len(device.get("modules", [])) for device in [*servers, *compute_nodes]
    )
    log.info(
        "Compute: servers=%d, nodes=%d, module_types=%d, modules=%d",
        len(servers),
        len(compute_nodes),
        len(module_types),
        module_count,
    )

    output_dir = Path(args.output_dir)
    log.info("Writing generated inventory to %s", output_dir.resolve())
    write_yaml(
        output_dir / "unifi.yaml",
        {"devices": unifi_devices, "vlans": vlans,
            "prefixes": prefixes, "cables": cables},
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
            log.info("Removed legacy generated file %s", legacy_path)

    print("Generated inventory summary:")
    print(
        f"  unifi_devices={len(unifi_devices)}, servers={len(servers)}, nodes={len(compute_nodes)}, "
        f"cables={len(cables)}, vlans={len(vlans)}, prefixes={len(prefixes)}, "
        f"module_types={len(module_types)}, modules={module_count}"
    )
    log.info("Collector finished in %.2fs", time.monotonic() - started)
    return 0
