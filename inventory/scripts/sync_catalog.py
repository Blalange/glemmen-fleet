"""Merge Device Type Library definitions into the in-memory inventory.

The inventory only stores DTL references. Definitions and elevation images are
fetched at sync time into a runtime cache and merged in memory. Manual values
and manually declared components always win.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from dtl_catalog import DtlCatalog, DtlError

# Internal key -> (DTL YAML key, allowed template fields).
COMPONENT_SCHEMAS: dict[str, tuple[str, set[str]]] = {
    "interfaces": (
        "interfaces",
        {
            "name",
            "label",
            "type",
            "channels",
            "channel_id",
            "enabled",
            "mgmt_only",
            "description",
            "poe_mode",
            "poe_type",
            "rf_role",
        },
    ),
    "console_ports": (
        "console-ports",
        {"name", "label", "type", "description"},
    ),
    "console_server_ports": (
        "console-server-ports",
        {"name", "label", "type", "description"},
    ),
    "power_ports": (
        "power-ports",
        {"name", "label", "type", "maximum_draw", "allocated_draw", "description"},
    ),
    "power_outlets": (
        "power-outlets",
        {"name", "label", "type", "maximum_draw", "allocated_draw", "description"},
    ),
    "rear_ports": (
        "rear-ports",
        {"name", "label", "type", "color", "positions", "description"},
    ),
    "front_ports": (
        "front-ports",
        {"name", "label", "type", "color", "positions", "description"},
    ),
    "module_bays": (
        "module-bays",
        {"name", "label", "position", "enabled", "description"},
    ),
    "device_bays": (
        "device-bays",
        {"name", "label", "enabled", "description"},
    ),
}

DEVICE_TYPE_FIELDS = (
    "u_height",
    "is_full_depth",
    "part_number",
    "description",
    "comments",
    "airflow",
    "weight",
    "weight_unit",
    "subdevice_role",
)

MODULE_TYPE_FIELDS = (
    "profile",
    "part_number",
    "description",
    "comments",
    "weight",
    "weight_unit",
)


@dataclass
class CatalogReport:
    device_types_enriched: int = 0
    module_types_enriched: int = 0
    components_added: int = 0
    fields_added: int = 0
    images_added: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"device_types={self.device_types_enriched}, "
            f"module_types={self.module_types_enriched}, "
            f"components={self.components_added}, fields={self.fields_added}, "
            f"images={self.images_added}, skipped={self.skipped}, "
            f"errors={len(self.errors)}"
        )


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _clean(value: Any) -> Any:
    # NetBox strips trailing whitespace/newlines from text fields. Normalize
    # before comparison so DTL comments do not cause an update on every sync.
    if isinstance(value, str):
        return value.rstrip()
    return value


def _normalize_component(section: str, raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    allowed = COMPONENT_SCHEMAS[section][1]
    component = {}
    for key, value in raw.items():
        if key not in allowed:
            continue
        value = _clean(value)
        if value is None or value == "":
            continue
        component[key] = value
    name = str(component.get("name") or "").strip()
    if not name:
        return None
    component["name"] = name
    if section in {"interfaces", "front_ports", "rear_ports"} and not component.get("type"):
        return None
    return component


def _merge_components(
    item: dict[str, Any], definition: dict[str, Any], kind: str
) -> tuple[bool, int]:
    changed = False
    added = 0
    for section, (yaml_key, _allowed) in COMPONENT_SCHEMAS.items():
        if kind == "module-type" and section == "device_bays":
            continue
        values = definition.get(yaml_key)
        if values is None:
            values = definition.get(section)
        if not isinstance(values, list):
            continue
        components = [
            component
            for component in (_normalize_component(section, raw) for raw in values)
            if component is not None
        ]
        if not components:
            continue
        current = item.get(section)
        if current is None:
            item[section] = components
            changed = True
            added += len(components)
            continue
        if not isinstance(current, list):
            continue
        by_name = {
            str(component.get("name")): component
            for component in current
            if isinstance(component, dict) and component.get("name")
        }
        for component in components:
            name = str(component["name"])
            existing = by_name.get(name)
            if existing is None:
                current.append(component)
                by_name[name] = component
                changed = True
                added += 1
                continue
            for key, value in component.items():
                if key == "name":
                    continue
                if _missing(existing.get(key)):
                    existing[key] = value
                    changed = True
    return changed, added


def _merge_definition(item: dict[str, Any], definition: dict[str, Any], kind: str) -> tuple[bool, int, int]:
    changed = False
    fields_added = 0
    fields = DEVICE_TYPE_FIELDS if kind == "device-type" else MODULE_TYPE_FIELDS
    for key in fields:
        if key not in definition:
            continue
        if _missing(item.get(key)):
            item[key] = _clean(definition[key])
            changed = True
            fields_added += 1
    if kind == "module-type" and isinstance(definition.get("attributes"), dict):
        attributes = item.setdefault("attributes", {})
        if isinstance(attributes, dict):
            for key, value in definition["attributes"].items():
                if _missing(attributes.get(key)):
                    attributes[key] = value
                    changed = True
                    fields_added += 1
    components_changed, components_added = _merge_components(item, definition, kind)
    return changed or components_changed, fields_added, components_added


def _attach_images(
    item: dict[str, Any],
    definition: dict[str, Any],
    catalog: DtlCatalog,
    *,
    fetch_images: bool,
) -> int:
    if not fetch_images:
        return 0
    images = item.get("images")
    if not isinstance(images, dict):
        images = {}
    catalog_images: dict[str, str] = {}
    added = 0
    other = images.get("other")
    if not isinstance(other, list):
        other = []
    for face in ("front", "rear"):
        path = catalog.image_path(definition, face)
        if path is None:
            continue
        if images.get(face):
            # The manually curated front/rear image wins. Keep the DTL
            # elevation image available as an attachment instead of dropping
            # it, so the catalog image is still included in NetBox.
            name = f"DTL {face} elevation"
            if not any(
                isinstance(entry, dict) and entry.get("name") == name
                for entry in other
            ):
                other.append(
                    {
                        "name": name,
                        "file": str(path),
                        "description": f"Device Type Library {face} elevation image.",
                    }
                )
                added += 1
        else:
            catalog_images[face] = str(path)
            added += 1
    if catalog_images:
        item["_catalog_images"] = catalog_images
    if added and other:
        images["other"] = other
        item["images"] = images
    return added


def _manufacturer_name(slug: str, manufacturers: dict[str, str]) -> str:
    return manufacturers.get(slug, slug.replace("-", " ").title())


def enrich_inventory(
    inventory: dict[str, list[Any]],
    catalog: DtlCatalog,
    *,
    manufacturers: dict[str, str] | None = None,
    fetch_images: bool = True,
) -> CatalogReport:
    manufacturers = manufacturers or {}
    report = CatalogReport()
    for item in inventory.get("device_types", []) or []:
        if not isinstance(item, dict):
            continue
        explicit_ref = bool(item.get("dtl"))
        try:
            definition = catalog.resolve(
                item,
                kind="device-type",
                manufacturer_name=_manufacturer_name(
                    str(item.get("manufacturer") or ""), manufacturers
                ),
            )
        except DtlError as exc:
            label = item.get("dtl") or item.get("slug") or item.get("model")
            message = f"device type {label}: {exc}"
            report.errors.append(message)
            if explicit_ref:
                item["_dtl_failed"] = True
                print(f"WARNING: DTL {message}", file=sys.stderr)
            continue
        if definition is None:
            if explicit_ref:
                item["_dtl_failed"] = True
                print(
                    f"WARNING: DTL reference for {item.get('slug') or item.get('model')} "
                    "was not found",
                    file=sys.stderr,
                )
            report.skipped += 1
            continue
        changed, fields_added, components_added = _merge_definition(
            item, definition, "device-type"
        )
        images_added = _attach_images(item, definition, catalog, fetch_images=fetch_images)
        if changed or images_added:
            report.device_types_enriched += 1
        report.fields_added += fields_added
        report.components_added += components_added
        report.images_added += images_added

    for item in inventory.get("module_types", []) or []:
        if not isinstance(item, dict):
            continue
        explicit_ref = bool(item.get("dtl"))
        try:
            definition = catalog.resolve(
                item,
                kind="module-type",
                manufacturer_name=_manufacturer_name(
                    str(item.get("manufacturer") or ""), manufacturers
                ),
            )
        except DtlError as exc:
            label = item.get("dtl") or item.get("slug") or item.get("model")
            message = f"module type {label}: {exc}"
            report.errors.append(message)
            if explicit_ref:
                item["_dtl_failed"] = True
                print(f"WARNING: DTL {message}", file=sys.stderr)
            continue
        if definition is None:
            if explicit_ref:
                item["_dtl_failed"] = True
                print(
                    f"WARNING: DTL reference for {item.get('slug') or item.get('model')} "
                    "was not found",
                    file=sys.stderr,
                )
            report.skipped += 1
            continue
        changed, fields_added, components_added = _merge_definition(
            item, definition, "module-type"
        )
        images_added = _attach_images(item, definition, catalog, fetch_images=fetch_images)
        if changed or images_added:
            report.module_types_enriched += 1
        report.fields_added += fields_added
        report.components_added += components_added
        report.images_added += images_added
    return report
