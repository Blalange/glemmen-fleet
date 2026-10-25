from __future__ import annotations

from typing import Any

from inventory_schema import DEFAULT_STATUS
from inventory_util import SyncError, normalize_color, normalize_prefix, require


class NetworkMixin:
    def sync_vlans(self) -> None:
        for item in self.inventory["vlans"]:
            vid = int(require(item, "vid", "VLAN"))
            name = require(item, "name", "VLAN")
            site_slug = item.get("site")
            data: dict[str, Any] = {
                "vid": vid,
                "name": name,
                "status": item.get("status", DEFAULT_STATUS),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
            }
            if site_slug:
                if site_slug not in self.sites:
                    raise SyncError(f"VLAN {vid}: unknown site {site_slug!r}")
                data["site"] = self.sites[site_slug].id
            label = f"vlan/{site_slug or 'global'}/{vid}"
            if site_slug:
                obj = self.ensure(
                    "vlan",
                    self.nb.ipam.vlans,
                    {"vid": vid, "site_id": data["site"]},
                    data,
                    tags=item.get("tags"),
                    label=label,
                )
            else:
                obj = self.reconcile(
                    "vlan",
                    self.nb.ipam.vlans,
                    self.find_global_vlan(vid),
                    data,
                    tags=item.get("tags"),
                    label=label,
                )
            self.vlans[(site_slug, vid)] = obj
            self.desired["vlans"].add((site_slug, vid))

    def sync_prefixes(self) -> None:
        for item in self.inventory["prefixes"]:
            prefix = normalize_prefix(require(item, "prefix", "prefix"))
            data: dict[str, Any] = {
                "prefix": prefix,
                "status": item.get("status", DEFAULT_STATUS),
                "description": item.get("description", ""),
                "comments": item.get("comments", ""),
                "is_pool": bool(item.get("is_pool", False)),
            }
            site_slug = item.get("site")
            # NetBox 4.x scopes a prefix through scope_type/scope_id rather
            # than a plain `site` field.
            if site_slug:
                if site_slug not in self.sites:
                    raise SyncError(f"prefix {prefix}: unknown site {site_slug!r}")
                data["scope_type"] = "dcim.site"
                data["scope_id"] = self.sites[site_slug].id
            else:
                data["scope_type"] = None
                data["scope_id"] = None
            vlan_vid = item.get("vlan")
            if vlan_vid is not None:
                vlan = self.vlans.get((site_slug, int(vlan_vid))) or self.vlans.get((None, int(vlan_vid)))
                if vlan is None:
                    raise SyncError(f"prefix {prefix}: unknown VLAN {vlan_vid!r}")
                data["vlan"] = vlan.id
            self.ensure(
                "prefix",
                self.nb.ipam.prefixes,
                {"prefix": prefix},
                data,
                tags=item.get("tags"),
                label=f"prefix/{prefix}",
            )
            self.desired["prefixes"].add(prefix)

    def termination_key(self, terminations: Any) -> tuple[tuple[str, int], ...]:
        keys: list[tuple[str, int]] = []
        for term in terminations or []:
            if isinstance(term, dict):
                object_type = term.get("object_type")
                object_id = term.get("object_id")
            else:
                object_type = getattr(term, "object_type", None)
                object_id = getattr(term, "object_id", None)
            if object_id is None:
                continue
            keys.append((str(object_type), int(object_id)))
        return tuple(sorted(keys))

    def cable_key(self, cable: Any) -> tuple[Any, Any]:
        sides = [
            self.termination_key(getattr(cable, "a_terminations", [])),
            self.termination_key(getattr(cable, "b_terminations", [])),
        ]
        return tuple(sorted(sides))

    def sync_cables(self) -> None:
        resolved: list[tuple[dict[str, Any], tuple[Any, Any], Any, Any]] = []
        desired: set[Any] = set()
        for item in self.inventory["cables"]:
            a_device = require(item, "a_device", "cable")
            a_interface = require(item, "a_interface", "cable")
            b_device = require(item, "b_device", "cable")
            b_interface = require(item, "b_interface", "cable")
            a = self.interfaces.get((a_device, a_interface))
            b = self.interfaces.get((b_device, b_interface))
            if a is None:
                raise SyncError(f"cable: unknown interface {a_device}/{a_interface}")
            if b is None:
                raise SyncError(f"cable: unknown interface {b_device}/{b_interface}")
            a_terms = (("dcim.interface", int(a.id)),)
            b_terms = (("dcim.interface", int(b.id)),)
            key = tuple(sorted([a_terms, b_terms]))
            resolved.append((item, key, a, b))
            desired.add(key)

        # Set the desired set first so prune_cables can remove stale cables
        # before a new cable needs one of the same terminations.
        self.desired["cables"] = desired
        if self.prune:
            self.prune_cables()

        existing = {self.cable_key(cable): cable for cable in self.nb.dcim.cables.all()}
        for item, key, a, b in resolved:
            a_device = require(item, "a_device", "cable")
            a_interface = require(item, "a_interface", "cable")
            b_device = require(item, "b_device", "cable")
            b_interface = require(item, "b_interface", "cable")
            data: dict[str, Any] = {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": a.id}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": b.id}],
                "type": item.get("type", "cat6"),
                "status": item.get("status", "connected"),
                "label": item.get("label", ""),
                "description": item.get("description", ""),
                "color": normalize_color(item["color"]) if item.get("color") else "000000",
            }
            cable = self.reconcile(
                "cable",
                self.nb.dcim.cables,
                existing.get(key),
                data,
                tags=item.get("tags"),
                label=f"cable/{a_device}/{a_interface}<->{b_device}/{b_interface}",
                compare_fields=("type", "status", "label", "description", "color", "tags"),
            )
            self.cables[key] = cable
