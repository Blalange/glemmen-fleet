#!/usr/bin/env python3
"""Reconcile the NetBox YAML inventory into the NetBox REST API.

The inventory has two layers:

  * ``inventory/generated`` is written by ``scripts/netbox/generate_inventory.py``
    from the live UniFi controller and Kubernetes/Talos nodes;
  * ``inventory/manual`` is edited by hand and owns models, rack layout,
    physical servers, accessories, manual cables and VIPs.

This script merges both layers (manual values win), expands the declarative
rack layout into NetBox positions, and reconciles the result. Every object it
manages is tagged with the ``gitops`` tag. Pruning is opt-in and only ever
deletes objects that carry that tag, so objects created by hand in the NetBox
UI are safe.

Run locally (uv fetches Python and the pinned dependencies):

    uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 --with requests==2.32.5 \
      python sync.py --inventory inventory --check

    NETBOX_URL=https://netbox.glem.men NETBOX_TOKEN=... \
    uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 --with requests==2.32.5 \
      python sync.py --inventory inventory --dry-run --prune

The CronJob runs the same script with DRY_RUN=false and PRUNE=true and mounts
the device-type images at /images.
"""

from __future__ import annotations

import argparse
import copy
import ipaddress
import mimetypes
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

import pynetbox
import requests
import yaml

MANAGED_TAG_NAME = "GitOps"
MANAGED_TAG_SLUG = "gitops"
MANAGED_TAG_COLOR = "607d8b"
DEFAULT_COLOR = "9e9e9e"
DEFAULT_STATUS = "active"

SECTIONS = (
    "sites",
    "manufacturers",
    "platforms",
    "device_roles",
    "device_types",
    "racks",
    "vlans",
    "prefixes",
    "devices",
    "ip_addresses",
    "cables",
)

# Keys that only configure the collector or sync behaviour and are not NetBox
# objects. They are silently ignored by the inventory loader.
AUXILIARY_KEYS = {"settings", "unifi_models", "unifi_vlan_names"}
LAYER_ORDER = {"generated": 0, "manual": 1}

class SyncError(RuntimeError):
    """A user-facing inventory or API error."""


