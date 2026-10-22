#!/usr/bin/env python3
"""Generate the NetBox inventory from the live UniFi controller and cluster.

The generated YAML is consumed by flux/apps/netbox/sync.py. It contains:

  * the UniFi adopted devices (UDM, switches, APs) with their ports/radios,
    management IPs and uplink cables;
  * the Talos/Kubernetes nodes with their real addresses, versions, roles and
    capacity;
  * the manually documented Zyxel GS1920-24, VLANs and prefixes from UniFi;
  * the cluster VIPs.

Credentials are read from the `unpoller-unifi` Secret in the monitoring
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
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import requests
import yaml

SITE_NAME = "Glemmen"
SITE_SLUG = "glemmen"
DEFAULT_TAG = "production"
RACK_NAME = "Glemmen rack"
RACK_TOP = 42

# Physical rack order, top to bottom. The 240..100 row is ordered by the
# server number encoded in its UniFi name, followed by the remaining machines.
RACK_SERVER_IPS = (
    "192.168.6.181",  # 240emy
    "192.168.6.230",  # ubuntu-server
    "192.168.6.133",  # server220
    "192.168.6.68",   # glemmen210
    "192.168.6.190",  # server190
    "192.168.6.140",  # LeanderWin10 (140)
    "192.168.6.120",  # server120
    "192.168.6.100",  # dellserver01
)
EXTRA_SERVER_IPS = (*RACK_SERVER_IPS, "192.168.6.70")

SERVER_NAME_OVERRIDES = {
    "192.168.6.181": "240emy",
    "192.168.6.230": "ubuntu-server",
    "192.168.6.133": "server220",
    "192.168.6.68": "glemmen210",
    "192.168.6.190": "server190",
    "192.168.6.140": "LeanderWin10",
    "192.168.6.120": "server120",
    "192.168.6.100": "dellserver01",
    "192.168.6.70": "MinecraftServer",
}

# 2U machines in the bottom of the rack.
TWO_U_NODES = {"glemmen50", "glemmen60", "glemmen80"}
TWO_U_SERVER_IPS = {"192.168.6.70"}
TWO_U_RACK_NAMES = {*TWO_U_NODES, "MinecraftServer"}

# Server NIC -> USW Pro 24 PoE port. The 1xx nodes bond eno8303/eno8403.
SERVER_PORTS = {
    "glemmen50": {"enp11s0f0": 3},
    "glemmen60": {"eno1": 4},
    "glemmen130": {"eno8303": 5, "eno8403": 6},
    "glemmen150": {"eno8303": 7, "eno8403": 8},
    "glemmen160": {"eno8303": 9, "eno8403": 10},
    "glemmen170": {"eno8303": 11, "eno8403": 12},
    "glemmen180": {"eno8303": 13, "eno8403": 14},
    "glemmen80": {"enp4s0f0": 26},
}

MODELS: dict[str, dict[str, Any]] = {
    "UDMPRO": {
        "name": "UniFi Dream Machine Pro",
        "slug": "ubiquiti-udm-pro",
        "u_height": 1,
        "is_full_depth": True,
    },
    "USWED35": {
        "name": "USW Flex 2.5G 5",
        "slug": "ubiquiti-usw-flex-2-5g-5",
        "u_height": 1,
        "is_full_depth": False,
    },
    "USWED37": {
        "name": "USW Flex 2.5G 8 PoE",
        "slug": "ubiquiti-usw-flex-2-5g-8-poe",
        "u_height": 1,
        "is_full_depth": False,
    },
    "USMINI": {
        "name": "USW Flex Mini",
        "slug": "ubiquiti-usw-flex-mini",
        "u_height": 1,
        "is_full_depth": False,
    },
    "US24PRO": {
        "name": "USW Pro 24 PoE",
        "slug": "ubiquiti-usw-pro-24-poe",
        "u_height": 1,
        "is_full_depth": True,
    },
    "US8P150": {
        "name": "US 8 PoE 150W",
        "slug": "ubiquiti-us-8-poe-150w",
        "u_height": 1,
        "is_full_depth": True,
    },
    "UAP6MP": {
        "name": "U6 Pro",
        "slug": "ubiquiti-u6-pro",
        "u_height": 1,
        "is_full_depth": False,
    },
}


def slugify(value: str) -> str:
    import re

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
    session.headers.update({"User-Agent": "glemmen-fleet-netbox-import/1.0"})
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


def model_info(code: str) -> dict[str, Any]:
    info = MODELS.get(code)
    if info:
        return info
    return {
        "name": code or "UniFi device",
        "slug": f"ubiquiti-{slugify(code or 'unifi-device')}",
        "u_height": 1,
        "is_full_depth": False,
    }


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


def unifi_device_name(device: dict, counts: Counter[str]) -> str:
    mac = str(device.get("mac") or "").replace(":", "")
    raw = (device.get("name") or device.get("hostname") or "").strip()
    if not raw:
        raw = f"{model_info(device.get('model'))['name']} {mac[-4:]}"
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


def build_unifi(
    devices: list[dict],
    networks: list[dict],
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], list[dict]]:
    raw_names = [
        (d.get("name") or d.get("hostname") or model_info(d.get("model"))["name"]).strip()
        for d in devices
    ]
    counts = Counter(raw_names)
    names = {str(d.get("mac")).upper(): unifi_device_name(d, counts) for d in devices}
    by_mac = {str(d.get("mac")).upper(): d for d in devices}

    device_types: dict[str, dict] = {}
    unifi_devices: list[dict] = []
    cables: list[dict] = []
    cable_endpoints: set[tuple[str, str]] = set()

    for d in devices:
        mac = str(d.get("mac") or "").upper()
        name = names[mac]
        model = str(d.get("model") or "")
        info = model_info(model)
        device_types.setdefault(
            info["slug"],
            {
                "manufacturer": "ubiquiti",
                "model": info["name"],
                "slug": info["slug"],
                "u_height": info["u_height"],
                "is_full_depth": info["is_full_depth"],
                "description": f"UniFi model {model}.",
            },
        )
        device_type = "uap" if d.get("type") == "uap" else "usw" if d.get("type") == "usw" else "udm"
        role = {"uap": "access-point", "usw": "switch", "udm": "gateway"}[device_type]
        description = (
            f"UniFi {info['name']} ({model}), firmware {d.get('version') or 'unknown'}, "
            f"MAC {d.get('mac')}"
        )
        interfaces: list[dict] = []
        ports = d.get("port_table") or []
        port_by_idx: dict[int, dict] = {}
        iface_names_by_idx: dict[int, str] = {}

        for port in ports:
            try:
                idx = int(port.get("port_idx"))
            except (TypeError, ValueError):
                continue
            port_by_idx[idx] = port
            iface_name = f"Port {idx}"
            iface_names_by_idx[idx] = iface_name
            interfaces.append(
                {
                    "name": iface_name,
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
            iface_names_by_idx[1] = "Port 1"
            interfaces.append(
                {
                    "name": "Port 1",
                    "type": "1000base-t",
                    "enabled": True,
                    "description": "Uplink Ethernet port",
                }
            )

        management_ips: list[dict] = []
        if device_type == "udm":
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
                        interface["ip_addresses"] = [
                            {"address": wan_address, "description": "WAN"}
                        ]
                        break
        elif device_type == "usw":
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
            radios = d.get("radio_table") or []
            for radio in radios:
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
                "site": SITE_SLUG,
                "role": role,
                "device_type": info["slug"],
                "platform": "unifi",
                "status": unifi_status(d),
                "serial": d.get("serial") or "",
                "description": description,
                "tags": [DEFAULT_TAG],
                "interfaces": interfaces,
            }
        )

    # Prefer active devices when two stale uplink records point at the same
    # parent port (offline Flex Minis often do this).
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
        # A parent port can only carry one cable. UniFi's stale uplink records
        # can point two offline devices at the same port; keep the first one.
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

    return unifi_devices, list(device_types.values()), cables


def network_name(network: dict) -> str:
    name = str(network.get("name") or "").strip()
    # The UniFi controller currently stores the VLAN 4 name with a broken
    # replacement character. Keep NetBox's name correct.
    if name.startswith("Server tilbeh"):
        return "Server tilbehør"
    return name


def build_networks(
    networks: list[dict], udm: dict | None
) -> tuple[list[dict], list[dict]]:
    vlans: dict[int, dict] = {}
    prefixes: dict[str, dict] = {}
    for network in networks:
        vlan_id = network.get("vlan")
        name = network_name(network)
        purpose = network.get("purpose")
        if vlan_id is not None:
            vlans[int(vlan_id)] = {
                "vid": int(vlan_id),
                "name": name,
                "site": SITE_SLUG,
                "status": "active",
                "description": f"UniFi network: {name}",
            }
        elif name == "Default":
            vlans[1] = {
                "vid": 1,
                "name": "Default",
                "site": SITE_SLUG,
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
            "site": SITE_SLUG,
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
                        "site": SITE_SLUG,
                        "status": "active",
                        "description": "WAN (UDM Pro)",
                    },
                )
            except ValueError:
                pass

    return list(vlans.values()), list(prefixes.values())


def build_servers(
    clients: list[dict],
    usw_name: str,
    existing_names: set[str],
    switch_ports_by_ip: dict[str, dict],
) -> tuple[list[dict], dict[str, int]]:
    clients_by_ip = {
        str(client.get("ip")): client
        for client in clients
        if client.get("is_wired") and client.get("ip")
    }
    servers: list[dict] = []
    server_ports: dict[str, int] = {}
    for ip in EXTRA_SERVER_IPS:
        client = clients_by_ip.get(ip)
        port_info = switch_ports_by_ip.get(ip) or {}
        if client is None and not port_info:
            print(f"WARNING: no UniFi client or switch port for server {ip}", file=sys.stderr)
            continue
        name = str(
            SERVER_NAME_OVERRIDES.get(ip)
            or (client or {}).get("hostname")
            or (client or {}).get("name")
            or f"server{ip.rsplit('.', 1)[-1]}"
        )
        if name in existing_names:
            name = f"{name}-{ip.rsplit('.', 1)[-1]}"
        port = (client or {}).get("sw_port") or port_info.get("port")
        if port is None:
            print(f"WARNING: no switch port for server {name} ({ip})", file=sys.stderr)
            continue
        port = int(port)
        server_ports[name] = port
        mac = str((client or {}).get("mac") or port_info.get("mac") or "")
        active = client is not None or bool(port_info.get("connected"))
        servers.append(
            {
                "name": name,
                "site": SITE_SLUG,
                "role": "server",
                "device_type": (
                    "dell-server-2u" if ip in TWO_U_SERVER_IPS else "dell-server"
                ),
                "status": "active" if active else "offline",
                "serial": "",
                "description": (
                    f"Dell server at {ip} (UniFi client {name}), "
                    f"USW Pro 24 PoE Port {port}, MAC {mac}"
                ),
                "tags": [DEFAULT_TAG],
                "interfaces": [
                    {
                        "name": "mgmt",
                        "type": "virtual",
                        "mgmt_only": True,
                        "enabled": True,
                        "description": "Management address",
                        "ip_addresses": [{"address": f"{ip}/24", "primary": True}],
                    },
                    {
                        "name": "Port 1",
                        "type": "1000base-t",
                        "enabled": True,
                        "description": f"Uplink to {usw_name} Port {port}",
                    },
                ],
            }
        )
    return servers, server_ports


def build_nodes(nodes: list[dict], talos_dir: Path) -> list[dict]:
    result: list[dict] = []
    for node in nodes:
        metadata = node.get("metadata") or {}
        status = node.get("status") or {}
        node_info = status.get("nodeInfo") or {}
        capacity = status.get("capacity") or {}
        labels = metadata.get("labels") or {}
        name = metadata.get("name")
        if not name:
            continue
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
                "site": SITE_SLUG,
                "role": role,
                "device_type": (
                    "talos-node-2u" if name in TWO_U_NODES else "talos-node"
                ),
                "platform": "talos-linux",
                "status": "active" if ready else "offline",
                "serial": node_info.get("systemUUID") or "",
                "description": description,
                "tags": [DEFAULT_TAG],
                "interfaces": interfaces,
            }
        )
    return result


def build_zyxel() -> dict:
    interfaces: list[dict] = [
        {
            "name": "mgmt",
            "type": "virtual",
            "mgmt_only": True,
            "description": "VLAN 4 management address",
            "ip_addresses": [
                {"address": "192.168.4.2/24", "primary": True}
            ],
        }
    ]
    for port in range(1, 29):
        description = "Spare access port."
        if port == 1:
            description = "iDRAC DL51JZ3."
        elif port == 2:
            description = "iDRAC FL51JZ3."
        elif port == 3:
            description = "iDRAC 4L51JZ3."
        elif port == 4:
            description = "iDRAC 6L51JZ3."
        elif port == 5:
            description = "iDRAC 2L51JZ3."
        elif port == 6:
            description = "Server accessory (MAC 5c:f3:fc:de:12:3d)."
        elif port == 7:
            description = "Spare access port (link up, no MAC learned)."
        elif port == 8:
            description = "Management laptop / access device."
        elif port == 28:
            description = "Uplink to UDM Pro Port 8 (VLAN 1 native, VLAN 4 tagged)."
        interface: dict[str, Any] = {
            "name": f"Port {port}",
            "type": "1000base-x-sfp" if port >= 25 else "1000base-t",
            "enabled": True,
            "description": description,
        }
        if port == 28:
            interface.update(
                {"mode": "tagged", "untagged_vlan": 1, "tagged_vlans": [4]}
            )
        else:
            interface["untagged_vlan"] = 4
        interfaces.append(interface)
    return {
        "name": "zyxel-gs1920-24",
        "site": SITE_SLUG,
        "role": "switch",
        "device_type": "zyxel-gs1920-24",
        "status": "active",
        "description": "Zyxel GS1920-24, firmware V4.10(AAOB.5); server-accessory switch on VLAN 4.",
        "tags": [DEFAULT_TAG],
        "interfaces": interfaces,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="flux/apps/netbox/inventory/inventory.yaml")
    parser.add_argument("--talos-config-dir", default="talos/nodes")
    parser.add_argument("--kubectl", default=os.environ.get("KUBECTL", "kubectl"))
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    parser.add_argument("--nodes-json", help="use a saved kubectl get nodes -o json file")
    parser.add_argument("--from-cluster", action="store_true", help="read credentials from unpoller-unifi")
    parser.add_argument("--unifi-url", default=os.environ.get("UNIFI_URL", "https://unifi.glem.men"))
    args = parser.parse_args(argv)

    if args.from_cluster:
        username, password = read_cluster_secret(args.kubectl, args.kubeconfig)
    else:
        username = os.environ.get("UNIFI_USER", "")
        password = os.environ.get("UNIFI_PASS", "")
    if not username or not password:
        raise SystemExit("set UNIFI_USER/UNIFI_PASS or use --from-cluster")

    devices, networks, clients = unifi_get(args.unifi_url.rstrip("/"), username, password)
    unifi_devices, device_types, cables = build_unifi(devices, networks)
    udm = next((d for d in devices if d.get("type") == "udm"), None)
    vlans, prefixes = build_networks(networks, udm)
    nodes = build_nodes(read_nodes(args.kubectl, args.kubeconfig, args.nodes_json), Path(args.talos_config_dir))
    usw_name = next(
        d["name"] for d in unifi_devices if d["device_type"] == "ubiquiti-usw-pro-24-poe"
    )
    usw = next(device for device in devices if device.get("model") == "US24PRO")
    switch_ports_by_ip: dict[str, dict] = {}
    for port in usw.get("port_table") or []:
        last_connection = port.get("last_connection") or {}
        if not last_connection.get("ip") or port.get("port_idx") is None:
            continue
        switch_ports_by_ip[str(last_connection["ip"])] = {
            "port": int(port["port_idx"]),
            "mac": last_connection.get("mac"),
            "connected": last_connection.get("connected"),
        }
    existing_names = {
        "zyxel-gs1920-24",
        *(d["name"] for d in nodes),
        *(d["name"] for d in unifi_devices),
    }
    extra_servers, server_ports = build_servers(
        clients, usw_name, existing_names, switch_ports_by_ip
    )

    base_device_types = [
        {
            "manufacturer": "generic",
            "model": "Talos node",
            "slug": "talos-node",
            "u_height": 1,
            "is_full_depth": True,
            "description": "Generic x86 node running Talos Linux.",
        },
        {
            "manufacturer": "generic",
            "model": "Talos node (2U)",
            "slug": "talos-node-2u",
            "u_height": 2,
            "is_full_depth": True,
            "description": "Generic 2U x86 node running Talos Linux.",
        },
        {
            "manufacturer": "zyxel",
            "model": "GS1920-24",
            "slug": "zyxel-gs1920-24",
            "u_height": 1,
            "is_full_depth": True,
            "description": "24 x 1G ports + 4 x SFP, firmware V4.10(AAOB.5).",
        },
        {
            "manufacturer": "dell",
            "model": "Dell server",
            "slug": "dell-server",
            "u_height": 1,
            "is_full_depth": True,
            "description": "Generic Dell rack server.",
        },
        {
            "manufacturer": "dell",
            "model": "Dell server (2U)",
            "slug": "dell-server-2u",
            "u_height": 2,
            "is_full_depth": True,
            "description": "Generic 2U Dell rack server.",
        },
    ]
    all_device_types = {d["slug"]: d for d in base_device_types}
    for device_type in device_types:
        all_device_types.setdefault(device_type["slug"], device_type)

    known_cables = [dict(cable) for cable in cables]
    udm_name = next((d["name"] for d in unifi_devices if d["role"] == "gateway"), None)
    if udm_name:
        known_cables.append(
            {
                "a_device": udm_name,
                "a_interface": "Port 8",
                "b_device": "zyxel-gs1920-24",
                "b_interface": "Port 28",
                "type": "cat6",
                "status": "connected",
                "description": "Zyxel server-accessory uplink (VLAN 1 native, VLAN 4 tagged).",
            }
        )

    seen_cables = {
        frozenset(
            {
                (cable["a_device"], cable["a_interface"]),
                (cable["b_device"], cable["b_interface"]),
            }
        )
        for cable in known_cables
    }

    def add_cable(
        a_device: str,
        a_interface: str,
        b_device: str,
        b_interface: str,
        cable_type: str,
        description: str,
    ) -> None:
        key = frozenset({(a_device, a_interface), (b_device, b_interface)})
        if key in seen_cables:
            return
        seen_cables.add(key)
        known_cables.append(
            {
                "a_device": a_device,
                "a_interface": a_interface,
                "b_device": b_device,
                "b_interface": b_interface,
                "type": cable_type,
                "status": "connected",
                "description": description,
            }
        )

    for device_name, members in SERVER_PORTS.items():
        for interface_name, port in members.items():
            add_cable(
                device_name,
                interface_name,
                usw_name,
                f"Port {port}",
                "dac-passive" if port == 26 else "cat6",
                f"{device_name} {interface_name} to {usw_name} Port {port}",
            )

    for server_name, port in server_ports.items():
        add_cable(
            server_name,
            "Port 1",
            usw_name,
            f"Port {port}",
            "cat6",
            f"{server_name} Port 1 to {usw_name} Port {port}",
        )

    devices_all = [build_zyxel(), *nodes, *unifi_devices, *extra_servers]
    devices_by_name = {device["name"]: device for device in devices_all}
    server_by_ip = {
        server["interfaces"][0]["ip_addresses"][0]["address"].split("/")[0]: server["name"]
        for server in extra_servers
    }
    rack_names = [
        name
        for name in (
            udm_name,
            usw_name,
            server_by_ip.get("192.168.6.181"),
            server_by_ip.get("192.168.6.230"),
            server_by_ip.get("192.168.6.133"),
            server_by_ip.get("192.168.6.68"),
            server_by_ip.get("192.168.6.190"),
            "glemmen180",
            "glemmen170",
            "glemmen160",
            "glemmen150",
            server_by_ip.get("192.168.6.140"),
            "glemmen130",
            server_by_ip.get("192.168.6.120"),
            server_by_ip.get("192.168.6.100"),
            "glemmen80",
            server_by_ip.get("192.168.6.70"),
            "glemmen50",
            "glemmen60",
        )
        if name
    ]
    cursor = RACK_TOP
    for name in rack_names:
        device = devices_by_name.get(name)
        if device is None:
            print(f"WARNING: rack device {name!r} not found", file=sys.stderr)
            continue
        height = 2 if name in TWO_U_RACK_NAMES else 1
        device["rack"] = RACK_NAME
        # NetBox stores the lowest occupied unit as the rack position.
        device["position"] = cursor - height + 1
        device["face"] = "front"
        cursor -= height

    inventory = {
        "sites": [
            {
                "name": SITE_NAME,
                "slug": SITE_SLUG,
                "status": "active",
                "description": "2ITA Kubernetes cluster and network at Glemmen.",
            }
        ],
        "manufacturers": [
            {"name": "Generic", "slug": "generic", "description": "Generic or custom hardware."},
            {"name": "Zyxel", "slug": "zyxel"},
            {"name": "Ubiquiti", "slug": "ubiquiti"},
            {"name": "Dell", "slug": "dell"},
        ],
        "platforms": [
            {
                "name": "Talos Linux",
                "slug": "talos-linux",
                "description": "Immutable Kubernetes operating system.",
            },
            {
                "name": "UniFi",
                "slug": "unifi",
                "manufacturer": "ubiquiti",
                "description": "UniFi OS / UniFi Network firmware.",
            },
        ],
        "device_roles": [
            {"name": "Kubernetes control plane", "slug": "control-plane", "color": "326ce5"},
            {"name": "Kubernetes worker", "slug": "worker", "color": "6fa8dc"},
            {"name": "Switch", "slug": "switch", "color": "00a0d1"},
            {"name": "Gateway", "slug": "gateway", "color": "f5a623"},
            {"name": "Access point", "slug": "access-point", "color": "7b61ff"},
            {"name": "Server", "slug": "server", "color": "9e9e9e"},
        ],
        "device_types": list(all_device_types.values()),
        "racks": [
            {
                "name": RACK_NAME,
                "site": SITE_SLUG,
                "status": "active",
                "width": 19,
                "u_height": 42,
                "desc_units": False,
                "description": "Physical Glemmen rack, ordered top to bottom.",
            }
        ],
        "vlans": vlans,
        "prefixes": prefixes,
        "devices": devices_all,
        "ip_addresses": [
            {
                "address": "192.168.6.98/24",
                "status": "active",
                "description": "Talos Kubernetes API Layer2 VIP.",
            },
            {
                "address": "192.168.6.99/24",
                "status": "active",
                "description": "MetalLB ingress VIP used by Traefik.",
            },
        ],
        "cables": known_cables,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by scripts/netbox/generate_inventory.py from the live UniFi\n"
        "# controller and Kubernetes/Talos node data. Re-run it to refresh, review\n"
        "# the diff, and commit. Do not hand-edit generated sections.\n\n"
    )
    output.write_text(
        header + yaml.safe_dump(inventory, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"Wrote {output}")
    print(
        f"  devices={len(devices_all)} ({len(nodes)} nodes, {len(unifi_devices)} UniFi, "
        f"{len(extra_servers)} servers, 1 Zyxel), "
        f"cables={len(known_cables)}, vlans={len(vlans)}, prefixes={len(prefixes)}, "
        f"racked={len(rack_names)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
