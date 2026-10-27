"""Validate the final merged inventory without contacting NetBox.

The sync job and the language server share the same rule engine. The sync
entrypoint prints every problem; the language server maps each problem's
semantic location to a YAML range.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from inventory_keys import natural_key
from inventory_problems import Problem, ProblemLocation, problem
from inventory_racks import rack_occupancy_problems
from inventory_schema import SECTIONS
from inventory_util import (
    SyncError,
    as_list,
    normalize_address,
    require,
)


def collect_inventory_problems(
    inventory: dict[str, list[Any]],
    *,
    report_duplicates: bool = True,
) -> list[Problem]:
    """Return every inventory problem instead of raising the first one.

    ``report_duplicates`` can be disabled when the loader already collected
    duplicate-definition problems into the same result list.
    """

    problems: list[Problem] = []
    keys_by_section: dict[str, dict[int, Any]] = {}

    def add(
        code: str,
        message: str,
        *,
        severity: str = "error",
        kind: str | None = None,
        key: Any = None,
        field_path: tuple[Any, ...] = (),
        related: list[ProblemLocation] | None = None,
    ) -> None:
        problems.append(
            problem(
                code,
                message,
                severity=severity,
                kind=kind,
                key=key,
                field_path=field_path,
                related=related,
            )
        )

    def add_unknown(
        message: str,
        *,
        kind: str,
        key: Any,
        field_path: tuple[Any, ...],
        related: list[ProblemLocation] | None = None,
    ) -> None:
        add(
            "unknown-reference",
            message,
            severity="warning",
            kind=kind,
            key=key,
            field_path=field_path,
            related=related,
        )

    def require_value(
        item: dict[str, Any],
        field: str,
        kind: str,
        *,
        key: Any = None,
        label: str | None = None,
    ) -> Any:
        value = item.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            add(
                "missing-field",
                f"{label or kind.replace('_', ' ')}: missing required field {field!r}",
                kind=kind,
                key=key,
                field_path=(field,),
            )
            return None
        return value

    def index_section(
        section: str,
        kind: str,
        required_fields: tuple[str, ...],
    ) -> dict[Any, dict[str, Any]]:
        result: dict[Any, dict[str, Any]] = {}
        keys_by_section[section] = {}
        for raw in inventory[section]:
            if not isinstance(raw, dict):
                add(
                    "invalid-object",
                    f"{kind}: every entry must be a mapping",
                    kind=kind,
                )
                continue
            missing = False
            for field in required_fields:
                if require_value(raw, field, kind) is None:
                    missing = True
            if missing:
                continue
            try:
                key = natural_key(section, raw)
            except (TypeError, ValueError, SyncError) as exc:
                add(
                    "invalid-key",
                    f"{section}: invalid key: {exc}",
                    kind=kind,
                    key=None,
                    field_path=(required_fields[-1],),
                )
                continue
            if key in result:
                if report_duplicates:
                    add(
                        "duplicate-definition",
                        f"{section}: duplicate key {key!r}",
                        kind=kind,
                        key=key,
                    )
                continue
            result[key] = raw
            keys_by_section[section][id(raw)] = key
        return result

    def key_for(section: str, item: dict[str, Any]) -> Any:
        return keys_by_section.get(section, {}).get(id(item))

    sites = index_section("sites", "site", ("name",))
    manufacturers = index_section("manufacturers", "manufacturer", ("name",))
    platforms = index_section("platforms", "platform", ("name",))
    roles = index_section("device_roles", "device_role", ("name",))
    owner_groups = index_section("owner_groups", "owner_group", ("name",))
    owners = index_section("owners", "owner", ("name",))
    contact_roles = index_section("contact_roles", "contact_role", ("name",))
    contacts = index_section("contacts", "contact", ("name",))
    module_bay_types = index_section("module_bay_types", "module_bay_type", ("name",))
    device_types = index_section("device_types", "device_type", ("manufacturer", "model"))
    module_types = index_section("module_types", "module_type", ("manufacturer", "model"))
    racks = index_section("racks", "rack", ("site", "name"))
    vlans = index_section("vlans", "vlan", ("vid",))
    index_section("prefixes", "prefix", ("prefix",))
    devices_by_name = index_section("devices", "device", ("name",))
    index_section(
        "cables",
        "cable",
        ("a_device", "a_interface", "b_device", "b_interface"),
    )

    for item in inventory["platforms"]:
        if not isinstance(item, dict):
            continue
        manufacturer = item.get("manufacturer")
        if manufacturer and manufacturer not in manufacturers:
            add_unknown(
                f"platform {item.get('name')!r}: unknown manufacturer {manufacturer!r}",
                kind="platform",
                key=key_for("platforms", item),
                field_path=("manufacturer",),
            )

    for item in inventory["owners"]:
        if not isinstance(item, dict):
            continue
        group = item.get("group")
        if group and group not in owner_groups:
            add_unknown(
                f"owner {item.get('name')!r}: unknown owner group {group!r}",
                kind="owner",
                key=key_for("owners", item),
                field_path=("group",),
            )

    for item in inventory["device_types"]:
        if not isinstance(item, dict):
            continue
        manufacturer = item.get("manufacturer")
        if manufacturer and manufacturer not in manufacturers:
            add_unknown(
                f"device type {item.get('model')!r}: unknown manufacturer {manufacturer!r}",
                kind="device_type",
                key=key_for("device_types", item),
                field_path=("manufacturer",),
            )

    for item in inventory["module_bay_types"]:
        if not isinstance(item, dict):
            continue
        manufacturer = item.get("manufacturer")
        if manufacturer and manufacturer not in manufacturers:
            add_unknown(
                f"module bay type {item.get('name')!r}: unknown manufacturer {manufacturer!r}",
                kind="module_bay_type",
                key=key_for("module_bay_types", item),
                field_path=("manufacturer",),
            )

    for item in inventory["module_types"]:
        if not isinstance(item, dict):
            continue
        key = key_for("module_types", item)
        manufacturer = item.get("manufacturer")
        if manufacturer and manufacturer not in manufacturers:
            add_unknown(
                f"module type {item.get('model')!r}: unknown manufacturer {manufacturer!r}",
                kind="module_type",
                key=key,
                field_path=("manufacturer",),
            )
        if not item.get("profile"):
            add(
                "missing-field",
                "module type: missing required field 'profile'",
                kind="module_type",
                key=key,
                field_path=("profile",),
            )
        for index, bay_type in enumerate(as_list(item.get("bay_types"))):
            if bay_type not in module_bay_types:
                add_unknown(
                    f"module type {item.get('model')!r}: unknown module bay type {bay_type!r}",
                    kind="module_type",
                    key=key,
                    field_path=("bay_types", index),
                )

    for item in inventory["racks"]:
        if not isinstance(item, dict):
            continue
        site = item.get("site")
        if site and site not in sites:
            add_unknown(
                f"rack {item.get('name')!r}: unknown site {site!r}",
                kind="rack",
                key=key_for("racks", item),
                field_path=("site",),
            )

    for item in inventory["vlans"]:
        if not isinstance(item, dict):
            continue
        key = key_for("vlans", item)
        site = item.get("site")
        if site is not None and site not in sites:
            add_unknown(
                f"VLAN {item.get('vid')}: unknown site {site!r}",
                kind="vlan",
                key=key,
                field_path=("site",),
            )
        try:
            vid = int(require(item, "vid", "VLAN"))
        except (TypeError, ValueError, SyncError):
            continue
        if not 1 <= vid <= 4094:
            add(
                "invalid-value",
                f"VLAN {item.get('vid')}: VID must be between 1 and 4094",
                kind="vlan",
                key=key,
                field_path=("vid",),
            )

    for item in inventory["prefixes"]:
        if not isinstance(item, dict):
            continue
        key = key_for("prefixes", item)
        site = item.get("site")
        if site is not None and site not in sites:
            add_unknown(
                f"prefix {item.get('prefix')!r}: unknown site {site!r}",
                kind="prefix",
                key=key,
                field_path=("site",),
            )
        vlan = item.get("vlan")
        if vlan is not None:
            try:
                vlan_id = int(vlan)
            except (TypeError, ValueError):
                add(
                    "invalid-value",
                    f"prefix {item.get('prefix')!r}: invalid VLAN id {vlan!r}",
                    kind="prefix",
                    key=key,
                    field_path=("vlan",),
                )
            else:
                if (site, vlan_id) not in vlans and (None, vlan_id) not in vlans:
                    add_unknown(
                        f"prefix {item.get('prefix')!r}: unknown VLAN {vlan!r}",
                        kind="prefix",
                        key=key,
                        field_path=("vlan",),
                    )

    all_addresses: dict[str, tuple[str, ProblemLocation]] = {}

    def add_ip(
        ip: dict[str, Any],
        device_name: str,
        interface_name: str,
        *,
        kind: str,
        key: Any,
        field_path: tuple[Any, ...],
        top_level: bool = False,
    ) -> str | None:
        raw_address = ip.get("address")
        if raw_address is None:
            add(
                "missing-field",
                "IP address: missing required field 'address'",
                kind=kind,
                key=key,
                field_path=field_path,
            )
            return None
        try:
            address = normalize_address(raw_address)
        except SyncError as exc:
            add(
                "invalid-address",
                str(exc),
                kind=kind,
                key=key,
                field_path=field_path,
            )
            return None
        previous = all_addresses.get(address)
        if previous is not None:
            previous_label, previous_location = previous
            message = (
                f"duplicate IP address {address}"
                if top_level
                else f"duplicate IP address {address} on "
                f"{device_name}/{interface_name} and {previous_label}"
            )
            add(
                "duplicate-ip",
                message,
                kind=kind,
                key=key,
                field_path=field_path,
                related=[previous_location],
            )
        else:
            all_addresses[address] = (
                f"{device_name}/{interface_name}",
                ProblemLocation(kind, key, field_path),
            )
        return address

    for device in inventory["devices"]:
        if not isinstance(device, dict):
            continue
        name = device.get("name")
        if not name:
            continue
        name = str(name)
        if devices_by_name.get(name) is not device:
            continue
        key = name
        site = device.get("site")
        role = device.get("role")
        device_type = device.get("device_type")
        platform = device.get("platform")
        rack = device.get("rack")

        if not site:
            add(
                "missing-field",
                f"device {name}: missing required field 'site'",
                kind="device",
                key=key,
                field_path=("site",),
            )
        if not role:
            add(
                "missing-field",
                f"device {name}: missing required field 'role'",
                kind="device",
                key=key,
                field_path=("role",),
            )
        if not device_type:
            add(
                "missing-field",
                f"device {name}: missing required field 'device_type'",
                kind="device",
                key=key,
                field_path=("device_type",),
            )
        if site and site not in sites:
            add_unknown(
                f"device {name}: unknown site {site!r}",
                kind="device",
                key=key,
                field_path=("site",),
            )
        if role and role not in roles:
            add_unknown(
                f"device {name}: unknown device role {role!r}",
                kind="device",
                key=key,
                field_path=("role",),
            )
        if device_type and device_type not in device_types:
            add_unknown(
                f"device {name}: unknown device type {device_type!r}",
                kind="device",
                key=key,
                field_path=("device_type",),
            )
        if platform and platform not in platforms:
            add_unknown(
                f"device {name}: unknown platform {platform!r}",
                kind="device",
                key=key,
                field_path=("platform",),
            )
        owner = device.get("owner")
        if owner and owner not in owners:
            add_unknown(
                f"device {name}: unknown owner {owner!r}",
                kind="device",
                key=key,
                field_path=("owner",),
            )
        if rack and (site, rack) not in racks:
            add_unknown(
                f"device {name}: unknown rack {rack!r} in site {site!r}",
                kind="device",
                key=key,
                field_path=("rack",),
            )
        face = device.get("face")
        if face and face not in {"front", "rear"}:
            add(
                "invalid-value",
                f"device {name}: face must be 'front' or 'rear', not {face!r}",
                kind="device",
                key=key,
                field_path=("face",),
            )

        contact_assignments: dict[tuple[str, str], ProblemLocation] = {}
        for index, entry in enumerate(device.get("contacts", []) or []):
            if not isinstance(entry, dict):
                add(
                    "invalid-object",
                    f"device {name}: every contact must be a mapping",
                    kind="device",
                    key=key,
                    field_path=("contacts", index),
                )
                continue
            contact = entry.get("contact")
            contact_role = entry.get("role")
            if not contact:
                add(
                    "missing-field",
                    f"contact on {name}: missing required field 'contact'",
                    kind="device",
                    key=key,
                    field_path=("contacts", index, "contact"),
                )
            if not contact_role:
                add(
                    "missing-field",
                    f"contact on {name}: missing required field 'role'",
                    kind="device",
                    key=key,
                    field_path=("contacts", index, "role"),
                )
            if contact and contact not in contacts:
                add_unknown(
                    f"device {name}: unknown contact {contact!r}",
                    kind="device",
                    key=key,
                    field_path=("contacts", index, "contact"),
                )
            if contact_role and contact_role not in contact_roles:
                add_unknown(
                    f"device {name}: unknown contact role {contact_role!r}",
                    kind="device",
                    key=key,
                    field_path=("contacts", index, "role"),
                )
            if contact and contact_role:
                assignment = (str(contact), str(contact_role))
                if assignment in contact_assignments:
                    add(
                        "duplicate-contact",
                        f"device {name}: duplicate contact {contact!r} "
                        f"with role {contact_role!r}",
                        kind="device",
                        key=key,
                        field_path=("contacts", index),
                        related=[contact_assignments[assignment]],
                    )
                else:
                    contact_assignments[assignment] = ProblemLocation(
                        "device", key, ("contacts", index)
                    )

        interfaces: dict[str, dict[str, Any]] = {}
        primaries: dict[int, tuple[str, ProblemLocation]] = {}
        device_addresses: set[str] = set()
        for index, interface in enumerate(device.get("interfaces", []) or []):
            if not isinstance(interface, dict):
                add(
                    "invalid-object",
                    f"device {name}: every interface must be a mapping",
                    kind="device",
                    key=key,
                    field_path=("interfaces", index),
                )
                continue
            interface_name = interface.get("name")
            if not interface_name:
                add(
                    "missing-field",
                    f"interface on {name}: missing required field 'name'",
                    kind="device",
                    key=key,
                    field_path=("interfaces", index, "name"),
                )
                continue
            interface_name = str(interface_name)
            if interface_name in interfaces:
                add(
                    "duplicate-interface",
                    f"device {name}: duplicate interface {interface_name!r}",
                    kind="device",
                    key=key,
                    field_path=("interfaces", index, "name"),
                )
                continue
            interfaces[interface_name] = interface

            vlan_fields: list[tuple[tuple[Any, ...], Any]] = []
            if interface.get("untagged_vlan") is not None:
                vlan_fields.append(
                    (("untagged_vlan",), interface.get("untagged_vlan"))
                )
            for vid_index, vid in enumerate(as_list(interface.get("tagged_vlans"))):
                vlan_fields.append((("tagged_vlans", vid_index), vid))
            for field_suffix, vid in vlan_fields:
                if vid is None:
                    continue
                try:
                    vid_int = int(vid)
                except (TypeError, ValueError):
                    add(
                        "invalid-value",
                        f"device {name} interface {interface_name}: invalid VLAN {vid!r}",
                        kind="device",
                        key=key,
                        field_path=("interfaces", index, *field_suffix),
                    )
                    continue
                if (site, vid_int) not in vlans and (None, vid_int) not in vlans:
                    add_unknown(
                        f"device {name} interface {interface_name}: unknown VLAN "
                        f"{vid!r} in site {site!r}",
                        kind="device",
                        key=key,
                        field_path=("interfaces", index, *field_suffix),
                    )

            for ip_index, ip in enumerate(interface.get("ip_addresses", []) or []):
                if not isinstance(ip, dict):
                    add(
                        "invalid-object",
                        f"device {name}: every IP address must be a mapping",
                        kind="device",
                        key=key,
                        field_path=("interfaces", index, "ip_addresses", ip_index),
                    )
                    continue
                address = add_ip(
                    ip,
                    name,
                    interface_name,
                    kind="device",
                    key=key,
                    field_path=("interfaces", index, "ip_addresses", ip_index, "address"),
                )
                if address is None:
                    continue
                device_addresses.add(address)
                if ip.get("primary"):
                    version = ipaddress.ip_interface(address).version
                    if version in primaries:
                        _previous_address, previous_location = primaries[version]
                        add(
                            "primary-ip",
                            f"device {name}: more than one primary IPv{version} address",
                            kind="device",
                            key=key,
                            field_path=(
                                "interfaces",
                                index,
                                "ip_addresses",
                                ip_index,
                                "address",
                            ),
                            related=[previous_location],
                        )
                    else:
                        primaries[version] = (
                            address,
                            ProblemLocation(
                                "device",
                                key,
                                ("interfaces", index, "ip_addresses", ip_index, "address"),
                            ),
                        )

        module_bays: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(device.get("module_bays", []) or []):
            if not isinstance(item, dict):
                add(
                    "invalid-object",
                    f"device {name}: every module bay must be a mapping",
                    kind="device",
                    key=key,
                    field_path=("module_bays", index),
                )
                continue
            bay_name = item.get("name")
            if not bay_name:
                add(
                    "missing-field",
                    f"module bay on {name}: missing required field 'name'",
                    kind="device",
                    key=key,
                    field_path=("module_bays", index, "name"),
                )
                continue
            bay_name = str(bay_name)
            if bay_name in module_bays:
                add(
                    "duplicate-module-bay",
                    f"device {name}: duplicate module bay {bay_name!r}",
                    kind="device",
                    key=key,
                    field_path=("module_bays", index, "name"),
                )
                continue
            module_bays[bay_name] = item
            single_bay_type = item.get("bay_type")
            if single_bay_type and single_bay_type not in module_bay_types:
                add_unknown(
                    f"device {name} module bay {bay_name}: unknown module bay type "
                    f"{single_bay_type!r}",
                    kind="device",
                    key=key,
                    field_path=("module_bays", index, "bay_type"),
                )
            for bay_index, bay_type in enumerate(as_list(item.get("bay_types"))):
                if bay_type not in module_bay_types:
                    add_unknown(
                        f"device {name} module bay {bay_name}: unknown module bay type "
                        f"{bay_type!r}",
                        kind="device",
                        key=key,
                        field_path=("module_bays", index, "bay_types", bay_index),
                    )

        modules: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(device.get("modules", []) or []):
            if not isinstance(item, dict):
                add(
                    "invalid-object",
                    f"device {name}: every module must be a mapping",
                    kind="device",
                    key=key,
                    field_path=("modules", index),
                )
                continue
            bay_name = item.get("bay")
            if not bay_name:
                add(
                    "missing-field",
                    f"module on {name}: missing required field 'bay'",
                    kind="device",
                    key=key,
                    field_path=("modules", index, "bay"),
                )
                continue
            bay_name = str(bay_name)
            if bay_name in modules:
                add(
                    "duplicate-module",
                    f"device {name}: more than one module in bay {bay_name!r}",
                    kind="device",
                    key=key,
                    field_path=("modules", index, "bay"),
                )
                continue
            if bay_name not in module_bays:
                add(
                    "unknown-module-bay",
                    f"device {name}: module references unknown bay {bay_name!r}",
                    kind="device",
                    key=key,
                    field_path=("modules", index, "bay"),
                )
            module_type = item.get("module_type")
            if not module_type:
                add(
                    "missing-field",
                    f"module in {bay_name} on {name}: missing required field 'module_type'",
                    kind="device",
                    key=key,
                    field_path=("modules", index, "module_type"),
                )
            elif module_type not in module_types:
                add_unknown(
                    f"device {name} module {bay_name}: unknown module type {module_type!r}",
                    kind="device",
                    key=key,
                    field_path=("modules", index, "module_type"),
                )
            modules[bay_name] = item

        oob_ip = device.get("oob_ip")
        if oob_ip:
            try:
                oob = normalize_address(oob_ip)
                oob_host = str(ipaddress.ip_interface(oob).ip)
            except SyncError as exc:
                add(
                    "invalid-address",
                    str(exc),
                    kind="device",
                    key=key,
                    field_path=("oob_ip",),
                )
            else:
                if not any(
                    str(ipaddress.ip_interface(address).ip) == oob_host
                    for address in device_addresses
                ):
                    add(
                        "oob-ip",
                        f"device {name}: oob_ip {oob_ip!r} is not assigned "
                        "to an interface on the same device",
                        kind="device",
                        key=key,
                        field_path=("oob_ip",),
                    )

    problems.extend(
        rack_occupancy_problems(inventory["devices"], device_types, racks)
    )

    for index, ip in enumerate(inventory["ip_addresses"]):
        if not isinstance(ip, dict):
            add(
                "invalid-object",
                "ip_addresses: every entry must be a mapping",
                kind="ip_address",
            )
            continue
        raw_address = ip.get("address")
        try:
            address = normalize_address(raw_address)
        except (SyncError, TypeError):
            add(
                "invalid-address",
                f"ip_addresses: invalid address {raw_address!r}",
                kind="ip_address",
                key=None,
                field_path=("address",),
            )
            continue
        device_name = ip.get("device")
        interface_name = ip.get("interface")
        if not device_name:
            if interface_name:
                add(
                    "missing-field",
                    "ip_addresses: interface requires device",
                    kind="ip_address",
                    key=address,
                    field_path=("interface",),
                )
            add_ip(
                ip,
                "",
                "unassigned",
                kind="ip_address",
                key=address,
                field_path=("address",),
                top_level=True,
            )
            continue
        if device_name not in devices_by_name:
            add_unknown(
                f"ip_addresses: unknown device {device_name!r}",
                kind="ip_address",
                key=address,
                field_path=("device",),
            )
            continue
        if interface_name:
            device_interfaces = {
                str(interface.get("name"))
                for interface in devices_by_name[device_name].get("interfaces", []) or []
                if isinstance(interface, dict) and interface.get("name")
            }
            if interface_name not in device_interfaces:
                add_unknown(
                    f"ip_addresses: unknown interface {device_name}/{interface_name}",
                    kind="ip_address",
                    key=address,
                    field_path=("interface",),
                    related=[ProblemLocation("device", device_name)],
                )
        add_ip(
            ip,
            str(device_name),
            str(interface_name or "unassigned"),
            kind="ip_address",
            key=address,
            field_path=("address",),
        )

    for item in inventory["cables"]:
        if not isinstance(item, dict):
            continue
        key = key_for("cables", item)
        for side in ("a", "b"):
            device_name = item.get(f"{side}_device")
            interface_name = item.get(f"{side}_interface")
            if not device_name or not interface_name:
                continue
            if device_name not in devices_by_name:
                add_unknown(
                    f"cable {side}: unknown device {device_name!r}",
                    kind="cable",
                    key=key,
                    field_path=(f"{side}_device",),
                )
                continue
            device_interfaces = {
                str(interface.get("name"))
                for interface in devices_by_name[device_name].get("interfaces", []) or []
                if isinstance(interface, dict) and interface.get("name")
            }
            if interface_name not in device_interfaces:
                add_unknown(
                    f"cable {side}: unknown interface {device_name}/{interface_name}",
                    kind="cable",
                    key=key,
                    field_path=(f"{side}_interface",),
                    related=[ProblemLocation("device", device_name)],
                )

    return problems


def inventory_summary(inventory: dict[str, list[Any]]) -> str:
    counts = ", ".join(
        f"{section}={len(inventory[section])}" for section in SECTIONS if inventory[section]
    )
    return f"Inventory OK: {counts or 'empty'}"


def validate_inventory(inventory: dict[str, list[Any]]) -> None:
    """Validate references and uniqueness, raising the first problem.

    The raise-first wrapper keeps the existing programmatic API and tests. The
    ``sync.py --check`` path calls :func:`collect_inventory_problems` directly
    so it can print every problem.
    """

    problems = collect_inventory_problems(inventory)
    if problems:
        raise SyncError(problems[0].message)
    print(inventory_summary(inventory))
