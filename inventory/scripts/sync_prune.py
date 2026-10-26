from __future__ import annotations

from typing import Any, Callable

from inventory_util import SyncError, format_key


class PruneMixin:
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

    def prune_modules(self) -> None:
        def skip(module: Any) -> bool:
            device = getattr(module, "device", None)
            return getattr(device, "name", None) in self.planned_delete_devices

        self.prune_model(
            "module",
            self.nb.dcim.modules,
            self.desired["modules"],
            lambda module: (module.device.name, module.module_bay.name),
            skip=skip,
        )

    def prune_module_bays(self) -> None:
        def skip(bay: Any) -> bool:
            device = getattr(bay, "device", None)
            return getattr(device, "name", None) in self.planned_delete_devices

        self.prune_model(
            "module_bay",
            self.nb.dcim.module_bays,
            self.desired["module_bays"],
            lambda bay: (bay.device.name, bay.name),
            skip=skip,
        )

    def prune_ip_addresses(self) -> None:
        self.prune_model("ip_address", self.nb.ipam.ip_addresses, self.desired["ip_addresses"], lambda ip: ip.address)

    def prune_contact_assignments(self) -> None:
        self.prune_model(
            "contact_assignment",
            self.nb.tenancy.contact_assignments,
            self.desired["contact_assignments"],
            lambda assignment: (
                str(assignment.object_type),
                int(assignment.object_id),
                int(assignment.contact.id),
                int(assignment.role.id),
            ),
        )

    def prune_contacts(self) -> None:
        self.prune_model(
            "contact",
            self.nb.tenancy.contacts,
            self.desired["contacts"],
            lambda item: item.name,
        )

    def prune_contact_roles(self) -> None:
        self.prune_model(
            "contact_role",
            self.nb.tenancy.contact_roles,
            self.desired["contact_roles"],
            lambda item: item.slug,
        )

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

    def prune_module_types(self) -> None:
        self.prune_model(
            "module_type",
            self.nb.dcim.module_types,
            self.desired["module_types"],
            lambda item: (
                getattr(getattr(item, "manufacturer", None), "slug", None),
                item.model,
            ),
        )

    def prune_module_bay_types(self) -> None:
        self.prune_model(
            "module_bay_type",
            self.nb.dcim.module_bay_types,
            self.desired["module_bay_types"],
            lambda item: item.slug,
        )

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
        self.prune_modules()
        self.prune_module_bays()
        self.prune_ip_addresses()
        self.prune_contact_assignments()
        self.prune_racks()
        self.prune_prefixes()
        self.prune_vlans()
        self.prune_contact_roles()
        self.prune_contacts()
        self.prune_device_types()
        self.prune_module_types()
        self.prune_platforms()
        self.prune_device_roles()
        self.prune_module_bay_types()
        self.prune_manufacturers()
        self.prune_sites()