class MockObject:
    """Stand-in for a newly created object during a dry run.

    Dry runs still need IDs to resolve references between objects that would be
    created in the same run. Negative IDs can never collide with NetBox IDs.
    """

    _next_id = -1

    def __init__(self, **data: Any) -> None:
        self.id = MockObject._next_id
        MockObject._next_id -= 1
        for key, value in data.items():
            setattr(self, key, value)
        if not hasattr(self, "tags"):
            self.tags = []

    def update(self, data: dict[str, Any] | None = None, **kwargs: Any) -> None:
        data = {**(data or {}), **kwargs}
        for key, value in data.items():
            setattr(self, key, value)

    def save(self) -> None:
        return None

    def delete(self) -> None:
        return None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require(mapping: dict[str, Any], key: str, kind: str) -> Any:
    value = mapping.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SyncError(f"{kind}: missing required field {key!r}")
    return value


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def slugify(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    return slug or "item"


def normalize_color(value: Any) -> str:
    # NetBox's ColorValidator only accepts lowercase six-digit hex values.
    color = str(value).strip().lstrip("#").lower()
    if not re.fullmatch(r"[0-9a-f]{6}", color):
        raise SyncError(f"invalid color {value!r}; expected six hex digits")
    return color


def normalize_mac(value: Any) -> str:
    digits = re.sub(r"[^0-9A-Fa-f]", "", str(value))
    if len(digits) != 12:
        raise SyncError(f"invalid MAC address {value!r}")
    return ":".join(digits[index : index + 2].upper() for index in range(0, 12, 2))


def normalize_address(value: Any) -> str:
    try:
        return str(ipaddress.ip_interface(str(value).strip()))
    except ValueError as exc:
        raise SyncError(f"invalid IP address {value!r}: {exc}") from exc


def normalize_prefix(value: Any) -> str:
    try:
        return str(ipaddress.ip_network(str(value).strip(), strict=False))
    except ValueError as exc:
        raise SyncError(f"invalid prefix {value!r}: {exc}") from exc


def same_value(current: Any, desired: Any) -> bool:
    """Compare API values (records, choice objects, lists) with desired values."""
    # NetBox's ChoiceField serializes choices as {"value": ..., "label": ...};
    # pynetbox turns those dicts into Record objects with .value/.label.
    if isinstance(current, dict) and "value" in current:
        current = current["value"]
    elif not isinstance(current, (str, int, float, bool, list, tuple, dict)) and hasattr(current, "value"):
        current = current.value
    # Blank optional strings and null are equivalent for our purposes (NetBox
    # frequently stores "" as null).
    if desired == "" and current is None:
        return True
    if desired is None and current == "":
        return True
    if isinstance(desired, int):
        return getattr(current, "id", current) == desired
    if isinstance(desired, list):
        current_list = current if isinstance(current, list) else as_list(current)
        current_ids = sorted(str(getattr(item, "id", item)) for item in current_list)
        desired_ids = sorted(str(getattr(item, "id", item)) for item in desired)
        return current_ids == desired_ids
    if desired is None:
        return current is None
    return current == desired


def format_key(key: Any) -> str:
    if isinstance(key, tuple):
        return "/".join(str(part) for part in key if part is not None)
    return str(key)


def _normalize_merge_value(key: str, current: Any, desired: Any) -> Any:
    if key in {"tags", "tagged_vlans"}:
        return sorted({*as_list(current), *as_list(desired)})
    if key == "interfaces":
        return _merge_named_list(current, desired, "name")
    if key == "ip_addresses":
        return _merge_named_list(current, desired, "address")
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
    if section in {"sites", "manufacturers", "platforms", "device_roles", "device_types"}:
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


def validate_inventory(inventory: dict[str, list[Any]]) -> None:
    """Validate references and uniqueness without contacting NetBox."""

    def index(items: list[dict[str, Any]], kind: str, key_fn: Callable[[dict[str, Any]], Any]) -> dict[Any, dict[str, Any]]:
        result: dict[Any, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise SyncError(f"{kind}: every entry must be a mapping")
            key = key_fn(item)
            if key in result:
                raise SyncError(f"{kind}: duplicate key {key!r}")
            result[key] = item
        return result

    sites = index(
        inventory["sites"],
        "sites",
        lambda item: item.get("slug") or slugify(require(item, "name", "site")),
    )
    manufacturers = index(
        inventory["manufacturers"],
        "manufacturers",
        lambda item: item.get("slug") or slugify(require(item, "name", "manufacturer")),
    )
    platforms = index(
        inventory["platforms"],
        "platforms",
        lambda item: item.get("slug") or slugify(require(item, "name", "platform")),
    )
    roles = index(
        inventory["device_roles"],
        "device_roles",
        lambda item: item.get("slug") or slugify(require(item, "name", "device role")),
    )
    device_types = index(
        inventory["device_types"],
        "device_types",
        lambda item: item.get("slug")
        or slugify(f"{require(item, 'manufacturer', 'device type')}-{require(item, 'model', 'device type')}"),
    )
    racks = index(
        inventory["racks"],
        "racks",
        lambda item: (require(item, "site", "rack"), require(item, "name", "rack")),
    )
    vlans = index(
        inventory["vlans"],
        "vlans",
        lambda item: (item.get("site"), int(require(item, "vid", "VLAN"))),
    )

    for item in inventory["platforms"]:
        manufacturer = item.get("manufacturer")
        if manufacturer and manufacturer not in manufacturers:
            raise SyncError(f"platform {item.get('name')!r}: unknown manufacturer {manufacturer!r}")

    for item in inventory["device_types"]:
        manufacturer = require(item, "manufacturer", "device type")
        if manufacturer not in manufacturers:
            raise SyncError(f"device type {item.get('model')!r}: unknown manufacturer {manufacturer!r}")

    for item in inventory["racks"]:
        site = require(item, "site", "rack")
        if site not in sites:
            raise SyncError(f"rack {item.get('name')!r}: unknown site {site!r}")

    for item in inventory["vlans"]:
        site = item.get("site")
        if site is not None and site not in sites:
            raise SyncError(f"VLAN {item.get('vid')}: unknown site {site!r}")
        if not 1 <= int(require(item, "vid", "VLAN")) <= 4094:
            raise SyncError(f"VLAN {item.get('vid')}: VID must be between 1 and 4094")

    index(
        inventory["prefixes"],
        "prefixes",
        lambda item: normalize_prefix(require(item, "prefix", "prefix")),
    )
    for item in inventory["prefixes"]:
        site = item.get("site")
        if site is not None and site not in sites:
            raise SyncError(f"prefix {item.get('prefix')!r}: unknown site {site!r}")
        vlan = item.get("vlan")
        if vlan is not None and (site, int(vlan)) not in vlans and (None, int(vlan)) not in vlans:
            raise SyncError(f"prefix {item.get('prefix')!r}: unknown VLAN {vlan!r}")

    devices_by_name: dict[str, dict[str, Any]] = {}
    all_addresses: dict[str, str] = {}

    def add_ip(ip: dict[str, Any], device_name: str, interface_name: str) -> None:
        address = normalize_address(require(ip, "address", "IP address"))
        if address in all_addresses:
            raise SyncError(
                f"duplicate IP address {address} on {device_name}/{interface_name} "
                f"and {all_addresses[address]}"
            )
        all_addresses[address] = f"{device_name}/{interface_name}"

    for device in inventory["devices"]:
        name = str(require(device, "name", "device"))
        if name in devices_by_name:
            raise SyncError(f"devices: duplicate device name {name!r}")
        devices_by_name[name] = device

        site = require(device, "site", "device")
        role = require(device, "role", "device")
        device_type = require(device, "device_type", "device")
        platform = device.get("platform")
        rack = device.get("rack")

        if site not in sites:
            raise SyncError(f"device {name}: unknown site {site!r}")
        if role not in roles:
            raise SyncError(f"device {name}: unknown device role {role!r}")
        if device_type not in device_types:
            raise SyncError(f"device {name}: unknown device type {device_type!r}")
        if platform and platform not in platforms:
            raise SyncError(f"device {name}: unknown platform {platform!r}")
        if rack and (site, rack) not in racks:
            raise SyncError(f"device {name}: unknown rack {rack!r} in site {site!r}")
        if not rack and (device.get("position") is not None or device.get("face")):
            raise SyncError(f"device {name}: position/face require a rack")
        if rack and device.get("position") is not None and not device.get("face"):
            raise SyncError(f"device {name}: a rack position also requires a face")
        face = device.get("face")
        if face and face not in {"front", "rear"}:
            raise SyncError(f"device {name}: face must be 'front' or 'rear', not {face!r}")

        interfaces: dict[str, dict[str, Any]] = {}
        primaries: dict[int, str] = {}
        for interface in device.get("interfaces", []) or []:
            if not isinstance(interface, dict):
                raise SyncError(f"device {name}: every interface must be a mapping")
            interface_name = str(require(interface, "name", f"interface on {name}"))
            if interface_name in interfaces:
                raise SyncError(f"device {name}: duplicate interface {interface_name!r}")
            interfaces[interface_name] = interface
            for vid in as_list(interface.get("untagged_vlan")) + as_list(interface.get("tagged_vlans")):
                if (site, int(vid)) not in vlans and (None, int(vid)) not in vlans:
                    raise SyncError(
                        f"device {name} interface {interface_name}: unknown VLAN {vid!r} "
                        f"in site {site!r}"
                    )
            for ip in interface.get("ip_addresses", []) or []:
                add_ip(ip, name, interface_name)
                if ip.get("primary"):
                    version = ipaddress.ip_interface(normalize_address(ip["address"])).version
                    if version in primaries:
                        raise SyncError(
                            f"device {name}: more than one primary IPv{version} address"
                        )
                    primaries[version] = normalize_address(ip["address"])

    # Validate rack occupancy. A 42U rack with a few devices is easy to get
    # wrong by hand, so check bounds and overlaps before touching NetBox.
    occupancy: dict[tuple[str, str], dict[int, str]] = {}
    for device in inventory["devices"]:
        rack_name = device.get("rack")
        position = device.get("position")
        if not rack_name or position is None:
            continue
        site = str(device["site"])
        key = (site, str(rack_name))
        rack_item = racks.get(key)
        if rack_item is None:
            raise SyncError(f"device {device['name']}: unknown rack {rack_name!r}")
        device_type_item = device_types[str(device["device_type"])]
        height = int(device_type_item.get("u_height", 1))
        if height < 1:
            raise SyncError(f"device {device['name']}: a racked device type must be at least 1U")
        start = int(position)
        end = start + height - 1
        rack_height = int(require(rack_item, "u_height", "rack"))
        if start < 1 or end > rack_height:
            raise SyncError(
                f"device {device['name']}: rack position {start}-{end} is outside "
                f"rack {rack_name!r} (1-{rack_height})"
            )
        used = occupancy.setdefault(key, {})
        for unit in range(start, end + 1):
            if unit in used:
                raise SyncError(
                    f"rack {rack_name!r} unit {unit} is used by both "
                    f"{used[unit]!r} and {device['name']!r}"
                )
            used[unit] = str(device["name"])

    for ip in inventory["ip_addresses"]:
        device_name = ip.get("device")
        interface_name = ip.get("interface")
        if not device_name:
            if interface_name:
                raise SyncError("ip_addresses: interface requires device")
            address = normalize_address(require(ip, "address", "IP address"))
            if address in all_addresses:
                raise SyncError(f"duplicate IP address {address}")
            all_addresses[address] = "unassigned"
            continue
        if device_name not in devices_by_name:
            raise SyncError(f"ip_addresses: unknown device {device_name!r}")
        if interface_name:
            interfaces = {
                interface["name"]
                for interface in devices_by_name[device_name].get("interfaces", []) or []
                if isinstance(interface, dict) and "name" in interface
            }
            if interface_name not in interfaces:
                raise SyncError(
                    f"ip_addresses: unknown interface {device_name}/{interface_name}"
                )
        add_ip(ip, device_name, interface_name or "unassigned")

    index(
        inventory["cables"],
        "cables",
        # Sort the two ends so A->B and B->A are detected as the same cable.
        lambda item: tuple(
            sorted(
                [
                    (require(item, "a_device", "cable"), require(item, "a_interface", "cable")),
                    (require(item, "b_device", "cable"), require(item, "b_interface", "cable")),
                ]
            )
        ),
    )
    for item in inventory["cables"]:
        for side in ("a", "b"):
            device_name = item.get(f"{side}_device")
            interface_name = item.get(f"{side}_interface")
            if device_name not in devices_by_name:
                raise SyncError(f"cable {side}: unknown device {device_name!r}")
            interfaces = {
                interface["name"]
                for interface in devices_by_name[device_name].get("interfaces", []) or []
                if isinstance(interface, dict) and "name" in interface
            }
            if interface_name not in interfaces:
                raise SyncError(
                    f"cable {side}: unknown interface {device_name}/{interface_name}"
                )

    counts = ", ".join(
        f"{section}={len(inventory[section])}" for section in SECTIONS if inventory[section]
    )
    print(f"Inventory OK: {counts or 'empty'}")


def wait_for_netbox(nb: pynetbox.api, url: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            nb.status()
            # /api/status is unauthenticated. This request proves that the
            # bootstrap API token exists as well, so the first sync does not
            # race the chart's superuser/token creation.
            list(nb.users.users.filter(limit=1))
            return
        except Exception as exc:  # noqa: BLE001 - report the last connection error
            if time.monotonic() >= deadline:
                raise SyncError(f"NetBox is not ready at {url}: {exc}") from exc
            print(f"Waiting for NetBox (status and API token) at {url}...", file=sys.stderr)
            time.sleep(5)


class Reconciler:
    def __init__(
        self,
        nb: pynetbox.api,
        inventory: dict[str, list[Any]],
        dry_run: bool = False,
        prune: bool = False,
        url: str = "",
        token: str = "",
        images_dir: Path | None = None,
        force_images: bool = False,
    ) -> None:
        self.nb = nb
        self.inventory = inventory
        self.dry_run = dry_run
        self.prune = prune
        self.url = url.rstrip("/")
        self.token = token
        self.images_dir = images_dir
        self.force_images = force_images
        self.counts: Counter[str] = Counter()
        self.tags: dict[str, Any] = {}
        self.sites: dict[str, Any] = {}
        self.site_slugs_by_id: dict[int, str] = {}
        self.manufacturers: dict[str, Any] = {}
        self.platforms: dict[str, Any] = {}
        self.roles: dict[str, Any] = {}
        self.device_types: dict[str, Any] = {}
        self.racks: dict[tuple[str, str], Any] = {}
        self.vlans: dict[tuple[str | None, int], Any] = {}
        self.devices: dict[str, Any] = {}
        self.device_sites: dict[str, str] = {}
        self.interfaces: dict[tuple[str, str], Any] = {}
        self.ip_addresses: dict[str, Any] = {}
        self.cables: dict[Any, Any] = {}
        self.device_primary: dict[str, dict[int, str]] = {}
        self.planned_delete_devices: set[str] = set()
        self.desired: dict[str, set[Any]] = {section: set() for section in SECTIONS}
        # Interfaces are nested under devices rather than being a top-level
        # inventory section, but pruning still needs their desired keys.
        self.desired["interfaces"] = set()

    # ------------------------------------------------------------------ logging

    def log(self, action: str, kind: str, label: Any, detail: str = "") -> None:
        self.counts[action] += 1
        prefix = "WOULD " if self.dry_run else ""
        message = f"{prefix}{action.upper():6} {kind:12} {label}"
        if detail:
            message += f" ({detail})"
        print(message, flush=True)

    def summary(self) -> None:
        mode = "DRY RUN" if self.dry_run else "APPLIED"
        print()
        print(
            f"{mode}: {self.counts['create']} create, "
            f"{self.counts['update']} update, "
            f"{self.counts['delete']} delete, "
            f"{self.counts['unchanged']} unchanged",
            flush=True,
        )
        if self.dry_run:
            print("No changes were written to NetBox.", flush=True)

    # -------------------------------------------------------------------- tags

    def ensure_tag(self, name: str) -> Any:
        slug = slugify(name)
        if slug in self.tags:
            return self.tags[slug]
        tag = self.nb.extras.tags.get(slug=slug)
        if tag is None:
            tag = self.nb.extras.tags.get(name=name)
        if tag is None:
            self.log("create", "tag", slug)
            if self.dry_run:
                tag = MockObject(name=name, slug=slug, color=MANAGED_TAG_COLOR)
            else:
                tag = self.nb.extras.tags.create(name=name, slug=slug, color=MANAGED_TAG_COLOR)
        self.tags[slug] = tag
        return tag

    def tag_ids(self, extra: Any = None) -> list[int]:
        ids = [self.ensure_tag(MANAGED_TAG_NAME).id]
        for name in as_list(extra):
            ids.append(self.ensure_tag(str(name)).id)
        return list(dict.fromkeys(ids))

    def is_managed(self, obj: Any) -> bool:
        return any(
            getattr(tag, "slug", None) == MANAGED_TAG_SLUG
            for tag in (getattr(obj, "tags", None) or [])
        )

    # -------------------------------------------------------------- reconciling

    def get_one(self, endpoint: Any, lookup: dict[str, Any], kind: str) -> Any:
        # A dry run resolves objects created earlier in the same run to
        # negative mock IDs. Those IDs cannot exist in NetBox, so do not send
        # them as filter values (the API rejects them with a 400).
        if any(isinstance(value, int) and value < 0 for value in lookup.values()):
            return None
        try:
            return endpoint.get(**lookup)
        except Exception as exc:  # noqa: BLE001 - pynetbox raises several types
            raise SyncError(f"look up {kind} {lookup}: {exc}") from exc

    def reconcile(
        self,
        kind: str,
        endpoint: Any,
        existing: Any,
        data: dict[str, Any],
        tags: Any = None,
        label: Any = None,
        compare_fields: Iterable[str] | None = None,
    ) -> Any:
        label = label if label is not None else kind
        data = dict(data)
        data["tags"] = self.tag_ids(tags)

        if existing is None:
            self.log("create", kind, label)
            if self.dry_run:
                return MockObject(**data)
            try:
                return endpoint.create(**data)
            except Exception as exc:  # noqa: BLE001
                raise SyncError(f"create {kind} {label}: {exc}") from exc

        fields = tuple(compare_fields) if compare_fields is not None else tuple(data)
        changes: dict[str, Any] = {}
        for field in fields:
            if field not in data:
                continue
            if not same_value(getattr(existing, field, None), data[field]):
                changes[field] = data[field]

        if changes:
            self.log("update", kind, label, ", ".join(sorted(changes)))
            if not self.dry_run:
                try:
                    existing.update(changes)
                except Exception as exc:  # noqa: BLE001
                    raise SyncError(f"update {kind} {label}: {exc}") from exc
        else:
            self.counts["unchanged"] += 1

        return existing

    def ensure(
        self,
        kind: str,
        endpoint: Any,
        lookup: dict[str, Any],
        data: dict[str, Any],
        tags: Any = None,
        label: Any = None,
    ) -> Any:
        existing = self.get_one(endpoint, lookup, kind)
        return self.reconcile(kind, endpoint, existing, data, tags=tags, label=label or lookup)

    def find_global_vlan(self, vid: int) -> Any:
        matches = [vlan for vlan in self.nb.ipam.vlans.filter(vid=vid) if vlan.site is None]
        if len(matches) > 1:
            raise SyncError(f"VLAN {vid}: multiple global VLANs found")
        return matches[0] if matches else None

    def resolve_vlan(self, site_slug: str, vid: int) -> Any:
        vlan = self.vlans.get((site_slug, vid)) or self.vlans.get((None, vid))
        if vlan is None:
            raise SyncError(f"VLAN {vid} is not declared for site {site_slug!r}")
        return vlan

    # --------------------------------------------------------------- inventory

    def sync_sites(self) -> None:
        for item in self.inventory["sites"]:
            name = require(item, "name", "site")
            slug = item.get("slug") or slugify(name)
            data: dict[str, Any] = {
                "name": name,
                "slug": slug,
                "status": item.get("status", DEFAULT_STATUS),
                "facility": item.get("facility", ""),
                "description": item.get("description", ""),
                "physical_address": item.get("physical_address", ""),
                "shipping_address": item.get("shipping_address", ""),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "comments": item.get("comments", ""),
            }
            obj = self.ensure(
                "site",
                self.nb.dcim.sites,
                {"slug": slug},
                data,
                tags=item.get("tags"),
                label=f"site/{slug}",
            )
            self.sites[slug] = obj
            self.site_slugs_by_id[obj.id] = slug
            self.desired["sites"].add(slug)

    def sync_manufacturers(self) -> None:
        for item in self.inventory["manufacturers"]:
            name = require(item, "name", "manufacturer")
            slug = item.get("slug") or slugify(name)
            data: dict[str, Any] = {
                "name": name,
                "slug": slug,
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            obj = self.ensure(
                "manufacturer",
                self.nb.dcim.manufacturers,
                {"slug": slug},
                data,
                tags=item.get("tags"),
                label=f"manufacturer/{slug}",
            )
            self.manufacturers[slug] = obj
            self.desired["manufacturers"].add(slug)

    def sync_platforms(self) -> None:
        for item in self.inventory["platforms"]:
            name = require(item, "name", "platform")
            slug = item.get("slug") or slugify(name)
            manufacturer = item.get("manufacturer")
            data: dict[str, Any] = {
                "name": name,
                "slug": slug,
                "manufacturer": None,
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            if manufacturer:
                if manufacturer not in self.manufacturers:
                    raise SyncError(f"platform {name!r}: unknown manufacturer {manufacturer!r}")
                data["manufacturer"] = self.manufacturers[manufacturer].id
            obj = self.ensure(
                "platform",
                self.nb.dcim.platforms,
                {"slug": slug},
                data,
                tags=item.get("tags"),
                label=f"platform/{slug}",
            )
            self.platforms[slug] = obj
            self.desired["platforms"].add(slug)

    def sync_device_roles(self) -> None:
        for item in self.inventory["device_roles"]:
            name = require(item, "name", "device role")
            slug = item.get("slug") or slugify(name)
            data: dict[str, Any] = {
                "name": name,
                "slug": slug,
                "color": normalize_color(item.get("color", DEFAULT_COLOR)),
                "vm_role": bool(item.get("vm_role", False)),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            obj = self.ensure(
                "device_role",
                self.nb.dcim.device_roles,
                {"slug": slug},
                data,
                tags=item.get("tags"),
                label=f"device_role/{slug}",
            )
            self.roles[slug] = obj
            self.desired["device_roles"].add(slug)

    def sync_device_types(self) -> None:
        for item in self.inventory["device_types"]:
            manufacturer = require(item, "manufacturer", "device type")
            model = require(item, "model", "device type")
            if manufacturer not in self.manufacturers:
                raise SyncError(f"device type {model!r}: unknown manufacturer {manufacturer!r}")
            slug = item.get("slug") or slugify(f"{manufacturer}-{model}")
            data: dict[str, Any] = {
                "manufacturer": self.manufacturers[manufacturer].id,
                "model": model,
                "slug": slug,
                "u_height": item.get("u_height", 1),
                "is_full_depth": bool(item.get("is_full_depth", True)),
                "part_number": item.get("part_number", ""),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
                "airflow": item.get("airflow") or "",
                "weight": item.get("weight"),
                "weight_unit": item.get("weight_unit") or "",
            }
            obj = self.ensure(
                "device_type",
                self.nb.dcim.device_types,
                {"slug": slug},
                data,
                tags=item.get("tags"),
                label=f"device_type/{slug}",
            )
            self.device_types[slug] = obj
            self.desired["device_types"].add(slug)
            self.sync_device_type_images(obj, item)

    # ----------------------------------------------------------- type images

    def _auth_headers(self) -> dict[str, str]:
        scheme = "Bearer" if self.token.startswith("nbt_") else "Token"
        return {"Authorization": f"{scheme} {self.token}", "Accept": "application/json"}

    def _image_path(self, reference: Any) -> Path:
        if not reference:
            raise SyncError("empty image reference")
        if self.images_dir is None:
            raise SyncError("no image directory configured")
        path = self.images_dir / Path(str(reference)).name
        if not path.is_file():
            raise SyncError(f"image file {path} does not exist")
        return path

    @staticmethod
    def _remote_basename(value: Any) -> str:
        if not value:
            return ""
        return str(value).rstrip("/").split("/")[-1]

    def _patch_device_type_image(self, obj: Any, field: str, path: Path) -> None:
        current = getattr(obj, field, None)
        # NetBox stores uploads under a generated filename, so the remote
        # basename cannot be compared with the source file. Upload once and
        # require --force-images to replace an existing image.
        if not self.force_images and current:
            return
        if not isinstance(getattr(obj, "id", None), int) or obj.id < 0:
            # The device type is created earlier in this dry run; its mock ID
            # cannot be used for an upload URL.
            self.counts["unchanged"] += 1
            return
        if not self.url or not self.token:
            raise SyncError("NetBox URL/token are required to upload device-type images")
        self.log("update", "device_type", f"{getattr(obj, 'slug', obj.id)} {field}", path.name)
        if self.dry_run:
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            response = requests.patch(
                f"{self.url}/api/dcim/device-types/{obj.id}/",
                headers=self._auth_headers(),
                files={field: (path.name, handle, mime)},
                timeout=60,
            )
        if response.status_code >= 300:
            raise SyncError(
                f"upload {field} for device type {getattr(obj, 'slug', obj.id)}: "
                f"HTTP {response.status_code}: {response.text[:300]}"
            )

    def sync_device_type_images(self, obj: Any, item: dict[str, Any]) -> None:
        images = item.get("images") or {}
        for field in ("front", "rear"):
            if images.get(field):
                self._patch_device_type_image(obj, f"{field}_image", self._image_path(images[field]))

        desired_other = images.get("other") or []
        desired_names: set[str] = set()
        existing = []
        if self.url and self.token:
            existing = list(
                self.nb.extras.image_attachments.filter(
                    object_type="dcim.devicetype", object_id=obj.id
                )
            )
        by_name = {str(getattr(att, "name", "")): att for att in existing}

        for entry in desired_other:
            if not isinstance(entry, dict):
                raise SyncError(f"device type {getattr(obj, 'slug', obj.id)}: image must be a mapping")
            name = str(require(entry, "name", "device type image"))
            path = self._image_path(entry.get("file"))
            desired_names.add(name)
            attachment = by_name.get(name)
            # NetBox renames uploaded files; do not compare basenames. Replace
            # an existing attachment only when --force-images is used.
            if attachment and not self.force_images:
                continue
            if not self.url or not self.token:
                raise SyncError("NetBox URL/token are required to upload device-type images")
            if not isinstance(getattr(obj, "id", None), int) or obj.id < 0:
                self.counts["unchanged"] += 1
                continue
            self.log(
                "update" if attachment else "create",
                "image_attachment",
                f"{getattr(obj, 'slug', obj.id)}/{name}",
                path.name,
            )
            if self.dry_run:
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            description = f"GitOps: {entry.get('description', '')}".strip()
            with path.open("rb") as handle:
                if attachment:
                    response = requests.patch(
                        f"{self.url}/api/extras/image-attachments/{attachment.id}/",
                        headers=self._auth_headers(),
                        data={
                            "object_type": "dcim.devicetype",
                            "object_id": obj.id,
                            "name": name,
                            "description": description,
                        },
                        files={"image": (path.name, handle, mime)},
                        timeout=60,
                    )
                else:
                    response = requests.post(
                        f"{self.url}/api/extras/image-attachments/",
                        headers=self._auth_headers(),
                        data={
                            "object_type": "dcim.devicetype",
                            "object_id": obj.id,
                            "name": name,
                            "description": description,
                        },
                        files={"image": (path.name, handle, mime)},
                        timeout=60,
                    )
            if response.status_code >= 300:
                raise SyncError(
                    f"upload image {name!r} for device type {getattr(obj, 'slug', obj.id)}: "
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )

        for attachment in existing:
            name = str(getattr(attachment, "name", ""))
            if name in desired_names:
                continue
            if not str(getattr(attachment, "description", "") or "").startswith("GitOps:"):
                continue
            if not self.url or not self.token:
                continue
            self.log("delete", "image_attachment", f"{getattr(obj, 'slug', obj.id)}/{name}")
            if self.dry_run:
                continue
            response = requests.delete(
                f"{self.url}/api/extras/image-attachments/{attachment.id}/",
                headers=self._auth_headers(),
                timeout=60,
            )
            if response.status_code >= 300:
                raise SyncError(
                    f"delete image {name!r} for device type {getattr(obj, 'slug', obj.id)}: "
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )

    def sync_racks(self) -> None:
        for item in self.inventory["racks"]:
            name = require(item, "name", "rack")
            site_slug = require(item, "site", "rack")
            if site_slug not in self.sites:
                raise SyncError(f"rack {name!r}: unknown site {site_slug!r}")
            data: dict[str, Any] = {
                "name": name,
                "site": self.sites[site_slug].id,
                "status": item.get("status", DEFAULT_STATUS),
                "width": int(item.get("width", 19)),
                "u_height": int(item.get("u_height", 42)),
                "desc_units": bool(item.get("desc_units", False)),
                "facility_id": item.get("facility_id", ""),
                "serial": item.get("serial", ""),
                "asset_tag": item.get("asset_tag"),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            obj = self.ensure(
                "rack",
                self.nb.dcim.racks,
                {"name": name, "site_id": data["site"]},
                data,
                tags=item.get("tags"),
                label=f"rack/{site_slug}/{name}",
            )
            self.racks[(site_slug, name)] = obj
            self.desired["racks"].add((site_slug, name))

    def sync_vlans(self) -> None:
        for item in self.inventory["vlans"]:
            vid = int(require(item, "vid", "VLAN"))
            name = require(item, "name", "VLAN")
            site_slug = item.get("site")
            data: dict[str, Any] = {
                "vid": vid,
                "name": name,
                "status": item.get("status", DEFAULT_STATUS),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            if site_slug:
                if site_slug not in self.sites:
                    raise SyncError(f"VLAN {vid}: unknown site {site_slug!r}")
                data["site"] = self.sites[site_slug].id
            label = f"vlan/{site_slug or 'global'}/{vid}"
            if site_slug:
                obj = self.ensure(
                    "vlan",
                    self.nb.ipam.vlans,
                    {"vid": vid, "site_id": data["site"]},
                    data,
                    tags=item.get("tags"),
                    label=label,
                )
            else:
                obj = self.reconcile(
                    "vlan",
                    self.nb.ipam.vlans,
                    self.find_global_vlan(vid),
                    data,
                    tags=item.get("tags"),
                    label=label,
                )
            self.vlans[(site_slug, vid)] = obj
            self.desired["vlans"].add((site_slug, vid))

    def sync_prefixes(self) -> None:
        for item in self.inventory["prefixes"]:
            prefix = normalize_prefix(require(item, "prefix", "prefix"))
            data: dict[str, Any] = {
                "prefix": prefix,
                "status": item.get("status", DEFAULT_STATUS),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
                "is_pool": bool(item.get("is_pool", False)),
            }
            site_slug = item.get("site")
            # NetBox 4.x scopes a prefix through scope_type/scope_id rather
            # than a plain `site` field.
            if site_slug:
                if site_slug not in self.sites:
                    raise SyncError(f"prefix {prefix}: unknown site {site_slug!r}")
                data["scope_type"] = "dcim.site"
                data["scope_id"] = self.sites[site_slug].id
            else:
                data["scope_type"] = None
                data["scope_id"] = None
            vlan_vid = item.get("vlan")
            if vlan_vid is not None:
                vlan = self.vlans.get((site_slug, int(vlan_vid))) or self.vlans.get((None, int(vlan_vid)))
                if vlan is None:
                    raise SyncError(f"prefix {prefix}: unknown VLAN {vlan_vid!r}")
                data["vlan"] = vlan.id
            obj = self.ensure(
                "prefix",
                self.nb.ipam.prefixes,
                {"prefix": prefix},
                data,
                tags=item.get("tags"),
                label=f"prefix/{prefix}",
            )
            self.desired["prefixes"].add(prefix)

    def sync_devices(self) -> None:
        # Remove the rack reservation of devices that are about to be pruned.
        # Otherwise a new device can collide with the old device that still
        # occupies its target unit until the prune phase runs.
        desired_devices = {
            (str(item.get("site")), str(item.get("name")))
            for item in self.inventory["devices"]
        }
        for device in self.nb.dcim.devices.all():
            if not self.is_managed(device):
                continue
            site_slug = (
                self.site_slugs_by_id.get(device.site.id)
                if getattr(device, "site", None)
                else None
            )
            if (site_slug, str(getattr(device, "name", ""))) in desired_devices:
                continue
            changes: dict[str, Any] = {}
            if getattr(device, "rack", None) or getattr(device, "position", None) is not None:
                changes.update({"rack": None, "position": None, "face": ""})
            if getattr(device, "primary_ip4", None) is not None:
                changes["primary_ip4"] = None
            if getattr(device, "primary_ip6", None) is not None:
                changes["primary_ip6"] = None
            if not changes:
                continue
            self.log("update", "device", f"{site_slug}/{device.name}", "clear before prune")
            if not self.dry_run:
                try:
                    device.update(changes)
                except Exception as exc:  # noqa: BLE001
                    raise SyncError(f"device {device.name}: clear before prune: {exc}") from exc

        # Rack positions are updated bottom-up. Moving a device down can
        # otherwise collide with a device that has not moved out of the way yet.
        devices = sorted(
            self.inventory["devices"],
            key=lambda item: (
                item.get("position") is None,
                int(item.get("position")) if item.get("position") is not None else 0,
                str(item.get("name")),
            ),
        )
        for item in devices:
            name = str(require(item, "name", "device"))
            site_slug = require(item, "site", "device")
            role_slug = require(item, "role", "device")
            device_type_slug = require(item, "device_type", "device")

            if site_slug not in self.sites:
                raise SyncError(f"device {name}: unknown site {site_slug!r}")
            if role_slug not in self.roles:
                raise SyncError(f"device {name}: unknown device role {role_slug!r}")
            if device_type_slug not in self.device_types:
                raise SyncError(f"device {name}: unknown device type {device_type_slug!r}")

            rack = None
            position = None
            face = None
            rack_name = item.get("rack")
            if rack_name:
                rack = self.racks.get((site_slug, rack_name))
                if rack is None:
                    raise SyncError(f"device {name}: unknown rack {rack_name!r}")
                position = item.get("position")
                face = item.get("face")

            platform = None
            platform_slug = item.get("platform")
            if platform_slug:
                platform = self.platforms.get(platform_slug)
                if platform is None:
                    raise SyncError(f"device {name}: unknown platform {platform_slug!r}")

            data: dict[str, Any] = {
                "name": name,
                "site": self.sites[site_slug].id,
                "role": self.roles[role_slug].id,
                "device_type": self.device_types[device_type_slug].id,
                "status": item.get("status", DEFAULT_STATUS),
                "platform": platform.id if platform else None,
                "rack": rack.id if rack else None,
                "position": int(position) if position is not None else None,
                # Device.face is a non-null CharField with a blank default; use
                # "" rather than None when the device is not rack-mounted.
                "face": face or "",
                "serial": item.get("serial", ""),
                "asset_tag": item.get("asset_tag"),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            device = self.ensure(
                "device",
                self.nb.dcim.devices,
                {"name": name, "site_id": data["site"]},
                data,
                tags=item.get("tags"),
                label=f"device/{site_slug}/{name}",
            )
            self.devices[name] = device
            self.device_sites[name] = site_slug
            self.desired["devices"].add((site_slug, name))

            for interface in item.get("interfaces", []) or []:
                self.sync_interface(device, name, site_slug, interface)

    def sync_interface(self, device: Any, device_name: str, site_slug: str, item: dict[str, Any]) -> None:
        name = str(require(item, "name", f"interface on {device_name}"))
        key = (device_name, name)
        if key in self.interfaces:
            raise SyncError(f"device {device_name}: duplicate interface {name!r}")

        untagged_vlan = None
        tagged_vlans: list[Any] = []
        mode = item.get("mode")
        if item.get("untagged_vlan") is not None:
            untagged_vlan = self.resolve_vlan(site_slug, int(item["untagged_vlan"]))
            if not mode:
                mode = "access"
        for vid in as_list(item.get("tagged_vlans")):
            tagged_vlans.append(self.resolve_vlan(site_slug, int(vid)))
            if not mode:
                mode = "tagged"
        if untagged_vlan is not None and tagged_vlans and not item.get("mode"):
            mode = "tagged"

        data: dict[str, Any] = {
            "device": device.id,
            "name": name,
            "type": item.get("type", "other"),
            "enabled": bool(item.get("enabled", True)),
            "mgmt_only": bool(item.get("mgmt_only", False)),
            "description": item.get("description", ""),
            "mtu": int(item["mtu"]) if item.get("mtu") is not None else None,
            "mac_address": normalize_mac(item["mac_address"]) if item.get("mac_address") else None,
            "mode": mode or "",
            "untagged_vlan": untagged_vlan.id if untagged_vlan else None,
            "tagged_vlans": [vlan.id for vlan in tagged_vlans],
        }
        interface = self.ensure(
            "interface",
            self.nb.dcim.interfaces,
            {"device_id": device.id, "name": name},
            data,
            tags=item.get("tags"),
            label=f"interface/{device_name}/{name}",
        )
        self.interfaces[key] = interface
        self.desired["interfaces"].add(key)

        for ip in item.get("ip_addresses", []) or []:
            self.sync_ip(ip, device, device_name, interface)

    def sync_ip(
        self,
        item: dict[str, Any],
        device: Any | None,
        device_name: str | None,
        interface: Any | None,
    ) -> Any:
        address = normalize_address(require(item, "address", "IP address"))
        data: dict[str, Any] = {
            "address": address,
            "status": item.get("status", DEFAULT_STATUS),
            "dns_name": item.get("dns_name", ""),
            "description": item.get("description", ""),
            "role": item.get("role") or "",
        }
        if interface is not None:
            data["assigned_object_type"] = "dcim.interface"
            data["assigned_object_id"] = interface.id
        else:
            data["assigned_object_type"] = None
            data["assigned_object_id"] = None

        obj = self.ensure(
            "ip_address",
            self.nb.ipam.ip_addresses,
            {"address": address},
            data,
            tags=item.get("tags"),
            label=f"ip/{address}",
        )
        self.ip_addresses[address] = obj
        self.desired["ip_addresses"].add(address)

        if item.get("primary"):
            if device is None or device_name is None:
                raise SyncError(f"IP {address}: primary requires a device and interface")
            if interface is None:
                raise SyncError(f"IP {address}: primary requires an interface")
            version = ipaddress.ip_interface(address).version
            primaries = self.device_primary.setdefault(device_name, {})
            if version in primaries and primaries[version] != address:
                raise SyncError(f"device {device_name}: more than one primary IPv{version} address")
            primaries[version] = address

        return obj

    def sync_standalone_ips(self) -> None:
        for item in self.inventory["ip_addresses"]:
            device_name = item.get("device")
            interface_name = item.get("interface")
            device = None
            interface = None
            if device_name:
                device = self.devices.get(device_name)
                if device is None:
                    raise SyncError(f"IP {item.get('address')}: unknown device {device_name!r}")
                if interface_name:
                    interface = self.interfaces.get((device_name, interface_name))
                    if interface is None:
                        raise SyncError(
                            f"IP {item.get('address')}: unknown interface "
                            f"{device_name}/{interface_name}"
                        )
            self.sync_ip(item, device, device_name, interface)

    def sync_primary_ips(self) -> None:
        for device_name, device in self.devices.items():
            wanted = self.device_primary.get(device_name, {})
            for version in (4, 6):
                field = f"primary_ip{version}"
                address = wanted.get(version)
                desired_obj = self.ip_addresses.get(address) if address else None
                desired_id = desired_obj.id if desired_obj is not None else None
                current = getattr(device, field, None)
                current_id = getattr(current, "id", None) if current is not None else None
                if current_id == desired_id:
                    continue
                self.log("update", "device", f"{device_name} {field}", address or "clear")
                if not self.dry_run:
                    try:
                        device.update({field: desired_id})
                    except Exception as exc:  # noqa: BLE001
                        raise SyncError(f"device {device_name}: set {field}: {exc}") from exc

    def termination_key(self, terminations: Any) -> tuple[tuple[str, int], ...]:
        keys: list[tuple[str, int]] = []
        for term in terminations or []:
            if isinstance(term, dict):
                object_type = term.get("object_type")
                object_id = term.get("object_id")
            else:
                object_type = getattr(term, "object_type", None)
                object_id = getattr(term, "object_id", None)
            if object_id is None:
                continue
            keys.append((str(object_type), int(object_id)))
        return tuple(sorted(keys))

    def cable_key(self, cable: Any) -> tuple[Any, Any]:
        sides = [
            self.termination_key(getattr(cable, "a_terminations", [])),
            self.termination_key(getattr(cable, "b_terminations", [])),
        ]
        return tuple(sorted(sides))

    def sync_cables(self) -> None:
        resolved: list[tuple[dict[str, Any], tuple[Any, Any], Any, Any]] = []
        desired: set[Any] = set()
        for item in self.inventory["cables"]:
            a_device = require(item, "a_device", "cable")
            a_interface = require(item, "a_interface", "cable")
            b_device = require(item, "b_device", "cable")
            b_interface = require(item, "b_interface", "cable")
            a = self.interfaces.get((a_device, a_interface))
            b = self.interfaces.get((b_device, b_interface))
            if a is None:
                raise SyncError(f"cable: unknown interface {a_device}/{a_interface}")
            if b is None:
                raise SyncError(f"cable: unknown interface {b_device}/{b_interface}")
            a_terms = (("dcim.interface", int(a.id)),)
            b_terms = (("dcim.interface", int(b.id)),)
            key = tuple(sorted([a_terms, b_terms]))
            resolved.append((item, key, a, b))
            desired.add(key)

        # Set the desired set first so prune_cables can remove stale cables
        # before a new cable needs one of the same terminations.
        self.desired["cables"] = desired
        if self.prune:
            self.prune_cables()

        existing = {self.cable_key(cable): cable for cable in self.nb.dcim.cables.all()}
        for item, key, a, b in resolved:
            a_device = require(item, "a_device", "cable")
            a_interface = require(item, "a_interface", "cable")
            b_device = require(item, "b_device", "cable")
            b_interface = require(item, "b_interface", "cable")
            data: dict[str, Any] = {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": a.id}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": b.id}],
                "type": item.get("type", "cat6"),
                "status": item.get("status", "connected"),
                "label": item.get("label", ""),
                "description": item.get("description", ""),
                "color": normalize_color(item["color"]) if item.get("color") else "000000",
            }
            cable = self.reconcile(
                "cable",
                self.nb.dcim.cables,
                existing.get(key),
                data,
                tags=item.get("tags"),
                label=f"cable/{a_device}/{a_interface}<->{b_device}/{b_interface}",
                compare_fields=("type", "status", "label", "description", "color", "tags"),
            )
            self.cables[key] = cable

    # ------------------------------------------------------------------ pruning

    def prune_model(
        self,
        kind: str,
        endpoint: Any,
        desired: set[Any],
        key_func: Callable[[Any], Any],
        skip: Callable[[Any], bool] | None = None,
        label_func: Callable[[Any], str] | None = None,
    ) -> None:
        for obj in endpoint.all():
            if not self.is_managed(obj):
                continue
            if skip is not None and skip(obj):
                continue
            key = key_func(obj)
            if key in desired:
                continue
            label = label_func(key) if label_func is not None else format_key(key)
            self.log("delete", kind, label)
            if self.dry_run:
                continue
            try:
                obj.delete()
            except Exception as exc:  # noqa: BLE001
                raise SyncError(f"delete {kind} {label}: {exc}") from exc

    def prune_cables(self) -> None:
        def label(key: tuple[Any, Any]) -> str:
            sides = ["/".join(str(part) for part in side if part is not None) for side in key]
            return " <-> ".join(sides)

        self.prune_model(
            "cable",
            self.nb.dcim.cables,
            self.desired["cables"],
            self.cable_key,
            label_func=label,
        )

    def prune_devices(self) -> None:
        if not self.prune:
            return
        for device in self.nb.dcim.devices.all():
            if not self.is_managed(device):
                continue
            site_slug = (
                self.site_slugs_by_id.get(device.site.id)
                if getattr(device, "site", None)
                else None
            )
            key = (site_slug, device.name)
            if key in self.desired["devices"]:
                continue
            self.log("delete", "device", format_key(key))
            self.planned_delete_devices.add(device.name)
            if self.dry_run:
                continue
            try:
                device.delete()
            except Exception as exc:  # noqa: BLE001
                raise SyncError(f"delete device {format_key(key)}: {exc}") from exc

    def prune_interfaces(self) -> None:
        def skip(interface: Any) -> bool:
            device = getattr(interface, "device", None)
            return getattr(device, "name", None) in self.planned_delete_devices

        self.prune_model(
            "interface",
            self.nb.dcim.interfaces,
            self.desired["interfaces"],
            lambda interface: (interface.device.name, interface.name),
            skip=skip,
        )

    def prune_ip_addresses(self) -> None:
        self.prune_model("ip_address", self.nb.ipam.ip_addresses, self.desired["ip_addresses"], lambda ip: ip.address)

    def prune_racks(self) -> None:
        self.prune_model(
            "rack",
            self.nb.dcim.racks,
            self.desired["racks"],
            lambda rack: (
                self.site_slugs_by_id.get(rack.site.id)
                if getattr(rack, "site", None)
                else None,
                rack.name,
            ),
        )

    def prune_prefixes(self) -> None:
        self.prune_model("prefix", self.nb.ipam.prefixes, self.desired["prefixes"], lambda prefix: prefix.prefix)

    def prune_vlans(self) -> None:
        self.prune_model(
            "vlan",
            self.nb.ipam.vlans,
            self.desired["vlans"],
            lambda vlan: (
                self.site_slugs_by_id.get(vlan.site.id)
                if getattr(vlan, "site", None)
                else None,
                vlan.vid,
            ),
        )

    def prune_device_types(self) -> None:
        self.prune_model("device_type", self.nb.dcim.device_types, self.desired["device_types"], lambda item: item.slug)

    def prune_platforms(self) -> None:
        self.prune_model("platform", self.nb.dcim.platforms, self.desired["platforms"], lambda item: item.slug)

    def prune_device_roles(self) -> None:
        self.prune_model("device_role", self.nb.dcim.device_roles, self.desired["device_roles"], lambda item: item.slug)

    def prune_manufacturers(self) -> None:
        self.prune_model("manufacturer", self.nb.dcim.manufacturers, self.desired["manufacturers"], lambda item: item.slug)

    def prune_sites(self) -> None:
        self.prune_model("site", self.nb.dcim.sites, self.desired["sites"], lambda item: item.slug)

    def prune_all(self) -> None:
        if not self.prune:
            return
        # Delete cables and devices first so their child interfaces and IPs do
        # not need to be removed one by one. Everything is tag-gated and these
        # phases only touch objects carrying the gitops tag.
        self.prune_cables()
        self.prune_devices()
        self.prune_interfaces()
        self.prune_ip_addresses()
        self.prune_racks()
        self.prune_prefixes()
        self.prune_vlans()
        self.prune_device_types()
        self.prune_platforms()
        self.prune_device_roles()
        self.prune_manufacturers()
        self.prune_sites()

    # --------------------------------------------------------------------- run

    def run(self) -> None:
        self.ensure_tag(MANAGED_TAG_NAME)
        self.sync_sites()
        self.sync_manufacturers()
        self.sync_platforms()
        self.sync_device_roles()
        self.sync_device_types()
        self.sync_racks()
        self.sync_vlans()
        self.sync_prefixes()
        self.sync_devices()
        self.sync_standalone_ips()
        self.sync_primary_ips()
        self.sync_cables()
        self.prune_all()
        self.summary()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        default=os.environ.get("NETBOX_INVENTORY_DIR", "/inventory"),
        help="directory containing inventory YAML files (default: /inventory)",
    )
    parser.add_argument("--url", default=os.environ.get("NETBOX_URL"), help="NetBox base URL")
    parser.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"), help="NetBox API token")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=env_bool("DRY_RUN", False),
        help="show the plan without writing to NetBox",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        default=env_bool("PRUNE", False),
        help="delete gitops-tagged objects that are no longer in the inventory",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the inventory and exit without contacting NetBox",
    )
    parser.add_argument(
        "--images",
        default=os.environ.get("NETBOX_IMAGES_DIR"),
        help="directory containing device-type images (default: <inventory>/images)",
    )
    parser.add_argument(
        "--force-images",
        action="store_true",
        default=env_bool("FORCE_IMAGES", False),
        help="re-upload device-type images even when the filename already matches",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=int(os.environ.get("NETBOX_WAIT_SECONDS", "120")),
        help="seconds to wait for NetBox to become reachable (default: 120)",
    )
    args = parser.parse_args(argv)

    inventory = load_inventory(Path(args.inventory))
    validate_inventory(inventory)
    if args.check:
        return 0

    if not args.url:
        raise SyncError("NETBOX_URL is not set (or pass --url)")
    if not args.token:
        raise SyncError("NETBOX_TOKEN is not set (or pass --token)")

    nb = pynetbox.api(args.url.rstrip("/"), token=args.token)
    wait_for_netbox(nb, args.url, args.wait)
    images_dir = Path(args.images) if args.images else Path(args.inventory) / "images"
    Reconciler(
        nb,
        inventory,
        dry_run=args.dry_run,
        prune=args.prune,
        url=args.url,
        token=args.token,
        images_dir=images_dir,
        force_images=args.force_images,
    ).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - keep CronJob logs readable
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
