from __future__ import annotations

import ipaddress
from typing import Any

from inventory_schema import DEFAULT_STATUS
from inventory_util import SyncError, as_list, normalize_address, normalize_mac, require


class DevicesMixin:
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
            for module_bay in item.get("module_bays", []) or []:
                self.sync_module_bay(device, name, module_bay)
            for module in item.get("modules", []) or []:
                self.sync_module(device, name, module)
            self.sync_oob_ip(device, name, item)

    def sync_oob_ip(self, device: Any, device_name: str, item: dict[str, Any]) -> None:
        raw = item.get("oob_ip")
        if not raw:
            return
        address = normalize_address(raw)
        host = str(ipaddress.ip_interface(address).ip)
        matches = [
            ip
            for key, ip in self.ip_addresses.items()
            if str(ipaddress.ip_interface(key).ip) == host
        ]
        if len(matches) != 1:
            raise SyncError(
                f"device {device_name}: oob_ip {raw!r} must resolve to exactly one "
                f"declared interface address"
            )
        oob_ip = matches[0]
        current = getattr(device, "oob_ip", None)
        current_id = getattr(current, "id", None) if current is not None else None
        if current_id == oob_ip.id:
            return
        self.log("update", "device", f"{device_name} oob_ip", address)
        if not self.dry_run:
            try:
                device.update({"oob_ip": oob_ip.id})
            except Exception as exc:  # noqa: BLE001
                raise SyncError(f"device {device_name}: set oob_ip: {exc}") from exc

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

    def sync_module_bay(self, device: Any, device_name: str, item: dict[str, Any]) -> None:
        name = str(require(item, "name", f"module bay on {device_name}"))
        key = (device_name, name)
        if key in self.module_bays:
            raise SyncError(f"device {device_name}: duplicate module bay {name!r}")
        bay_type_ids: list[int] = []
        for bay_type in as_list(item.get("bay_types") or item.get("bay_type")):
            bay_type_obj = self.module_bay_types.get(str(bay_type))
            if bay_type_obj is None:
                raise SyncError(
                    f"device {device_name} module bay {name}: unknown module bay type {bay_type!r}"
                )
            bay_type_ids.append(bay_type_obj.id)
        data: dict[str, Any] = {
            "device": device.id,
            "name": name,
            "label": item.get("label", ""),
            "position": str(item.get("position", "")) or "",
            "enabled": bool(item.get("enabled", True)),
            "description": item.get("description", ""),
            "module_bay_types": bay_type_ids,
        }
        bay = self.ensure(
            "module_bay",
            self.nb.dcim.module_bays,
            {"device_id": device.id, "name": name},
            data,
            tags=item.get("tags"),
            label=f"module_bay/{device_name}/{name}",
        )
        self.module_bays[key] = bay
        self.desired["module_bays"].add(key)

    def sync_module(self, device: Any, device_name: str, item: dict[str, Any]) -> None:
        bay_name = str(require(item, "bay", f"module on {device_name}"))
        key = (device_name, bay_name)
        if key in self.modules:
            raise SyncError(f"device {device_name}: more than one module in bay {bay_name!r}")
        bay = self.module_bays.get(key)
        if bay is None:
            raise SyncError(f"device {device_name}: unknown module bay {bay_name!r}")
        module_type_slug = str(require(item, "module_type", f"module in {bay_name} on {device_name}"))
        module_type = self.module_types.get(module_type_slug)
        if module_type is None:
            raise SyncError(
                f"device {device_name} module {bay_name}: unknown module type {module_type_slug!r}"
            )
        data: dict[str, Any] = {
            "device": device.id,
            "module_bay": bay.id,
            "module_type": module_type.id,
            "status": item.get("status", DEFAULT_STATUS),
            "serial": item.get("serial", ""),
            "asset_tag": item.get("asset_tag"),
            "description": item.get("description", ""),
        }
        module = self.ensure(
            "module",
            self.nb.dcim.modules,
            {"device_id": device.id, "module_bay_id": bay.id},
            data,
            tags=item.get("tags"),
            label=f"module/{device_name}/{bay_name}",
        )
        self.modules[key] = module
        self.desired["modules"].add(key)

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
            # NetBox lowercases DNS names, so compare against the normalized form.
            "dns_name": str(item.get("dns_name", "")).lower(),
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
