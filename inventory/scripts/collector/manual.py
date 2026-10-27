"""Manual inventory loading and source mapping hints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from inventory_keys import inventory_files
from inventory_util import InventoryError

from .log import get_logger

log = get_logger(__name__)


def load_manual(directory: Path) -> dict[str, Any]:
    if not directory.is_dir():
        raise InventoryError(f"manual inventory directory {directory} does not exist")

    log.debug("Reading manual inventory from %s", directory)
    manual: dict[str, Any] = {
        "settings": {},
        "unifi_models": {},
        "unifi_vlan_names": {},
        "defaults": {},
        "devices": [],
    }
    files = inventory_files(directory)

    for path in files:
        log.debug("Reading manual inventory file %s", path)
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if document is None:
                continue
            if not isinstance(document, dict):
                raise InventoryError(f"{path}: every YAML document must be a mapping")
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
                        raise InventoryError(f"{path}: defaults must be a mapping")
                    manual["defaults"].update(value)
                elif key == "devices":
                    if not isinstance(value, list):
                        raise InventoryError(f"{path}: devices must be a list")
                    manual["devices"].extend(value)
                elif key in {
                    "sites",
                    "manufacturers",
                    "platforms",
                    "device_roles",
                    "device_types",
                    "module_bay_types",
                    "module_types",
                    "racks",
                    "vlans",
                    "prefixes",
                    "ip_addresses",
                    "cables",
                    "owner_groups",
                    "owners",
                    "contact_roles",
                    "contacts",
                }:
                    # Non-device manual sections are consumed by sync.py. The
                    # collector only needs settings, device types and devices.
                    continue
                else:
                    log.debug("Ignoring manual top-level key %r from %s", key, path)

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
    log.info(
        "Manual inventory: %d device(s), %d device type(s) from %d file(s)",
        len(manual["devices"]),
        len(manual["device_types_by_slug"]),
        len(files),
    )
    log.debug(
        "Manual settings: site=%s, rack=%s, default_tag=%s",
        manual["settings"].get("site"),
        manual["settings"].get("rack"),
        manual["settings"].get("default_tag"),
    )
    return manual

def _read_manual_section(directory: Path, section: str) -> list[dict]:
    result: list[dict] = []
    for path in inventory_files(directory):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(document, dict) and isinstance(document.get(section), list):
                result.extend(document[section])
    return result
