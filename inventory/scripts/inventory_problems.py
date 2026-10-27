"""Structured inventory problems shared by the sync job and language server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SECTION_KINDS = {
    "sites": "site",
    "manufacturers": "manufacturer",
    "platforms": "platform",
    "device_roles": "device_role",
    "owner_groups": "owner_group",
    "owners": "owner",
    "contact_roles": "contact_role",
    "contacts": "contact",
    "module_bay_types": "module_bay_type",
    "device_types": "device_type",
    "module_types": "module_type",
    "racks": "rack",
    "vlans": "vlan",
    "prefixes": "prefix",
    "devices": "device",
    "ip_addresses": "ip_address",
    "cables": "cable",
}


@dataclass(frozen=True)
class ProblemLocation:
    """Semantic location of a problem inside the inventory model.

    ``kind`` and ``key`` identify a definition or object. ``field_path`` is
    relative to that object and may contain list indices, for example
    ``("interfaces", 0, "untagged_vlan")``. The language server turns this
    into a file/line range; the sync job uses it for reporting.
    """

    kind: str
    key: Any
    field_path: tuple[Any, ...] = ()
    layer: str | None = None


@dataclass
class Problem:
    code: str
    message: str
    severity: str = "error"
    location: ProblemLocation | None = None
    related: list[ProblemLocation] = field(default_factory=list)


def problem(
    code: str,
    message: str,
    *,
    severity: str = "error",
    kind: str | None = None,
    key: Any = None,
    field_path: tuple[Any, ...] = (),
    layer: str | None = None,
    related: list[ProblemLocation] | None = None,
) -> Problem:
    location = (
        ProblemLocation(kind=kind, key=key, field_path=field_path, layer=layer)
        if kind is not None
        else None
    )
    return Problem(
        code=code,
        message=message,
        severity=severity,
        location=location,
        related=related or [],
    )
