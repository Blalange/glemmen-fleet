"""Build canonical generated device entries from source observations."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from inventory_util import normalize_mac

from .config import DEFAULT_TAG
from .hardware import (
    add_module_component,
    add_redfish_components,
    clean_model,
    disk_bay_type,
    disk_manufacturer,
    infer_memory_manufacturer,
    memory_class,
    normalize_manufacturer,
    register_module_type,
)
from .sources.talos import (
    cpu_cores,
    memory_gib,
    node_bond_members,
    node_disks,
    node_link,
    node_link_macs,
    talos_table,
)


def build_servers(
    clients: list[dict],
    manual: dict[str, Any],
    switch_name: str,
    *,
    module_types: dict[str, dict[str, Any]] | None = None,
    redfish_by_name: dict[str, dict[str, Any]] | None = None,
) -> list[dict]:
    clients_by_ip = {
        str(client.get("ip")): client
        for client in clients
        if client.get("is_wired") and client.get("ip")
    }
    clients_by_mac = {
        normalize_mac(client["mac"]): client
        for client in clients
        if client.get("is_wired") and client.get("mac")
    }
    site_slug = manual["settings"].get("site", "glemmen")
    default_tag = manual["settings"].get("default_tag", DEFAULT_TAG)
    servers: list[dict] = []
    catalog = module_types if module_types is not None else {}
    redfish_data = redfish_by_name or {}

    for device in manual["devices"]:
        if device.get("talos_node") or device.get("role") != "server":
            continue
        ip = str(device.get("unifi_ip") or "")
        claimed_mac = str(device.get("unifi_mac") or "")
        client = clients_by_ip.get(ip) if ip else None
        if client is None and claimed_mac:
            client = clients_by_mac.get(normalize_mac(claimed_mac))
        if not ip and not client:
            continue
        if not ip and client:
            ip = str(client.get("ip") or "")
        if not ip:
            continue
        name = str(device.get("name") or "")
        uplinks = device.get("uplinks") or []
        port = (client or {}).get("sw_port")
        mac = str((client or {}).get("mac") or "")
        unifi_name = str((client or {}).get("name") or (client or {}).get("hostname") or "")
        status = device.get("status") or ("active" if client else "offline")
        device_type = str(device.get("device_type") or "")

        # The management address belongs on the physical NIC that carries the
        # traffic to the switch, not on a virtual "mgmt" interface.
        if uplinks:
            primary_iface = str(uplinks[0].get("interface") or "Port 1")
        else:
            primary_iface = "Port 1"

        interfaces: list[dict] = []
        if uplinks:
            for uplink in uplinks:
                iface_name = str(uplink.get("interface") or "Port 1")
                entry: dict[str, Any] = {
                    "name": iface_name,
                    "type": "1000base-t",
                    "enabled": True,
                    "description": f"Uplink to {uplink.get('switch')} Port {uplink.get('port')}",
                }
                if iface_name == primary_iface:
                    if mac:
                        entry["mac_address"] = mac
                    entry["ip_addresses"] = [
                        {"address": f"{ip}/24", "dns_name": name, "primary": True}
                    ]
                interfaces.append(entry)
        else:
            interfaces.append(
                {
                    "name": "Port 1",
                    "type": "1000base-t",
                    "enabled": True,
                    "description": "Server network uplink",
                    "mac_address": mac or None,
                    "ip_addresses": [{"address": f"{ip}/24", "dns_name": name, "primary": True}],
                }
            )

        description = f"{name} at {ip}"
        if unifi_name:
            description += f" (UniFi client {unifi_name})"
        if port:
            description += f", {switch_name} Port {port}"
        if mac:
            description += f", MAC {mac}"
        if client is None:
            description += "; no active UniFi client"

        module_bays: list[dict] = []
        modules: list[dict] = []
        redfish = redfish_data.get(name)
        if redfish:
            add_redfish_components(module_bays, modules, catalog, redfish, device_type)

        server: dict[str, Any] = {
            "name": name,
            "site": str(device.get("site") or site_slug),
            "role": str(device.get("role") or "server"),
            "device_type": device_type,
            "status": status,
            "serial": str(device.get("serial") or ""),
            "description": description,
            "tags": sorted({default_tag, *[str(tag) for tag in device.get("tags") or []]}),
            "interfaces": interfaces,
        }
        if module_bays:
            server["module_bays"] = module_bays
        if modules:
            server["modules"] = modules
        servers.append(server)

    # Devices that are not UniFi clients or Talos nodes, but do have Redfish
    # observations. They already exist in the manual layer; this sparse entry
    # only attaches the discovered components.
    for device in manual["devices"]:
        if (
            device.get("talos_node")
            or device.get("unifi_ip")
            or device.get("role") != "server"
        ):
            continue
        name = str(device.get("name") or "")
        redfish = redfish_data.get(name)
        if not name or not redfish:
            continue
        module_bays: list[dict] = []
        modules: list[dict] = []
        add_redfish_components(
            module_bays, modules, catalog, redfish, str(device.get("device_type") or "")
        )
        if not modules:
            continue
        servers.append(
            {
                "name": name,
                "site": str(device.get("site") or site_slug),
                "role": str(device.get("role") or "server"),
                "device_type": str(device.get("device_type") or ""),
                "status": str(device.get("status") or "active"),
                "serial": str(device.get("serial") or ""),
                "description": str(device.get("description") or f"{name} via iDRAC Redfish"),
                "tags": sorted(
                    {default_tag, *[str(tag) for tag in device.get("tags") or []]}
                ),
                "module_bays": module_bays,
                "modules": modules,
            }
        )
    return servers

def build_nodes(
    nodes: list[dict],
    talos_dir: Path,
    manual: dict[str, Any],
    *,
    talosctl: str | None = None,
    talosconfig: str | None = None,
    module_types: dict[str, dict[str, Any]] | None = None,
    redfish_by_name: dict[str, dict[str, Any]] | None = None,
    manual_by_node: dict[str, dict[str, Any]] | None = None,
) -> list[dict]:
    result: list[dict] = []
    site_slug = manual["settings"].get("site", "glemmen")
    default_tag = manual["settings"].get("default_tag", DEFAULT_TAG)
    manual_devices = manual["devices"]
    manual_nodes = manual_by_node or {}
    catalog = module_types if module_types is not None else {}
    redfish_data = redfish_by_name or {}

    for node in nodes:
        metadata = node.get("metadata") or {}
        status = node.get("status") or {}
        node_info = status.get("nodeInfo") or {}
        capacity = status.get("capacity") or {}
        labels = metadata.get("labels") or {}
        name = metadata.get("name")
        if not name:
            continue
        manual_device = manual_nodes.get(name)
        if manual_device is None:
            manual_device = next(
                (
                    device
                    for device in manual_devices
                    if device.get("talos_node") == name or device.get("name") == name
                ),
                None,
            )
        if manual_device is None:
            print(f"WARNING: Talos node {name} has no manual device entry", file=sys.stderr)
        device_name = str((manual_device or {}).get("name") or name)

        internal_ip = None
        for address in status.get("addresses") or []:
            if address.get("type") == "InternalIP":
                internal_ip = address.get("address")
                break
        link = node_link(name, talos_dir)
        if link:
            iface_name, iface_address = link
        else:
            iface_name = "eth0"
            iface_address = f"{internal_ip}/24" if internal_ip else None
        if not iface_address:
            continue
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
        role = "control-plane" if "node-role.kubernetes.io/control-plane" in labels else "worker"
        if manual_device and manual_device.get("role"):
            role = str(manual_device["role"])
        device_type = str((manual_device or {}).get("device_type") or "")
        description = (
            f"{node_info.get('osImage', 'Talos')}, Kubernetes "
            f"{node_info.get('kubeletVersion', 'unknown')}, kernel "
            f"{node_info.get('kernelVersion', 'unknown')}, "
            f"{cpu_cores(capacity.get('cpu')):g} CPU, "
            f"{memory_gib(capacity.get('memory')):.1f} GiB RAM"
        )
        interfaces: list[dict] = []
        members = node_bond_members(name, talos_dir)
        macs: dict[str, str] = {}
        if talosctl:
            macs = node_link_macs(talosctl, talosconfig, name)
        for member in members:
            entry: dict[str, Any] = {
                "name": member,
                "type": "1000base-t",
                "enabled": True,
                "description": "bond0 member (LACP)",
            }
            if member in macs:
                entry["mac_address"] = macs[member]
            interfaces.append(entry)
        if members:
            iface_type = "lag"
        elif name == "glemmen80":
            iface_type = "10gbase-x-sfpp"
        else:
            iface_type = "1000base-t"
        primary_entry: dict[str, Any] = {
            "name": iface_name,
            "type": iface_type,
            "enabled": True,
            "description": "In-band management and cluster traffic",
            "ip_addresses": [
                {
                    "address": iface_address,
                    "dns_name": name,
                    "primary": True,
                }
            ],
        }
        if iface_name in macs:
            primary_entry["mac_address"] = macs[iface_name]
        interfaces.append(primary_entry)

        module_bays: list[dict] = []
        modules: list[dict] = []
        bay_disk_type = disk_bay_type(device_type)
        dimm_class = memory_class(device_type)
        redfish = redfish_data.get(device_name) or redfish_data.get(name)

        if redfish:
            add_redfish_components(module_bays, modules, catalog, redfish, device_type)

        if not redfish or not redfish.get("cpu"):
            if talosctl:
                for index, processor in enumerate(
                    talos_table(talosctl, talosconfig, name, "processors"), 1
                ):
                    spec = processor.get("spec") or {}
                    if not spec.get("productName") and not spec.get("coreCount"):
                        continue
                    model = clean_model(str(spec.get("productName") or f"CPU {index}"))
                    manufacturer = normalize_manufacturer(
                        str(spec.get("manufacturer") or "Intel")
                    )
                    module_type = register_module_type(
                        catalog,
                        manufacturer=manufacturer,
                        model=model,
                        profile="CPU",
                        attributes={
                            "cores": int(spec.get("coreCount") or 0),
                            "speed": float(
                                spec.get("bootSpeedMhz") or spec.get("maxSpeedMhz") or 0
                            )
                            / 1000,
                            "architecture": "x86_64",
                        },
                        bay_types=["cpu-socket"],
                        part_number=model,
                        description="Processor",
                    )
                    add_module_component(
                        module_bays,
                        modules,
                        str(spec.get("socket") or f"CPU {index}"),
                        "cpu-socket",
                        "Processor socket.",
                        module_type,
                        description=model,
                    )

        if not redfish or not redfish.get("memory"):
            if talosctl:
                for index, memory in enumerate(
                    talos_table(talosctl, talosconfig, name, "memorymodules"), 1
                ):
                    spec = memory.get("spec") or {}
                    if not spec.get("sizeMiB"):
                        continue
                    locator = str(spec.get("deviceLocator") or f"DIMM {index}")
                    bay_name = locator if locator.upper().startswith("DIMM") else f"DIMM {locator}"
                    model = str(spec.get("productName") or locator)
                    size_gb = int(spec.get("sizeMiB") or 0) // 1024
                    data_rate = int(spec.get("speed") or 0)
                    mem_manufacturer = infer_memory_manufacturer(
                        model, normalize_manufacturer(str(spec.get("manufacturer") or ""))
                    )
                    module_type = register_module_type(
                        catalog,
                        manufacturer=mem_manufacturer,
                        model=model,
                        profile="Memory",
                        attributes={
                            "size": size_gb,
                            "class": dimm_class,
                            "ecc": True,
                            "data_rate": data_rate,
                        },
                        bay_types=[f"dimm-{dimm_class.lower()}"],
                        part_number=model,
                        description="Memory module",
                    )
                    add_module_component(
                        module_bays,
                        modules,
                        bay_name,
                        f"dimm-{dimm_class.lower()}",
                        "Memory module slot.",
                        module_type,
                        serial=str(spec.get("serialNumber") or ""),
                        description=f"{size_gb} GB {dimm_class} {data_rate} MT/s",
                    )

        if not redfish or not redfish.get("disks"):
            if talosctl:
                disks, _system_disk = node_disks(talosctl, talosconfig, name)
                for index, disk in enumerate(disks, 1):
                    size_gb = int(disk["size"] / 1_000_000_000)
                    kind = "HD" if disk["rotational"] else "SSD"
                    model = disk["model"] or f"Disk {index}"
                    module_type = register_module_type(
                        catalog,
                        manufacturer=disk_manufacturer(model),
                        model=model,
                        profile="Hard disk",
                        attributes={"size": size_gb, "type": kind},
                        bay_types=[bay_disk_type],
                        part_number=model,
                        description="Disk drive",
                    )
                    add_module_component(
                        module_bays,
                        modules,
                        f"Drive {index}",
                        bay_disk_type,
                        "Drive bay.",
                        module_type,
                        serial=disk["serial"],
                        description=f"{size_gb} GB {kind}",
                    )

        device_entry: dict[str, Any] = {
            "name": device_name,
            "site": str((manual_device or {}).get("site") or site_slug),
            "role": role,
            "device_type": device_type,
            "platform": "talos-linux",
            "status": "active" if ready else "offline",
            "serial": node_info.get("systemUUID") or "",
            "description": description,
            "tags": sorted({default_tag, *[str(tag) for tag in (manual_device or {}).get("tags") or []]}),
            "interfaces": interfaces,
        }
        if module_bays:
            device_entry["module_bays"] = module_bays
        if modules:
            device_entry["modules"] = modules
        result.append(device_entry)
    return result
