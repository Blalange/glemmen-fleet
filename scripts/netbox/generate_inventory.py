#!/usr/bin/env python3
"""Collect live UniFi and Talos facts into the NetBox generated inventory layer.

The manual layer in ``flux/apps/netbox/inventory/manual`` is the source of truth
for everything that is not discoverable: device models, rack layout, physical
servers, accessories, manual cables and VIPs. This script only writes
``flux/apps/netbox/inventory/generated``; it never edits the manual layer.

Credentials are read from the ``unpoller-unifi`` Secret in the monitoring
namespace with --from-cluster, or from UNIFI_USER / UNIFI_PASS. They are never
written to the output.

Usage:
  uv run --with requests --with PyYAML \
    python scripts/netbox/generate_inventory.py --from-cluster \
    --kubectl ./scripts/bin/kubectl --kubeconfig talos/generated/kubeconfig

  uv run --with requests --with PyYAML \
    python scripts/netbox/generate_inventory.py --nodes-json /tmp/nodes.json
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import requests
import yaml

DEFAULT_MANUAL_DIR = "flux/apps/netbox/inventory/manual"
DEFAULT_OUTPUT_DIR = "flux/apps/netbox/inventory/generated"
DEFAULT_TAG = "production"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def run(cmd: list[str], *, capture: bool = True) -> str:
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=capture,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def kubectl_command(kubectl: str, kubeconfig: str | None, *args: str) -> list[str]:
    cmd = [kubectl]
    if kubeconfig:
        cmd.extend(["--kubeconfig", kubeconfig])
    cmd.extend(args)
    return cmd


def read_cluster_secret(kubectl: str, kubeconfig: str | None) -> tuple[str, str]:
    raw = run(
        kubectl_command(
            kubectl,
            kubeconfig,
            "-n",
            "monitoring",
            "get",
            "secret",
            "unpoller-unifi",
            "-o",
            "json",
        )
    )
    secret = json.loads(raw)
    data = secret.get("data") or {}
    try:
        username = base64.b64decode(data["username"]).decode()
        password = base64.b64decode(data["password"]).decode()
    except KeyError as exc:
        raise SystemExit(f"unpoller-unifi Secret is missing {exc}") from exc
    return username, password


def unifi_get(base: str, username: str, password: str) -> tuple[list[dict], list[dict], list[dict]]:
    session = requests.Session()
    session.headers.update({"User-Agent": "glemmen-fleet-netbox-import/2.0"})
    login = session.post(
        f"{base}/api/auth/login",
        json={"username": username, "password": password, "remember": True},
        timeout=30,
    )
    if login.status_code != 200:
        raise SystemExit(f"UniFi login failed: HTTP {login.status_code}: {login.text[:300]}")

    devices_response = session.get(
        f"{base}/proxy/network/api/s/default/stat/device", timeout=60
    )
    devices_response.raise_for_status()
    networks_response = session.get(
        f"{base}/proxy/network/api/s/default/rest/networkconf", timeout=60
    )
    networks_response.raise_for_status()
    clients_response = session.get(
        f"{base}/proxy/network/api/s/default/stat/sta", timeout=60
    )
    clients_response.raise_for_status()
    return (
        devices_response.json().get("data") or [],
        networks_response.json().get("data") or [],
        clients_response.json().get("data") or [],
    )


def read_nodes(kubectl: str, kubeconfig: str | None, nodes_json: str | None) -> list[dict]:
    if nodes_json:
        return json.loads(Path(nodes_json).read_text(encoding="utf-8-sig"))["items"]
    raw = run(kubectl_command(kubectl, kubeconfig, "get", "nodes", "-o", "json"))
    return json.loads(raw)["items"]


def load_manual(directory: Path) -> dict[str, Any]:
    if not directory.is_dir():
        raise SystemExit(f"manual inventory directory {directory} does not exist")

    manual: dict[str, Any] = {
        "settings": {},
        "unifi_models": {},
        "unifi_vlan_names": {},
        "defaults": {},
        "devices": [],
    }
    files: set[Path] = set()
    for pattern in ("*.yaml", "*.yml"):
        for path in directory.rglob(pattern):
            if any(part.startswith(".") for part in path.relative_to(directory).parts):
                continue
            files.add(path.resolve())

    for path in sorted(files):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if document is None:
                continue
            if not isinstance(document, dict):
                raise SystemExit(f"{path}: every YAML document must be a mapping")
            for key, value in document.items():
                if key in {"layer", "settings", "unifi_models", "unifi_vlan_names"}:
                    if key == "settings" and isinstance(value, dict):
                        manual["settings"].update(value)
                    elif key == "unifi_models" and isinstance(value, dict):
                        manual["unifi_models"].update(value)
                    elif key == "unifi_vlan_names" and isinstance(value, dict):
                        manual["unifi_vlan_names"].update(value)
                    continue
                if key == "defaults":
                    if not isinstance(value, dict):
                        raise SystemExit(f"{path}: defaults must be a mapping")
                    manual["defaults"].update(value)
                elif key == "devices":
                    if not isinstance(value, list):
                        raise SystemExit(f"{path}: devices must be a list")
                    manual["devices"].extend(value)
                elif key in {"sites", "manufacturers", "platforms", "device_roles", "device_types", "racks", "vlans", "prefixes", "ip_addresses", "cables"}:
                    # Non-device manual sections are consumed by sync.py. The
                    # collector only needs settings, device types and devices.
                    continue

    for device in manual["devices"]:
        for key, value in manual["defaults"].items():
            if key == "tags":
                device["tags"] = sorted({*[str(tag) for tag in device.get("tags") or []], *[str(tag) for tag in value or []]})
            elif key not in device:
                device[key] = value

    manual["devices_by_name"] = {
        str(device.get("name")): device
        for device in manual["devices"]
        if device.get("name")
    }
    manual["device_types_by_slug"] = {
        str(item.get("slug")): item
        for item in _read_manual_section(directory, "device_types")
        if item.get("slug")
    }
    return manual


def _read_manual_section(directory: Path, section: str) -> list[dict]:
    result: list[dict] = []
    for pattern in ("*.yaml", "*.yml"):
        for path in sorted(directory.rglob(pattern)):
            if any(part.startswith(".") for part in path.relative_to(directory).parts):
                continue
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if isinstance(document, dict) and isinstance(document.get(section), list):
                    result.extend(document[section])
    return result


def node_link(node_name: str, talos_dir: Path) -> tuple[str, str] | None:
    path = talos_dir / f"{node_name}.yaml"
    if not path.is_file():
        return None
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(document, dict):
            continue
        if document.get("kind") not in {"LinkConfig", "BondConfig"}:
            continue
        addresses = document.get("addresses") or []
        name = document.get("name")
        if name and addresses:
            address = addresses[0].get("address")
            if address:
                return str(name), str(address)
    return None


def node_bond_members(node_name: str, talos_dir: Path) -> list[str]:
    path = talos_dir / f"{node_name}.yaml"
    if not path.is_file():
        return []
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(document, dict) and document.get("kind") == "BondConfig":
            return [str(item) for item in document.get("links") or []]
    return []


def cpu_cores(value: str | None) -> float:
    if not value:
        return 0.0
    if value.endswith("m"):
        return float(value[:-1]) / 1000
    return float(value)


def memory_gib(value: str | None) -> float:
    if not value:
        return 0.0
    units = {"Ki": 1 / 1024 / 1024, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024}
    for unit, factor in units.items():
        if value.endswith(unit):
            return float(value[: -len(unit)]) * factor
    return float(value) / 1024 / 1024


def unifi_device_name(device: dict, counts: Counter[str]) -> str:
    mac = str(device.get("mac") or "").replace(":", "")
    raw = (device.get("name") or device.get("hostname") or "").strip()
    if not raw:
        raw = f"UniFi device {mac[-4:]}"
    if counts[raw] > 1:
        return f"{raw} ({mac[-4:].lower()})"
    return raw


def unifi_status(device: dict) -> str:
    state = device.get("state")
    if state == 1:
        return "active"
    if state == 2:
        return "inventory"
    return "offline"


def interface_type(port: dict) -> str:
    media = str(port.get("media") or "").upper()
    speed = int(port.get("speed") or 0)
    if "SFP+" in media or "10G" in media:
        return "10gbase-x-sfpp"
    if media == "SFP":
        return "1000base-x-sfp"
    if media in {"2P5GE", "2.5GE"} or speed == 2500:
        return "2.5gbase-t"
    if media == "10GE" or speed >= 10000:
        return "10gbase-t"
    return "1000base-t"


def port_description(port: dict) -> str:
    up = port.get("up")
    if up is True:
        state = "Up"
    elif up is False:
        state = "Down"
    else:
        state = "Unknown"
    speed = int(port.get("speed") or 0)
    speed_text = f"{speed / 1000:g} Gbps" if speed else "no link"
    poe = " PoE" if port.get("port_poe") else ""
    return f"{state}, {speed_text}{poe}"


def build_unifi(
    devices: list[dict],
    networks: list[dict],
    manual: dict[str, Any],
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict[str, str]]:
    raw_names = [
        (d.get("name") or d.get("hostname") or f"UniFi device {str(d.get('mac') or '')[-4:]}").strip()
        for d in devices
    ]
    counts = Counter(raw_names)
    names = {str(d.get("mac")).upper(): unifi_device_name(d, counts) for d in devices}
    by_mac = {str(d.get("mac")).upper(): d for d in devices}
    model_map = manual.get("unifi_models") or {}
    device_types = manual.get("device_types_by_slug") or {}
    site_slug = manual["settings"].get("site", "glemmen")
    default_tag = manual["settings"].get("default_tag", DEFAULT_TAG)

    unifi_devices: list[dict] = []
    cables: list[dict] = []
    cable_endpoints: set[tuple[str, str]] = set()

    for d in devices:
        mac = str(d.get("mac") or "").upper()
        name = names[mac]
        model = str(d.get("model") or "")
        slug = model_map.get(model) or f"ubiquiti-{slugify(model or 'unifi-device')}"
        device_type = device_types.get(slug) or {}
        model_name = device_type.get("model") or model or "UniFi device"
        role = {"uap": "access-point", "usw": "switch", "udm": "gateway"}.get(
            str(d.get("type")), "switch"
        )
        description = (
            f"UniFi {model_name} ({model}), firmware {d.get('version') or 'unknown'}, "
            f"MAC {d.get('mac')}"
        )
        interfaces: list[dict] = []
        ports = d.get("port_table") or []
        port_by_idx: dict[int, dict] = {}

        for port in ports:
            try:
                idx = int(port.get("port_idx"))
            except (TypeError, ValueError):
                continue
            port_by_idx[idx] = port
            interfaces.append(
                {
                    "name": f"Port {idx}",
                    "type": interface_type(port),
                    "enabled": True,
                    "description": port_description(port),
                    "mtu": None,
                    "mode": "",
                    "untagged_vlan": None,
                    "tagged_vlans": [],
                }
            )

        if not ports:
            interfaces.append(
                {
                    "name": "Port 1",
                    "type": "1000base-t",
                    "enabled": True,
                    "description": "Uplink Ethernet port",
                }
            )

        management_ips: list[dict] = []
        if role == "gateway":
            lan_ip = d.get("lan_ip")
            if lan_ip:
                management_ips.append({"address": f"{lan_ip}/24", "primary": True})
            for network in networks:
                subnet = network.get("ip_subnet")
                if subnet and network.get("purpose") in {"corporate", "remote-user-vpn"}:
                    address = str(ipaddress.ip_interface(subnet))
                    if not any(item["address"] == address for item in management_ips):
                        management_ips.append({"address": address})
            interfaces.insert(
                0,
                {
                    "name": "mgmt",
                    "type": "virtual",
                    "mgmt_only": True,
                    "enabled": True,
                    "description": "LAN and VPN gateway addresses",
                    "ip_addresses": management_ips,
                },
            )
            wan = d.get("wan1") or {}
            if wan.get("ip") and wan.get("netmask") and 9 in port_by_idx:
                wan_address = str(ipaddress.ip_interface(f"{wan['ip']}/{wan['netmask']}"))
                for interface in interfaces:
                    if interface["name"] == "Port 9":
                        interface["description"] = "WAN uplink"
                        interface["ip_addresses"] = [{"address": wan_address, "description": "WAN"}]
                        break
        elif role == "switch":
            mgmt_ip = d.get("ip") or (d.get("config_network") or {}).get("ip")
            if mgmt_ip:
                management_ips.append({"address": f"{mgmt_ip}/24", "primary": True})
            interfaces.insert(
                0,
                {
                    "name": "mgmt",
                    "type": "virtual",
                    "mgmt_only": True,
                    "enabled": True,
                    "description": "Switch management address",
                    "ip_addresses": management_ips,
                },
            )
        else:
            mgmt_ip = d.get("ip") or (d.get("config_network") or {}).get("ip")
            if mgmt_ip and interfaces:
                interfaces[0].setdefault("ip_addresses", []).append(
                    {"address": f"{mgmt_ip}/24", "primary": True}
                )
            for radio in d.get("radio_table") or []:
                radio_name = str(radio.get("name") or "wifi")
                band = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}.get(
                    str(radio.get("radio")), str(radio.get("radio"))
                )
                interfaces.append(
                    {
                        "name": radio_name,
                        "type": "ieee802.11ax",
                        "enabled": True,
                        "description": f"{band} radio",
                    }
                )

        unifi_devices.append(
            {
                "name": name,
                "site": site_slug,
                "role": role,
                "device_type": slug,
                "platform": "unifi",
                "status": unifi_status(d),
                "serial": d.get("serial") or "",
                "description": description,
                "tags": [default_tag],
                "interfaces": interfaces,
            }
        )

    for d in sorted(devices, key=lambda item: (item.get("state") != 1, str(item.get("mac")))):
        mac = str(d.get("mac") or "").upper()
        child_name = names[mac]
        uplink = d.get("uplink") or {}
        parent_mac = str(uplink.get("uplink_mac") or "").upper()
        parent = by_mac.get(parent_mac)
        if not parent:
            continue
        parent_name = names[parent_mac]
        local_idx = uplink.get("port_idx")
        remote_idx = uplink.get("uplink_remote_port")
        if local_idx is None or remote_idx is None:
            continue
        local_if = f"Port {int(local_idx)}"
        parent_if = f"Port {int(remote_idx)}"
        if (parent_name, parent_if) in cable_endpoints or (child_name, local_if) in cable_endpoints:
            continue
        cable_endpoints.add((parent_name, parent_if))
        cable_endpoints.add((child_name, local_if))
        speed = int(uplink.get("speed") or 0)
        cables.append(
            {
                "a_device": child_name,
                "a_interface": local_if,
                "b_device": parent_name,
                "b_interface": parent_if,
                "type": "cat6a" if speed > 1000 else "cat6",
                "status": "connected",
                "description": f"UniFi uplink to {parent_name} {parent_if}",
            }
        )

    # UniFi VLAN names that need correction are configured in the manual layer.
    vlan_names = {int(key): str(value) for key, value in (manual.get("unifi_vlan_names") or {}).items()}
    vlans, prefixes = build_networks(networks, next((d for d in devices if d.get("type") == "udm"), None), vlan_names, site_slug)
    return unifi_devices, cables, vlans, prefixes, names


def build_networks(
    networks: list[dict],
    udm: dict | None,
    vlan_names: dict[int, str],
    site_slug: str,
) -> tuple[list[dict], list[dict]]:
    vlans: dict[int, dict] = {}
    prefixes: dict[str, dict] = {}
    for network in networks:
        vlan_id = network.get("vlan")
        name = str(network.get("name") or "").strip()
        if vlan_id is not None and int(vlan_id) in vlan_names:
            name = vlan_names[int(vlan_id)]
        purpose = network.get("purpose")
        if vlan_id is not None:
            vlans[int(vlan_id)] = {
                "vid": int(vlan_id),
                "name": name,
                "site": site_slug,
                "status": "active",
                "description": f"UniFi network: {name}",
            }
        elif name == "Default":
            vlans[1] = {
                "vid": 1,
                "name": "Default",
                "site": site_slug,
                "status": "active",
                "description": "UniFi Default network (native VLAN).",
            }
        subnet = network.get("ip_subnet")
        if not subnet:
            continue
        try:
            prefix = str(ipaddress.ip_interface(subnet).network)
        except ValueError:
            continue
        if prefix in prefixes:
            continue
        entry: dict[str, Any] = {
            "prefix": prefix,
            "site": site_slug,
            "status": "active",
            "description": f"UniFi {name} ({purpose or 'network'})",
        }
        if name == "Default":
            entry["vlan"] = 1
        elif vlan_id is not None:
            entry["vlan"] = int(vlan_id)
        prefixes[prefix] = entry

    if udm:
        wan = udm.get("wan1") or {}
        if wan.get("ip") and wan.get("netmask"):
            try:
                prefix = str(ipaddress.ip_interface(f"{wan['ip']}/{wan['netmask']}").network)
                prefixes.setdefault(
                    prefix,
                    {
                        "prefix": prefix,
                        "site": site_slug,
                        "status": "active",
                        "description": "WAN (UDM Pro)",
                    },
                )
            except ValueError:
                pass

    return list(vlans.values()), list(prefixes.values())


def build_servers(
    clients: list[dict],
    manual: dict[str, Any],
    switch_name: str,
) -> list[dict]:
    clients_by_ip = {
        str(client.get("ip")): client
        for client in clients
        if client.get("is_wired") and client.get("ip")
    }
    site_slug = manual["settings"].get("site", "glemmen")
    default_tag = manual["settings"].get("default_tag", DEFAULT_TAG)
    servers: list[dict] = []

    for device in manual["devices"]:
        ip = device.get("unifi_ip")
        if not ip or device.get("talos_node"):
            continue
        name = str(device.get("name") or "")
        client = clients_by_ip.get(str(ip))
        uplinks = device.get("uplinks") or []
        port = (client or {}).get("sw_port")
        mac = str((client or {}).get("mac") or "")
        unifi_name = str((client or {}).get("name") or (client or {}).get("hostname") or "")
        status = device.get("status") or ("active" if client else "offline")

        interfaces: list[dict] = [
            {
                "name": "mgmt",
                "type": "virtual",
                "mgmt_only": True,
                "enabled": True,
                "description": "Management address",
                "ip_addresses": [{"address": f"{ip}/24", "primary": True}],
            }
        ]
        if uplinks:
            for uplink in uplinks:
                interfaces.append(
                    {
                        "name": str(uplink.get("interface") or "Port 1"),
                        "type": "1000base-t",
                        "enabled": True,
                        "description": f"Uplink to {uplink.get('switch')} Port {uplink.get('port')}",
                    }
                )
        else:
            interfaces.append(
                {
                    "name": "Port 1",
                    "type": "1000base-t",
                    "enabled": True,
                    "description": "Server network uplink",
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

        servers.append(
            {
                "name": name,
                "site": str(device.get("site") or site_slug),
                "role": str(device.get("role") or "server"),
                "device_type": str(device.get("device_type") or ""),
                "status": status,
                "serial": str(device.get("serial") or ""),
                "description": description,
                "tags": sorted({default_tag, *[str(tag) for tag in device.get("tags") or []]}),
                "interfaces": interfaces,
            }
        )
    return servers


def build_nodes(nodes: list[dict], talos_dir: Path, manual: dict[str, Any]) -> list[dict]:
    result: list[dict] = []
    site_slug = manual["settings"].get("site", "glemmen")
    default_tag = manual["settings"].get("default_tag", DEFAULT_TAG)
    manual_devices = manual["devices"]

    for node in nodes:
        metadata = node.get("metadata") or {}
        status = node.get("status") or {}
        node_info = status.get("nodeInfo") or {}
        capacity = status.get("capacity") or {}
        labels = metadata.get("labels") or {}
        name = metadata.get("name")
        if not name:
            continue
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
        description = (
            f"{node_info.get('osImage', 'Talos')}, Kubernetes "
            f"{node_info.get('kubeletVersion', 'unknown')}, kernel "
            f"{node_info.get('kernelVersion', 'unknown')}, "
            f"{cpu_cores(capacity.get('cpu')):g} CPU, "
            f"{memory_gib(capacity.get('memory')):.1f} GiB RAM"
        )
        interfaces: list[dict] = []
        members = node_bond_members(name, talos_dir)
        for member in members:
            interfaces.append(
                {
                    "name": member,
                    "type": "1000base-t",
                    "enabled": True,
                    "description": "bond0 member (LACP)",
                }
            )
        if members:
            iface_type = "lag"
        elif name == "glemmen80":
            iface_type = "10gbase-x-sfpp"
        else:
            iface_type = "1000base-t"
        interfaces.append(
            {
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
        )
        result.append(
            {
                "name": name,
                "site": str((manual_device or {}).get("site") or site_slug),
                "role": role,
                "device_type": str((manual_device or {}).get("device_type") or ""),
                "platform": "talos-linux",
                "status": "active" if ready else "offline",
                "serial": node_info.get("systemUUID") or "",
                "description": description,
                "tags": sorted({default_tag, *[str(tag) for tag in (manual_device or {}).get("tags") or []]}),
                "interfaces": interfaces,
            }
        )
    return result


def write_yaml(path: Path, sections: dict[str, Any], title: str) -> None:
    header = (
        f"# Generated by scripts/netbox/generate_inventory.py from the live UniFi\n"
        f"# controller and Kubernetes/Talos node data. Do not hand-edit this file.\n"
        f"# {title}\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        header + yaml.safe_dump({"layer": "generated", **sections}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual-dir", default=DEFAULT_MANUAL_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--talos-config-dir", default="talos/nodes")
    parser.add_argument("--kubectl", default=os.environ.get("KUBECTL", "kubectl"))
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    parser.add_argument("--nodes-json", help="use a saved kubectl get nodes -o json file")
    parser.add_argument("--from-cluster", action="store_true", help="read credentials from unpoller-unifi")
    parser.add_argument("--unifi-url", default=os.environ.get("UNIFI_URL", "https://unifi.glem.men"))
    args = parser.parse_args(argv)

    manual = load_manual(Path(args.manual_dir))

    if args.from_cluster:
        username, password = read_cluster_secret(args.kubectl, args.kubeconfig)
    else:
        username = os.environ.get("UNIFI_USER", "")
        password = os.environ.get("UNIFI_PASS", "")
    if not username or not password:
        raise SystemExit("set UNIFI_USER/UNIFI_PASS or use --from-cluster")

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
    servers = build_servers(clients, manual, switch_name)
    nodes = build_nodes(read_nodes(args.kubectl, args.kubeconfig, args.nodes_json), Path(args.talos_config_dir), manual)

    output_dir = Path(args.output_dir)
    write_yaml(
        output_dir / "unifi.yaml",
        {"devices": unifi_devices, "vlans": vlans, "prefixes": prefixes, "cables": cables},
        "UniFi adopted devices, VLANs, prefixes and uplink cables.",
    )
    write_yaml(
        output_dir / "servers.yaml",
        {"devices": servers},
        "Physical servers discovered through UniFi clients.",
    )
    write_yaml(
        output_dir / "talos.yaml",
        {"devices": nodes},
        "Talos/Kubernetes nodes.",
    )

    print(f"Wrote {output_dir}/unifi.yaml, servers.yaml, talos.yaml")
    print(
        f"  unifi_devices={len(unifi_devices)}, servers={len(servers)}, nodes={len(nodes)}, "
        f"cables={len(cables)}, vlans={len(vlans)}, prefixes={len(prefixes)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
