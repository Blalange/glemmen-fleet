"""Validate the final merged inventory without contacting NetBox."""

from __future__ import annotations

import ipaddress
from typing import Any, Callable

from inventory_schema import SECTIONS
from inventory_util import (
    SyncError,
    as_list,
    normalize_address,
    normalize_prefix,
    require,
    slugify,
)


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
    module_bay_types = index(
        inventory["module_bay_types"],
        "module_bay_types",
        lambda item: item.get("slug") or slugify(require(item, "name", "module bay type")),
    )
    module_types = index(
        inventory["module_types"],
        "module_types",
        lambda item: item.get("slug")
        or slugify(f"{require(item, 'manufacturer', 'module type')}-{require(item, 'model', 'module type')}"),
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

    for item in inventory["module_bay_types"]:
        manufacturer = item.get("manufacturer")
        if manufacturer and manufacturer not in manufacturers:
            raise SyncError(
                f"module bay type {item.get('name')!r}: unknown manufacturer {manufacturer!r}"
            )

    for item in inventory["module_types"]:
        manufacturer = require(item, "manufacturer", "module type")
        if manufacturer not in manufacturers:
            raise SyncError(f"module type {item.get('model')!r}: unknown manufacturer {manufacturer!r}")
        require(item, "profile", "module type")
        for bay_type in as_list(item.get("bay_types")):
            if bay_type not in module_bay_types:
                raise SyncError(
                    f"module type {item.get('model')!r}: unknown module bay type {bay_type!r}"
                )

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
        device_addresses: set[str] = set()
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
                address = normalize_address(ip["address"])
                device_addresses.add(address)
                add_ip(ip, name, interface_name)
                if ip.get("primary"):
                    version = ipaddress.ip_interface(normalize_address(ip["address"])).version
                    if version in primaries:
                        raise SyncError(
                            f"device {name}: more than one primary IPv{version} address"
                        )
                    primaries[version] = normalize_address(ip["address"])

        module_bays: dict[str, dict[str, Any]] = {}
        for item in device.get("module_bays", []) or []:
            if not isinstance(item, dict):
                raise SyncError(f"device {name}: every module bay must be a mapping")
            bay_name = str(require(item, "name", f"module bay on {name}"))
            if bay_name in module_bays:
                raise SyncError(f"device {name}: duplicate module bay {bay_name!r}")
            bay_type = item.get("bay_type")
            if bay_type and bay_type not in module_bay_types:
                raise SyncError(
                    f"device {name} module bay {bay_name}: unknown module bay type {bay_type!r}"
                )
            module_bays[bay_name] = item

        modules: dict[str, dict[str, Any]] = {}
        for item in device.get("modules", []) or []:
            if not isinstance(item, dict):
                raise SyncError(f"device {name}: every module must be a mapping")
            bay_name = str(require(item, "bay", f"module on {name}"))
            if bay_name in modules:
                raise SyncError(f"device {name}: more than one module in bay {bay_name!r}")
            if bay_name not in module_bays:
                raise SyncError(f"device {name}: module references unknown bay {bay_name!r}")
            module_type = str(require(item, "module_type", f"module in {bay_name} on {name}"))
            if module_type not in module_types:
                raise SyncError(
                    f"device {name} module {bay_name}: unknown module type {module_type!r}"
                )
            modules[bay_name] = item

        if device.get("oob_ip"):
            oob = normalize_address(device["oob_ip"])
            oob_host = str(ipaddress.ip_interface(oob).ip)
            if not any(
                str(ipaddress.ip_interface(address).ip) == oob_host
                for address in device_addresses
            ):
                raise SyncError(
                    f"device {name}: oob_ip {device['oob_ip']!r} is not assigned "
                    "to an interface on the same device"
                )

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
