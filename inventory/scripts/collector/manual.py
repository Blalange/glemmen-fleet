"""Manual inventory loading and source mapping hints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


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
