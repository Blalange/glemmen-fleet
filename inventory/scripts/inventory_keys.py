"""Shared inventory file discovery and natural keys.

The collector, sync job and language server all need to agree on which YAML
files make up the inventory and how objects are identified. Keep those rules
here instead of reimplementing them in each layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inventory_util import normalize_address, normalize_prefix, slugify

IGNORED_INVENTORY_FILES = {"kustomization.yaml", "kustomization.yml"}


def inventory_files(root: Path) -> list[Path]:
    """Return the YAML files that make up the inventory under *root*.

    Hidden paths are skipped because a Kubernetes ConfigMap volume contains a
    visible symlink plus a hidden ``..data`` tree. Resolved paths are
    de-duplicated so the same file is not loaded twice.
    """

    root = Path(root)
    files: set[Path] = set()
    for pattern in ("*.yaml", "*.yml"):
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if path.name in IGNORED_INVENTORY_FILES:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            try:
                files.add(path.resolve())
            except OSError:
                files.add(path)
    return sorted(files)


def natural_key(section: str, item: dict[str, Any]) -> Any:
    """Return the natural key used by the inventory layer merge.

    This is intentionally lenient: malformed values surface as validation
    problems separately. The key functions mirror the merge keys in
    :func:`sync_inventory._merge_section`.
    """

    if section == "module_types":
        return str(
            item.get("slug")
            or slugify(
                f"{item.get('manufacturer')}-{item.get('model') or item.get('name') or ''}"
            )
        )
    if section in {"owner_groups", "owners", "contacts"}:
        return str(item.get("name") or "")
    if section in {
        "sites",
        "manufacturers",
        "platforms",
        "device_roles",
        "contact_roles",
        "device_types",
        "module_bay_types",
    }:
        return str(item.get("slug") or slugify(item.get("name") or item.get("model") or ""))
    if section == "racks":
        return (str(item.get("site")), str(item.get("name")))
    if section == "vlans":
        return (item.get("site"), int(item.get("vid")))
    if section == "prefixes":
        return normalize_prefix(item.get("prefix"))
    if section == "devices":
        return str(item.get("name"))
    if section == "ip_addresses":
        return normalize_address(item.get("address"))
    if section == "cables":
        return cable_key(item)
    return None


def cable_key(item: dict[str, Any]) -> tuple[Any, Any]:
    """Return the order-independent key for a cable's two endpoints."""

    return tuple(
        sorted(
            [
                (str(item.get("a_device")), str(item.get("a_interface"))),
                (str(item.get("b_device")), str(item.get("b_interface"))),
            ]
        )
    )


def module_bay_key(device_name: str, bay_name: str) -> tuple[str, str]:
    return (str(device_name), str(bay_name))


def interface_key(device_name: str, interface_name: str) -> tuple[str, str]:
    return (str(device_name), str(interface_name))
