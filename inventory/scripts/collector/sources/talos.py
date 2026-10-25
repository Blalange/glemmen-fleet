"""Talos and Kubernetes source adapters."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from ..process import kubectl_command, run


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


def node_system_info(talosctl: str, talosconfig: str | None, node: str) -> dict[str, Any]:
    """Return the SMBIOS/system-information resource for one Talos node."""
    for item in talos_table(talosctl, talosconfig, node, "systeminformation"):
        spec = item.get("spec") or {}
        if spec:
            return spec
    return {}
