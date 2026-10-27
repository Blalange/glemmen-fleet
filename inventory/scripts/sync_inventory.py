"""Load, merge and expand the YAML inventory layers."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import yaml
from inventory_keys import cable_key, inventory_files, natural_key
from inventory_problems import SECTION_KINDS, Problem, problem
from inventory_racks import plan_rack_layout
from inventory_schema import AUXILIARY_KEYS, LAYER_ORDER, SECTIONS
from inventory_util import SyncError, as_list


def _normalize_merge_value(key: str, current: Any, desired: Any) -> Any:
    if key in {"tags", "sources", "tagged_vlans"}:
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


def _stamp_sources(item: dict[str, Any], layer: str, inherited: Any = None) -> None:
    """Attach provenance sources to an inventory item and its components.

    Manual objects always carry ``manual``. Generated objects keep the sources
    supplied by the collector; nested components inherit the parent's sources
    unless they declare a more specific source themselves.
    """
    sources = {
        str(source)
        for source in as_list(item.get("sources"))
        if source is not None and str(source)
    }
    if layer == "manual":
        sources.add("manual")
    elif not sources:
        sources = {
            str(source)
            for source in as_list(inherited)
            if source is not None and str(source)
        }
        if not sources:
            sources = {"generated"}
    item["sources"] = sorted(sources)

    for key in ("interfaces", "module_bays", "modules", "ip_addresses", "contacts"):
        nested_items = item.get(key)
        if not isinstance(nested_items, list):
            continue
        for nested in nested_items:
            if isinstance(nested, dict):
                _stamp_sources(nested, layer, item["sources"])


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

def _merge_section(
    section: str,
    generated: list[Any],
    manual: list[Any],
    problems: list[Problem] | None = None,
) -> list[dict[str, Any]]:
    merged: dict[Any, dict[str, Any]] = {}
    layers: dict[Any, str] = {}
    kind = SECTION_KINDS.get(section)
    for layer, items in (("generated", generated), ("manual", manual)):
        for raw in items:
            if not isinstance(raw, dict):
                message = f"{section}: every entry must be a mapping"
                if problems is not None:
                    problems.append(problem("invalid-object", message, kind=kind, key=None))
                    continue
                raise SyncError(message)
            try:
                key = natural_key(section, raw)
            except (TypeError, ValueError, SyncError) as exc:
                message = f"{section}: invalid key: {exc}"
                if problems is not None:
                    problems.append(
                        problem("invalid-key", message, kind=kind, key=None, layer=layer)
                    )
                    continue
                raise SyncError(message) from exc
            if key in merged:
                if layers[key] == layer:
                    message = f"{section}: duplicate key {key!r} in the {layer} layer"
                    if problems is not None:
                        problems.append(
                            problem(
                                "duplicate-definition",
                                message,
                                kind=kind,
                                key=key,
                                layer=layer,
                            )
                        )
                        continue
                    raise SyncError(message)
                merged[key] = _merge_mapping(merged[key], raw)
            else:
                merged[key] = copy.deepcopy(raw)
                layers[key] = layer
    return list(merged.values())

def _expand_uplinks(inventory: dict[str, list[Any]]) -> None:
    cables_by_key: dict[Any, dict[str, Any]] = {
        cable_key(cable): cable for cable in inventory["cables"]
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
                "sources": ["manual"],
            }
            key = cable_key(cable)
            if key in cables_by_key:
                existing = cables_by_key[key]
                sources = sorted(
                    {*as_list(existing.get("sources")), *as_list(cable["sources"])}
                )
                existing.update(cable)
                existing["sources"] = sources
            else:
                cables_by_key[key] = cable
    inventory["cables"] = list(cables_by_key.values())

def _expand_rack_layouts(
    inventory: dict[str, list[Any]],
    problems: list[Problem] | None = None,
) -> None:
    devices = {str(device.get("name")): device for device in inventory["devices"]}
    device_types = {
        str(natural_key("device_types", item)): item
        for item in inventory["device_types"]
    }

    for rack in inventory["racks"]:
        layout = rack.pop("layout", None)
        if not layout:
            continue
        rack_copy = {**rack, "layout": layout}
        plan = plan_rack_layout(rack_copy, devices, device_types)
        if plan.problems:
            if problems is not None:
                problems.extend(plan.problems)
            else:
                raise SyncError(plan.problems[0].message)
        for name, (start, _end, face) in plan.positions.items():
            device = devices[name]
            device["rack"] = rack["name"]
            device["position"] = start
            device["face"] = face

def load_inventory(
    directory: Path,
    problems: list[Problem] | None = None,
) -> dict[str, list[Any]]:
    if not directory.is_dir():
        raise SyncError(f"inventory directory {directory} does not exist")

    files = inventory_files(directory)
    if not files:
        raise SyncError(f"no YAML files found under {directory}")

    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(files):
        try:
            loaded = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            raise SyncError(f"{path}: {exc}") from exc
        for document in loaded:
            if document is None:
                continue
            if not isinstance(document, dict):
                raise SyncError(f"{path}: every YAML document must be a mapping")
            documents.append((path, document))
    return assemble_inventory(documents, problems=problems)


def assemble_inventory(
    documents: list[tuple[Path, dict[str, Any]]],
    problems: list[Problem] | None = None,
    *,
    warn: bool = True,
) -> dict[str, list[Any]]:
    """Merge already-loaded YAML documents into the inventory model.

    The language server uses this with parsed documents so it can share the
    same defaults, layer merge, uplink and rack expansion as the sync job.
    """

    by_layer: dict[str, dict[str, list[Any]]] = {
        layer: {section: [] for section in SECTIONS} for layer in LAYER_ORDER
    }
    defaults: dict[str, Any] = {}

    for path, document in documents:
        layer = str(document.get("layer") or "manual").strip().lower()
        if layer not in LAYER_ORDER:
            raise SyncError(f"{path}: unknown layer {layer!r}; expected generated or manual")
        for key, value in document.items():
            if key == "layer":
                continue
            if key == "defaults":
                if not isinstance(value, dict):
                    raise SyncError(f"{path}: defaults must be a mapping")
                defaults.update(copy.deepcopy(value))
            elif key in AUXILIARY_KEYS:
                continue
            elif key in by_layer[layer]:
                if value is None:
                    continue
                if not isinstance(value, list):
                    raise SyncError(f"{path}: {key} must be a list")
                stamped = copy.deepcopy(value)
                for raw in stamped:
                    if isinstance(raw, dict):
                        _stamp_sources(raw, layer)
                by_layer[layer][key].extend(stamped)
            elif warn:
                print(f"WARNING: {path}: ignoring unknown top-level key {key!r}", file=sys.stderr)

    inventory: dict[str, list[Any]] = {}
    for section in SECTIONS:
        inventory[section] = _merge_section(
            section,
            by_layer["generated"][section],
            by_layer["manual"][section],
            problems=problems,
        )

    for device in inventory["devices"]:
        for key, value in defaults.items():
            if key == "tags":
                device["tags"] = sorted({*as_list(device.get("tags")), *as_list(value)})
            elif key not in device:
                device[key] = copy.deepcopy(value)

    _expand_uplinks(inventory)
    _expand_rack_layouts(inventory, problems=problems)
    return inventory
