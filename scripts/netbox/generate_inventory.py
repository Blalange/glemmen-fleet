#!/usr/bin/env python3
"""Collect live UniFi, Talos and iDRAC Redfish facts into the NetBox generated inventory layer.

The manual layer in ``flux/apps/netbox/inventory/manual`` is the source of truth
for everything that is not discoverable: device models, rack layout, physical
servers, accessories, manual cables and VIPs. This script only writes
``flux/apps/netbox/inventory/generated``; it never edits the manual layer.

Credentials are read from the ``unpoller-unifi`` Secret in the monitoring
namespace with --from-cluster, or from UNIFI_USER / UNIFI_PASS. They are never
written to the output. iDRAC Redfish uses the read-only ``readonly`` account by
default; override it with IDRAC_USER / IDRAC_PASS.

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
DEFAULT_IDRAC_USER = "readonly"
DEFAULT_IDRAC_PASS = "readonly"


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


def talosctl_command(talosctl: str, talosconfig: str | None, node: str, *args: str) -> list[str]:
    cmd = [talosctl]
    if talosconfig:
        cmd.extend(["--talosconfig", talosconfig])
    if not _is_ip(node):
        # talosctl resolves a hostname through the talosconfig endpoints. When
        # that fails, connecting to the node address directly still works.
        endpoint = node_address(node)
        if endpoint:
            cmd.extend(["--endpoints", endpoint])
    cmd.extend(["-n", node, *args])
    return cmd


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def node_address(node_name: str) -> str | None:
    """Return the in-band address for a Talos node name.

    The usual naming is glemmenNNN -> 192.168.6.NNN. Nodes whose address does
    not follow that pattern are skipped; pass the node address explicitly in
    that case.
    """
    match = re.fullmatch(r"glemmen(\d{1,3})", node_name)
    if match and 1 <= int(match.group(1)) <= 254:
        return f"192.168.6.{int(match.group(1))}"
    return None


def talos_table(talosctl: str, talosconfig: str | None, node: str, resource: str) -> list[dict]:
    """Run ``talosctl get <resource> -o json`` and return the documents.

    Talos prints a stream of pretty-printed JSON documents (not one array), so
    decode the stream incrementally. Returns an empty list when the node is
    unreachable or the resource is not registered, so a missing node never
    breaks the whole inventory refresh.
    """
    try:
        raw = run(talosctl_command(talosctl, talosconfig, node, "get", resource, "-o", "json"))
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"WARNING: talosctl get {resource} on {node} failed: {exc}", file=sys.stderr)
        return []
    decoder = json.JSONDecoder()
    items: list[dict] = []
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        try:
            payload, index = decoder.raw_decode(raw, index)
        except json.JSONDecodeError:
            next_object = raw.find("{", index + 1)
            if next_object == -1:
                break
            index = next_object
            continue
        if isinstance(payload, dict):
            items.append(payload)
        elif isinstance(payload, list):
            items.extend(item for item in payload if isinstance(item, dict))
    return items


def disk_serial(spec: dict) -> str:
    serial = str(spec.get("serial") or "").strip()
    if serial:
        return serial
    for link in spec.get("symlinks") or []:
        name = Path(str(link)).name
        if name.startswith("ata-") and "_" in name:
            candidate = name.rsplit("_", 1)[-1]
            if candidate:
                return candidate
    return ""


def disk_manufacturer(model: str) -> str:
    model = model.upper()
    if model.startswith("ST"):
        return "seagate"
    if model.startswith("WD") or model.startswith("WDC"):
        return "western-digital"
    return "generic"


def normalize_manufacturer(name: str) -> str:
    value = name.strip().lower()
    if "intel" in value:
        return "intel"
    if "micron" in value:
        return "micron"
    if "amd" in value:
        return "amd"
    if "dell" in value:
        return "dell"
    if "ibm" in value or "lenovo" in value:
        return "ibm"
    if "seagate" in value or value.startswith("st"):
        return "seagate"
    if "western digital" in value or value.startswith("wd") or value.startswith("wdc"):
        return "western-digital"
    return "generic"


def clean_model(model: str) -> str:
    value = re.sub(r"\((?:R|TM|r|tm)\)", "", str(model))
    return re.sub(r"\s+", " ", value).strip()


def register_module_type(
    catalog: dict[str, dict[str, Any]],
    *,
    manufacturer: str,
    model: str,
    profile: str,
    attributes: dict[str, Any],
    bay_types: list[str],
    part_number: str = "",
    description: str = "",
) -> str:
    model = clean_model(model)
    if model.lower().startswith(manufacturer.lower() + " "):
        slug = slugify(model)
    else:
        slug = slugify(f"{manufacturer}-{model}")
    catalog.setdefault(
        slug,
        {
            "manufacturer": manufacturer,
            "model": model,
            "slug": slug,
            "profile": profile,
            "attributes": attributes,
            "bay_types": bay_types,
            "part_number": part_number or model,
            "description": description,
        },
    )
    return slug


def memory_class(device_type: str) -> str:
    if device_type in {"dell-poweredge-r250", "dell-poweredge-r630"}:
        return "DDR4"
    return "DDR3"


def infer_memory_manufacturer(model: str, fallback: str) -> str:
    value = model.upper()
    if value.startswith("9ASF"):
        return "micron"
    if value.startswith("M393"):
        return "samsung"
    if value.startswith(("HMA", "HMT")):
        return "sk-hynix"
    return fallback


def disk_bay_type(device_type: str) -> str:
    if device_type in {"dell-poweredge-r630", "ibm-system-x3650-m3"}:
        return "drive-2-5"
    return "drive-3-5"


def add_module_bay(
    module_bays: list[dict[str, Any]], bay_name: str, bay_type: str, description: str
) -> None:
    if any(bay["name"] == bay_name for bay in module_bays):
        return
    module_bays.append(
        {
            "name": bay_name,
            "position": str(len(module_bays) + 1),
            "bay_type": bay_type,
            "description": description,
        }
    )


def add_module_component(
    module_bays: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    bay_name: str,
    bay_type: str,
    bay_description: str,
    module_type: str,
    *,
    status: str = "active",
    serial: str = "",
    description: str = "",
) -> None:
    add_module_bay(module_bays, bay_name, bay_type, bay_description)
    modules.append(
        {
            "bay": bay_name,
            "module_type": module_type,
            "status": status,
            "serial": serial,
            "description": description,
        }
    )


def add_redfish_components(
    module_bays: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    redfish: dict[str, Any],
    device_type: str,
) -> None:
    """Add CPU, memory, disk, fan and PSU modules discovered through Redfish."""
    bay_disk_type = disk_bay_type(device_type)

    for cpu in redfish.get("cpu") or []:
        cpu_model = clean_model(cpu["model"])
        module_type = register_module_type(
            catalog,
            manufacturer=cpu["manufacturer"],
            model=cpu_model,
            profile="CPU",
            attributes={
                "cores": cpu["cores"],
                "speed": cpu["speed"],
                "architecture": cpu["architecture"],
            },
            bay_types=["cpu-socket"],
            part_number=cpu_model,
            description="Processor",
        )
        add_module_component(
            module_bays,
            modules,
            cpu["bay"],
            "cpu-socket",
            "Processor socket.",
            module_type,
            serial=cpu["serial"],
            description=cpu_model,
        )

    for memory in redfish.get("memory") or []:
        klass = str(memory["class"]).upper()
        bay_type = f"dimm-{klass.lower()}" if klass in {"DDR3", "DDR4", "DDR5"} else "dimm-ddr4"
        module_type = register_module_type(
            catalog,
            manufacturer=memory["manufacturer"],
            model=memory["model"],
            profile="Memory",
            attributes={
                "size": memory["size_gb"],
                "class": klass if klass in {"DDR3", "DDR4", "DDR5"} else "DDR4",
                "ecc": memory["ecc"],
                "data_rate": memory["data_rate"],
            },
            bay_types=[bay_type],
            part_number=memory["model"],
            description="Memory module",
        )
        add_module_component(
            module_bays,
            modules,
            memory["bay"],
            bay_type,
            "Memory module slot.",
            module_type,
            serial=memory["serial"],
            description=f"{memory['size_gb']} GB {klass} {memory['data_rate']} MT/s",
        )

    for disk in redfish.get("disks") or []:
        attributes: dict[str, Any] = {"size": disk["size_gb"], "type": disk["type"]}
        if disk["speed"]:
            attributes["speed"] = disk["speed"]
        module_type = register_module_type(
            catalog,
            manufacturer=disk["manufacturer"],
            model=disk["model"],
            profile="Hard disk",
            attributes=attributes,
            bay_types=[bay_disk_type],
            part_number=disk["model"],
            description="Disk drive",
        )
        add_module_component(
            module_bays,
            modules,
            disk["bay"],
            bay_disk_type,
            "Drive bay.",
            module_type,
            serial=disk["serial"],
            description=f"{disk['size_gb']} GB {disk['type']}",
        )

    for fan in redfish.get("fans") or []:
        module_type = register_module_type(
            catalog,
            manufacturer="dell",
            model="System Board Fan",
            profile="Fan",
            attributes={"rpm": 3600},
            bay_types=["fan"],
            part_number="System Board Fan",
            description="Chassis cooling fan",
        )
        add_module_component(
            module_bays,
            modules,
            fan["bay"],
            "fan",
            "Chassis cooling fan.",
            module_type,
            status=fan["status"],
            description=fan["name"],
        )

    for psu in redfish.get("psus") or []:
        attributes: dict[str, Any] = {
            "input_current": "AC",
            "input_voltage": 230,
            "hot_swappable": True,
        }
        if psu["wattage"]:
            attributes["wattage"] = psu["wattage"]
        module_type = register_module_type(
            catalog,
            manufacturer=psu["manufacturer"],
            model=psu["model"],
            profile="Power supply",
            attributes=attributes,
            bay_types=["power-supply"],
            part_number=psu["model"],
            description="Hot-swap power supply",
        )
        add_module_component(
            module_bays,
            modules,
            psu["bay"],
            "power-supply",
            "Hot-swap power supply bay.",
            module_type,
            status=psu["status"],
            serial=psu["serial"],
            description=f"{psu['wattage']} W" if psu["wattage"] else "Power supply",
        )


def collect_redfish(
    manual: dict[str, Any], username: str | None, password: str | None
) -> dict[str, dict[str, Any]]:
    """Read Redfish data for every manual device that declares an iDRAC address."""
    if not username or not password:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for device in manual["devices"]:
        name = str(device.get("name") or "")
        address = device.get("idrac_ip")
        if not name or not address:
            continue
        data = read_redfish(f"https://{address}", username, password)
        if data:
            result[name] = data
            print(f"Redfish: {name} ({address})", file=sys.stderr)
    return result


def _redfish_session(username: str, password: str) -> requests.Session:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.auth = (username, password)
    session.verify = False
    session.headers.update({"Accept": "application/json", "User-Agent": "glemmen-fleet-netbox/3.0"})
    return session


def _redfish_get(session: requests.Session, base: str, path: str) -> dict | None:
    try:
        response = session.get(f"{base}{path}", timeout=30)
    except requests.RequestException as exc:
        print(f"WARNING: Redfish {base}{path}: {exc}", file=sys.stderr)
        return None
    if response.status_code != 200:
        print(f"WARNING: Redfish {base}{path}: HTTP {response.status_code}", file=sys.stderr)
        return None
    try:
        return response.json()
    except ValueError:
        return None


def read_redfish(base: str, username: str, password: str) -> dict[str, Any] | None:
    """Read CPU, memory, disk, fan and PSU data from a Dell iDRAC."""
    session = _redfish_session(username, password)
    system = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1")
    if not system:
        return None
    result: dict[str, Any] = {"system": system, "cpu": [], "memory": [], "disks": [], "fans": [], "psus": []}

    processors = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1/Processors")
    for index, member in enumerate((processors or {}).get("Members", []), 1):
        data = _redfish_get(session, base, member["@odata.id"])
        if not data:
            continue
        oem = ((data.get("Oem") or {}).get("Dell") or {}).get("DellProcessor") or {}
        result["cpu"].append(
            {
                "bay": data.get("Name") or f"CPU {index}",
                "manufacturer": normalize_manufacturer(str(data.get("Manufacturer") or "Intel")),
                "model": str(data.get("Model") or f"CPU {index}"),
                "cores": int(data.get("TotalCores") or data.get("TotalEnabledCores") or 0),
                "speed": float(oem.get("CurrentClockSpeedMhz") or data.get("OperatingSpeedMHz") or data.get("MaxSpeedMHz") or 0) / 1000,
                "architecture": str(data.get("InstructionSet") or "x86_64").lower(),
                "serial": str(data.get("SerialNumber") or ""),
            }
        )

    memory = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1/Memory")
    for index, member in enumerate((memory or {}).get("Members", []), 1):
        data = _redfish_get(session, base, member["@odata.id"])
        if not data or not data.get("CapacityMiB"):
            continue
        result["memory"].append(
            {
                "bay": str(data.get("DeviceLocator") or data.get("Name") or f"DIMM {index}"),
                "manufacturer": normalize_manufacturer(str(data.get("Manufacturer") or "")),
                "model": str(data.get("PartNumber") or data.get("Name") or f"DIMM {index}"),
                "size_gb": int(data.get("CapacityMiB") or 0) // 1024,
                "class": str(data.get("MemoryDeviceType") or "DDR4"),
                "ecc": str(data.get("ErrorCorrection") or "").lower() not in {"", "no", "none"},
                "data_rate": int((data.get("OperatingSpeedMhz") or (data.get("AllowedSpeedsMHz") or [0])[0]) or 0),
                "serial": str(data.get("SerialNumber") or ""),
            }
        )

    storage = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1/Storage")
    seen_disks: set[str] = set()
    for controller_ref in (storage or {}).get("Members", []):
        controller = _redfish_get(session, base, controller_ref["@odata.id"])
        for drive_ref in (controller or {}).get("Drives", []):
            data = _redfish_get(session, base, drive_ref["@odata.id"])
            if not data:
                continue
            key = str(data.get("SerialNumber") or data.get("Id") or "")
            if key in seen_disks:
                continue
            seen_disks.add(key)
            media = str(data.get("MediaType") or "HDD").upper()
            media_type = {"HDD": "HD", "SSD": "SSD", "NVME": "NVME"}.get(media, media)
            result["disks"].append(
                {
                    "bay": f"Drive {len(result['disks']) + 1}",
                    "manufacturer": normalize_manufacturer(str(data.get("Manufacturer") or data.get("Model") or "")),
                    "model": str(data.get("Model") or f"Disk {len(result['disks']) + 1}"),
                    "size_gb": int(int(data.get("CapacityBytes") or 0) / 1_000_000_000),
                    "type": media_type,
                    "speed": int(data.get("RotationSpeedRPM") or 0),
                    "serial": str(data.get("SerialNumber") or ""),
                    "protocol": str(data.get("Protocol") or ""),
                }
            )

    thermal = _redfish_get(session, base, "/redfish/v1/Chassis/System.Embedded.1/Thermal")
    for index, fan in enumerate((thermal or {}).get("Fans", []), 1):
        result["fans"].append(
            {
                "bay": f"Fan {index}",
                "name": str(fan.get("Name") or fan.get("FanName") or f"Fan {index}"),
                "rpm": int(fan.get("Reading") or 0),
                "status": "active" if (fan.get("Status") or {}).get("State") != "Absent" else "inventory",
            }
        )

    power = _redfish_get(session, base, "/redfish/v1/Chassis/System.Embedded.1/Power")
    for index, psu in enumerate((power or {}).get("PowerSupplies", []), 1):
        result["psus"].append(
            {
                "bay": f"PSU {index}",
                "manufacturer": normalize_manufacturer(str(psu.get("Manufacturer") or "Dell")),
                "model": str(psu.get("Model") or f"Power Supply {index}"),
                "serial": str(psu.get("SerialNumber") or ""),
                "wattage": int(psu.get("PowerCapacityWatts") or 0),
                "status": "active" if (psu.get("Status") or {}).get("State") != "Absent" else "inventory",
            }
        )

    return result


def node_disks(talosctl: str, talosconfig: str | None, node: str) -> tuple[list[dict], str | None]:
    """Return the physical disks and the system disk name for one node."""
    disks = []
    for item in talos_table(talosctl, talosconfig, node, "disks"):
        spec = item.get("spec") or {}
        name = str(
            spec.get("dev_path")
            or spec.get("devPath")
            or item.get("metadata", {}).get("id")
            or ""
        ).rsplit("/", 1)[-1]
        transport = str(spec.get("transport") or "")
        if (
            not name
            or name.startswith("loop")
            or spec.get("cdrom")
            or transport == "iscsi"
            or str(spec.get("model") or "").upper() == "VIRTUAL-DISK"
        ):
            continue
        model = str(spec.get("model") or "").strip()
        if not model:
            # nvme/sata devices may expose the model through "device".
            model = str(spec.get("device") or "").strip()
        disks.append(
            {
                "name": name,
                "model": model,
                "serial": disk_serial(spec),
                "vendor": str(spec.get("modalias") or "").strip(),
                "wwid": str(spec.get("wwid") or ""),
                "size": int(spec.get("size") or 0),
                "transport": transport,
                "rotational": bool(spec.get("rotational")),
                "readonly": bool(spec.get("readonly")),
            }
        )

    system_disk = None
    for item in talos_table(talosctl, talosconfig, node, "systemdisks"):
        spec = item.get("spec") or {}
        system_disk = str(
            spec.get("devPath")
            or spec.get("dev_path")
            or spec.get("diskID")
            or ""
        ).rsplit("/", 1)[-1] or None
        break
    return disks, system_disk


def node_link_macs(talosctl: str, talosconfig: str | None, node: str) -> dict[str, str]:
    macs: dict[str, str] = {}
    for item in talos_table(talosctl, talosconfig, node, "links"):
        spec = item.get("spec") or {}
        name = str(spec.get("name") or item.get("metadata", {}).get("id") or "")
        mac = str(spec.get("hardwareAddr") or spec.get("hardware_addr") or "")
        if name and mac and mac != "00:00:00:00:00:00":
            macs[name] = mac
    return macs


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
                    "name": "lan",
                    "type": "virtual",
                    "mgmt_only": False,
                    "enabled": True,
                    "description": "LAN and VPN gateway addresses",
                    "mac_address": d.get("mac"),
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
            if mgmt_ip and interfaces:
                uplink = d.get("uplink") or {}
                local_port = uplink.get("port_idx")
                target_name = f"Port {int(local_port)}" if local_port else interfaces[0]["name"]
                target = next(
                    (interface for interface in interfaces if interface["name"] == target_name),
                    interfaces[0],
                )
                target.setdefault("mac_address", d.get("mac"))
                target.setdefault("ip_addresses", []).append(
                    {"address": f"{mgmt_ip}/24", "dns_name": slugify(name), "primary": True}
                )
        else:
            mgmt_ip = d.get("ip") or (d.get("config_network") or {}).get("ip")
            if mgmt_ip and interfaces:
                interfaces[0].setdefault("mac_address", d.get("mac"))
                interfaces[0].setdefault("ip_addresses", []).append(
                    {"address": f"{mgmt_ip}/24", "dns_name": slugify(name), "primary": True}
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
    *,
    module_types: dict[str, dict[str, Any]] | None = None,
    redfish_by_name: dict[str, dict[str, Any]] | None = None,
) -> list[dict]:
    clients_by_ip = {
        str(client.get("ip")): client
        for client in clients
        if client.get("is_wired") and client.get("ip")
    }
    site_slug = manual["settings"].get("site", "glemmen")
    default_tag = manual["settings"].get("default_tag", DEFAULT_TAG)
    servers: list[dict] = []
    catalog = module_types if module_types is not None else {}
    redfish_data = redfish_by_name or {}

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

    # Devices that are not UniFi clients but do expose an iDRAC. They already
    # exist in the manual layer; this sparse entry only attaches Redfish modules.
    for device in manual["devices"]:
        if (
            device.get("talos_node")
            or device.get("unifi_ip")
            or not device.get("idrac_ip")
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
) -> list[dict]:
    result: list[dict] = []
    site_slug = manual["settings"].get("site", "glemmen")
    default_tag = manual["settings"].get("default_tag", DEFAULT_TAG)
    manual_devices = manual["devices"]
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
        redfish = redfish_data.get(name)
        if redfish is None and manual_device:
            redfish = redfish_data.get(str(manual_device.get("name") or ""))

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
            "name": name,
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


def write_yaml(path: Path, sections: dict[str, Any], title: str) -> None:
    header = (
        f"# Generated by scripts/netbox/generate_inventory.py from the live UniFi\n"
        f"# controller, Kubernetes/Talos node data and iDRAC Redfish. Do not hand-edit this file.\n"
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
    parser.add_argument(
        "--talosctl",
        default=os.environ.get("TALOSCTL"),
        help="talosctl binary used to discover NICs and disks (omit to skip)",
    )
    parser.add_argument(
        "--talosconfig",
        default=os.environ.get("TALOSCONFIG", "talos/generated/talosconfig"),
        help="talosconfig for the talosctl lookups",
    )
    parser.add_argument("--from-cluster", action="store_true", help="read credentials from unpoller-unifi")
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
    module_types: dict[str, dict[str, Any]] = {}
    redfish_by_name = collect_redfish(manual, args.redfish_user, args.redfish_pass)
    if any(device.get("idrac_ip") for device in manual["devices"]) and not (
        args.redfish_user and args.redfish_pass
    ):
        print(
            "WARNING: iDRAC addresses are configured but IDRAC_USER/IDRAC_PASS are empty; "
            "skipping Redfish components",
            file=sys.stderr,
        )
    servers = build_servers(
        clients,
        manual,
        switch_name,
        module_types=module_types,
        redfish_by_name=redfish_by_name,
    )
    nodes = build_nodes(
        read_nodes(args.kubectl, args.kubeconfig, args.nodes_json),
        Path(args.talos_config_dir),
        manual,
        talosctl=args.talosctl,
        talosconfig=args.talosconfig,
        module_types=module_types,
        redfish_by_name=redfish_by_name,
    )

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
        {
            "module_types": list(module_types.values()),
            "devices": nodes,
        },
        "Talos/Kubernetes nodes and discovered hardware components.",
    )

    print(f"Wrote {output_dir}/unifi.yaml, servers.yaml, talos.yaml")
    print(
        f"  unifi_devices={len(unifi_devices)}, servers={len(servers)}, nodes={len(nodes)}, "
        f"cables={len(cables)}, vlans={len(vlans)}, prefixes={len(prefixes)}, "
        f"module_types={len(module_types)}, "
        f"modules={sum(len(device.get('modules', [])) for device in [*servers, *nodes])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
