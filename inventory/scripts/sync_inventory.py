"""Load, merge and expand the YAML inventory layers."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import yaml

from inventory_schema import AUXILIARY_KEYS, LAYER_ORDER, SECTIONS

IGNORED_INVENTORY_FILES = {"kustomization.yaml", "kustomization.yml"}
from inventory_util import (
    SyncError,
    as_list,
    normalize_address,
    normalize_prefix,
    slugify,
)


def _normalize_merge_value(key: str, current: Any, desired: Any) -> Any:
    if key in {"tags", "tagged_vlans"}:
        return sorted({*as_list(current), *as_list(desired)})
    if key == "interfaces":
        return _merge_named_list(current, desired, "name")
    if key == "ip_addresses":
        return _merge_named_list(current, desired, "address")
    if key == "module_bays":
        return _merge_named_list(current, desired, "name")
    if key == "modules":
        return _merge_named_list(current, desired, "bay")
    if key == "images":
        return _merge_images(current, desired)
    if isinstance(current, dict) and isinstance(desired, dict):
        return _merge_mapping(current, desired)
    return copy.deepcopy(desired)

def _merge_mapping(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        else:
            result[key] = _normalize_merge_value(key, result[key], value)
    return result

def _merge_named_list(base: Any, override: Any, key: str) -> list[Any]:
    items = [copy.deepcopy(item) for item in as_list(base)]
    order = [str(item.get(key)) for item in items if isinstance(item, dict) and item.get(key) is not None]
    by_key = {
        str(item.get(key)): item
        for item in items
        if isinstance(item, dict) and item.get(key) is not None
    }
    for item in as_list(override):
        if not isinstance(item, dict):
            continue
        item_key = str(item.get(key))
        if item_key in by_key:
            by_key[item_key] = _merge_mapping(by_key[item_key], item)
        else:
            by_key[item_key] = copy.deepcopy(item)
            order.append(item_key)
    return [by_key[item_key] for item_key in order]

def _merge_images(base: Any, override: Any) -> dict[str, Any]:
    result = copy.deepcopy(base or {})
    for key, value in (override or {}).items():
        if key == "other":
            result[key] = _merge_named_list(result.get(key), value, "name")
        else:
            result[key] = copy.deepcopy(value)
    return result

def _inventory_key(section: str, item: dict[str, Any]) -> Any:
    if section == "module_types":
        return str(
            item.get("slug")
            or slugify(
                f"{item.get('manufacturer')}-{item.get('model') or item.get('name') or ''}"
            )
        )
    if section in {
        "sites",
        "manufacturers",
        "platforms",
        "device_roles",
        "device_types",
        "module_bay_types",
    }:
        return str(item.get("slug") or slugify(item.get("name") or item.get("model") or ""))
    if section == "racks":
        return (str(item.get("site")), str(item.get("name")))
    if section == "vlans":
        return (str(item.get("site")), int(item.get("vid")))
    if section == "prefixes":
        return normalize_prefix(item.get("prefix"))
    if section == "devices":
        return str(item.get("name"))
    if section == "ip_addresses":
        return normalize_address(item.get("address"))
    if section == "cables":
        return tuple(
            sorted(
                [
                    (str(item.get("a_device")), str(item.get("a_interface"))),
                    (str(item.get("b_device")), str(item.get("b_interface"))),
                ]
            )
        )
    return None

def _merge_section(section: str, generated: list[Any], manual: list[Any]) -> list[dict[str, Any]]:
    merged: dict[Any, dict[str, Any]] = {}
    layers: dict[Any, str] = {}
    for layer, items in (("generated", generated), ("manual", manual)):
        for raw in items:
            if not isinstance(raw, dict):
                raise SyncError(f"{section}: every entry must be a mapping")
            key = _inventory_key(section, raw)
            if key in merged:
                if layers[key] == layer:
                    raise SyncError(f"{section}: duplicate key {key!r} in the {layer} layer")
                merged[key] = _merge_mapping(merged[key], raw)
            else:
                merged[key] = copy.deepcopy(raw)
                layers[key] = layer
    return list(merged.values())

def _cable_key(item: dict[str, Any]) -> tuple[Any, Any]:
    return tuple(
        sorted(
            [
                (str(item.get("a_device")), str(item.get("a_interface"))),
                (str(item.get("b_device")), str(item.get("b_interface"))),
            ]
        )
    )

def _expand_uplinks(inventory: dict[str, list[Any]]) -> None:
    cables_by_key: dict[Any, dict[str, Any]] = {
        _cable_key(cable): cable for cable in inventory["cables"]
    }
    for device in inventory["devices"]:
        uplinks = device.pop("uplinks", None) or []
        for uplink in uplinks:
            cable = {
                "a_device": device["name"],
                "a_interface": str(uplink.get("interface") or "Port 1"),
                "b_device": str(uplink.get("switch")),
                "b_interface": f"Port {int(uplink.get('port'))}",
                "type": str(uplink.get("type") or "cat6"),
                "status": "connected",
                "description": f"{device['name']} {uplink.get('interface')} to {uplink.get('switch')} Port {uplink.get('port')}",
            }
            key = _cable_key(cable)
            if key in cables_by_key:
                cables_by_key[key].update(cable)
            else:
                cables_by_key[key] = cable
    inventory["cables"] = list(cables_by_key.values())

def _expand_rack_layouts(inventory: dict[str, list[Any]]) -> None:
    devices = {str(device.get("name")): device for device in inventory["devices"]}
    device_types = {str(item.get("slug")): item for item in inventory["device_types"]}

    for rack in inventory["racks"]:
        layout = rack.pop("layout", None)
        if not layout:
            continue
        if not isinstance(layout, list):
            raise SyncError(f"rack {rack.get('name')!r}: layout must be a list")
        cursor = int(rack.get("u_height", 42))
        seen: set[str] = set()
        for entry in layout:
            if not isinstance(entry, dict):
                raise SyncError(f"rack {rack.get('name')!r}: every layout entry must be a mapping")
            gap = entry.get("gap", entry.get("empty"))
            if gap is not None:
                cursor -= int(gap)
                if cursor < 0:
                    raise SyncError(f"rack {rack.get('name')!r}: layout exceeds rack height")
                continue
            name = entry.get("device")
            if not name:
                raise SyncError(f"rack {rack.get('name')!r}: layout entry needs device or gap")
            name = str(name)
            if name in seen:
                raise SyncError(f"rack {rack.get('name')!r}: device {name!r} appears more than once")
            seen.add(name)
            device = devices.get(name)
            if device is None:
                raise SyncError(f"rack {rack.get('name')!r}: unknown device {name!r}")
            device_type = device_types.get(str(device.get("device_type")))
            if device_type is None:
                raise SyncError(f"device {name}: unknown device type {device.get('device_type')!r}")
            height = int(device_type.get("u_height", 1))
            position = cursor - height + 1
            if position < 1:
                raise SyncError(f"rack {rack.get('name')!r}: device {name!r} does not fit")
            device["rack"] = rack["name"]
            device["position"] = position
            device["face"] = str(entry.get("face") or device.get("face") or "front")
            cursor = position - 1
        if cursor != 0:
            raise SyncError(
                f"rack {rack.get('name')!r}: layout uses {int(rack.get('u_height', 42)) - cursor} "
                f"of {rack.get('u_height', 42)} units"
            )

def load_inventory(directory: Path) -> dict[str, list[Any]]:
    if not directory.is_dir():
        raise SyncError(f"inventory directory {directory} does not exist")

    # A Kubernetes ConfigMap volume contains a visible symlink plus the hidden
    # ..data/..<timestamp>/ tree. rglob() would otherwise load the same file
    # several times and report duplicate keys. Ignore hidden path components
    # and de-duplicate resolved files.
    files: set[Path] = set()
    for pattern in ("*.yaml", "*.yml"):
        for path in directory.rglob(pattern):
            relative = path.relative_to(directory)
            if path.name in IGNORED_INVENTORY_FILES:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            try:
                files.add(path.resolve())
            except OSError:
                files.add(path)
    if not files:
        raise SyncError(f"no YAML files found under {directory}")

    by_layer: dict[str, dict[str, list[Any]]] = {
        layer: {section: [] for section in SECTIONS} for layer in LAYER_ORDER
    }
    defaults: dict[str, Any] = {}

    for path in sorted(files):
        try:
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            raise SyncError(f"{path}: {exc}") from exc
        for document in documents:
            if document is None:
                continue
            if not isinstance(document, dict):
                raise SyncError(f"{path}: every YAML document must be a mapping")
            layer = str(document.get("layer") or "manual").strip().lower()
            if layer not in LAYER_ORDER:
                raise SyncError(f"{path}: unknown layer {layer!r}; expected generated or manual")
            for key, value in document.items():
                if key == "layer":
                    continue
                if key == "defaults":
                    if not isinstance(value, dict):
                        raise SyncError(f"{path}: defaults must be a mapping")
                    defaults.update(value)
                elif key in AUXILIARY_KEYS:
                    continue
                elif key in by_layer[layer]:
                    if value is None:
                        continue
                    if not isinstance(value, list):
                        raise SyncError(f"{path}: {key} must be a list")
                    by_layer[layer][key].extend(value)
                else:
                    print(f"WARNING: {path}: ignoring unknown top-level key {key!r}", file=sys.stderr)

    inventory: dict[str, list[Any]] = {}
    for section in SECTIONS:
        inventory[section] = _merge_section(
            section, by_layer["generated"][section], by_layer["manual"][section]
        )

    for device in inventory["devices"]:
        for key, value in defaults.items():
            if key == "tags":
                device["tags"] = sorted({*as_list(device.get("tags")), *as_list(value)})
            elif key not in device:
                device[key] = copy.deepcopy(value)

    _expand_uplinks(inventory)
    _expand_rack_layouts(inventory)
    return inventory
