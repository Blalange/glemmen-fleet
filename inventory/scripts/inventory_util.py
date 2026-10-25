"""Shared normalization and comparison helpers."""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any


class InventoryError(RuntimeError):
    """A user-facing inventory error."""


# Kept for compatibility with the sync code and existing call sites.
SyncError = InventoryError


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
    # pynetbox wraps JSON objects such as ModuleType.attributes in a Record.
    # Normalize it before comparing with the plain inventory dictionary. Only
    # do this when the desired value is a dict, so foreign-key fields are not
    # expanded into their full serialized object.
    if isinstance(desired, dict) and not isinstance(current, (dict, type(None))) and hasattr(
        current, "serialize"
    ):
        try:
            current = current.serialize()
        except Exception:  # noqa: BLE001 - fall back to the raw object
            pass
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
