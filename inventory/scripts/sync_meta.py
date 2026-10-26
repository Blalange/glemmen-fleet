from __future__ import annotations

from typing import Any

from inventory_schema import DEFAULT_COLOR, DEFAULT_STATUS
from inventory_util import SyncError, as_list, normalize_color, require, slugify


class MetaMixin:
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

    def sync_owner_groups(self) -> None:
        # Owners and owner groups are administrative models in NetBox and are
        # not taggable, so they are created/updated but never pruned.
        for item in self.inventory["owner_groups"]:
            name = str(require(item, "name", "owner group"))
            data: dict[str, Any] = {
                "name": name,
                "description": item.get("description", ""),
            }
            obj = self.ensure(
                "owner_group",
                self.nb.users.owner_groups,
                {"name": name},
                data,
                label=f"owner_group/{name}",
                manage_tags=False,
            )
            self.owner_groups[name] = obj
            self.desired["owner_groups"].add(name)

    def sync_owners(self) -> None:
        for item in self.inventory["owners"]:
            name = str(require(item, "name", "owner"))
            group = None
            group_name = item.get("group")
            if group_name:
                group = self.owner_groups.get(str(group_name))
                if group is None:
                    raise SyncError(f"owner {name!r}: unknown owner group {group_name!r}")
            data: dict[str, Any] = {
                "name": name,
                "description": item.get("description", ""),
                "group": group.id if group else None,
            }
            obj = self.ensure(
                "owner",
                self.nb.users.owners,
                {"name": name},
                data,
                label=f"owner/{name}",
                manage_tags=False,
            )
            self.owners[name] = obj
            self.desired["owners"].add(name)

    def sync_module_bay_types(self) -> None:
        for item in self.inventory["module_bay_types"]:
            name = require(item, "name", "module bay type")
            slug = item.get("slug") or slugify(name)
            manufacturer = None
            manufacturer_slug = item.get("manufacturer")
            if manufacturer_slug:
                manufacturer = self.manufacturers.get(manufacturer_slug)
                if manufacturer is None:
                    raise SyncError(
                        f"module bay type {name!r}: unknown manufacturer {manufacturer_slug!r}"
                    )
            data: dict[str, Any] = {
                "name": name,
                "slug": slug,
                "manufacturer": manufacturer.id if manufacturer else None,
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            obj = self.ensure(
                "module_bay_type",
                self.nb.dcim.module_bay_types,
                {"slug": slug},
                data,
                tags=item.get("tags"),
                label=f"module_bay_type/{slug}",
            )
            self.module_bay_types[slug] = obj
            self.desired["module_bay_types"].add(slug)

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
            }
            # DTL enrichment is fetched at sync time. If the catalog is
            # temporarily unavailable, keep the last NetBox values instead of
            # clearing optional metadata just because the inventory item has no
            # local copy.
            managed_by_catalog = bool(item.get("dtl"))
            optional = {
                "part_number": "",
                "description": "",
                "comments": "",
                "airflow": "",
                "weight": None,
                "weight_unit": "",
                "subdevice_role": "",
            }
            for key, default in optional.items():
                if managed_by_catalog and key not in item:
                    continue
                value = item.get(key, default)
                data[key] = value or "" if key in {"airflow", "weight_unit"} else value
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
            self.sync_device_type_components(obj, item)

    def sync_module_types(self) -> None:
        if not self.module_profiles:
            self.module_profiles = {
                profile.name: profile for profile in self.nb.dcim.module_type_profiles.all()
            }
        for item in self.inventory["module_types"]:
            manufacturer = require(item, "manufacturer", "module type")
            model = require(item, "model", "module type")
            profile_name = str(require(item, "profile", "module type"))
            if manufacturer not in self.manufacturers:
                raise SyncError(f"module type {model!r}: unknown manufacturer {manufacturer!r}")
            profile = self.module_profiles.get(profile_name)
            if profile is None:
                raise SyncError(f"module type {model!r}: unknown module type profile {profile_name!r}")
            slug = item.get("slug") or slugify(f"{manufacturer}-{model}")
            bay_type_ids: list[int] = []
            for bay_type in as_list(item.get("bay_types")):
                bay_type_obj = self.module_bay_types.get(str(bay_type))
                if bay_type_obj is None:
                    raise SyncError(
                        f"module type {model!r}: unknown module bay type {bay_type!r}"
                    )
                bay_type_ids.append(bay_type_obj.id)
            data: dict[str, Any] = {
                "manufacturer": self.manufacturers[manufacturer].id,
                "model": model,
                "profile": profile.id,
                "attributes": item.get("attributes") or {},
                "module_bay_types": bay_type_ids,
            }
            managed_by_catalog = bool(item.get("dtl"))
            optional = {
                "part_number": "",
                "description": "",
                "comments": "",
                "weight": None,
                "weight_unit": "",
            }
            for key, default in optional.items():
                if managed_by_catalog and key not in item:
                    continue
                value = item.get(key, default)
                data[key] = value or "" if key == "weight_unit" else value
            obj = self.ensure(
                "module_type",
                self.nb.dcim.module_types,
                {"manufacturer_id": self.manufacturers[manufacturer].id, "model": model},
                data,
                tags=item.get("tags"),
                label=f"module_type/{slug}",
            )
            self.module_types[slug] = obj
            self.desired["module_types"].add((manufacturer, model))
            self.sync_module_type_components(obj, item)

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
