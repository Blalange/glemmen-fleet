"""Shared reconciliation state and NetBox object helpers."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pynetbox

from inventory_schema import (
    MANAGED_TAG_COLOR,
    MANAGED_TAG_NAME,
    MANAGED_TAG_SLUG,
    SECTIONS,
)
from inventory_util import SyncError, as_list, same_value, slugify
from sync_api import MockObject


class SyncContext:
    def __init__(
        self,
        nb: pynetbox.api,
        inventory: dict[str, list[Any]],
        dry_run: bool = False,
        prune: bool = False,
        url: str = "",
        token: str = "",
        images_dir: Path | None = None,
        force_images: bool = False,
    ) -> None:
        self.nb = nb
        self.inventory = inventory
        self.dry_run = dry_run
        self.prune = prune
        self.url = url.rstrip("/")
        self.token = token
        self.images_dir = images_dir
        self.force_images = force_images
        self.counts: Counter[str] = Counter()
        self.tags: dict[str, Any] = {}
        self.sites: dict[str, Any] = {}
        self.site_slugs_by_id: dict[int, str] = {}
        self.manufacturers: dict[str, Any] = {}
        self.platforms: dict[str, Any] = {}
        self.roles: dict[str, Any] = {}
        self.device_types: dict[str, Any] = {}
        self.module_bay_types: dict[str, Any] = {}
        self.module_profiles: dict[str, Any] = {}
        self.module_types: dict[str, Any] = {}
        self.racks: dict[tuple[str, str], Any] = {}
        self.vlans: dict[tuple[str | None, int], Any] = {}
        self.devices: dict[str, Any] = {}
        self.device_sites: dict[str, str] = {}
        self.interfaces: dict[tuple[str, str], Any] = {}
        self.module_bays: dict[tuple[str, str], Any] = {}
        self.modules: dict[tuple[str, str], Any] = {}
        self.ip_addresses: dict[str, Any] = {}
        self.cables: dict[Any, Any] = {}
        self.device_primary: dict[str, dict[int, str]] = {}
        self.planned_delete_devices: set[str] = set()
        self.desired: dict[str, set[Any]] = {section: set() for section in SECTIONS}
        # Interfaces are nested under devices rather than being a top-level
        # inventory section, but pruning still needs their desired keys.
        self.desired["interfaces"] = set()
        self.desired["module_bays"] = set()
        self.desired["modules"] = set()

    def log(self, action: str, kind: str, label: Any, detail: str = "") -> None:
        self.counts[action] += 1
        prefix = "WOULD " if self.dry_run else ""
        message = f"{prefix}{action.upper():6} {kind:12} {label}"
        if detail:
            message += f" ({detail})"
        print(message, flush=True)

    def summary(self) -> None:
        mode = "DRY RUN" if self.dry_run else "APPLIED"
        print()
        print(
            f"{mode}: {self.counts['create']} create, "
            f"{self.counts['update']} update, "
            f"{self.counts['delete']} delete, "
            f"{self.counts['unchanged']} unchanged",
            flush=True,
        )
        if self.dry_run:
            print("No changes were written to NetBox.", flush=True)

    def ensure_tag(self, name: str) -> Any:
        slug = slugify(name)
        if slug in self.tags:
            return self.tags[slug]
        tag = self.nb.extras.tags.get(slug=slug)
        if tag is None:
            tag = self.nb.extras.tags.get(name=name)
        if tag is None:
            self.log("create", "tag", slug)
            if self.dry_run:
                tag = MockObject(name=name, slug=slug, color=MANAGED_TAG_COLOR)
            else:
                tag = self.nb.extras.tags.create(name=name, slug=slug, color=MANAGED_TAG_COLOR)
        self.tags[slug] = tag
        return tag

    def tag_ids(self, extra: Any = None) -> list[int]:
        ids = [self.ensure_tag(MANAGED_TAG_NAME).id]
        for name in as_list(extra):
            ids.append(self.ensure_tag(str(name)).id)
        return list(dict.fromkeys(ids))

    def is_managed(self, obj: Any) -> bool:
        return any(
            getattr(tag, "slug", None) == MANAGED_TAG_SLUG
            for tag in (getattr(obj, "tags", None) or [])
        )

    def get_one(self, endpoint: Any, lookup: dict[str, Any], kind: str) -> Any:
        # A dry run resolves objects created earlier in the same run to
        # negative mock IDs. Those IDs cannot exist in NetBox, so do not send
        # them as filter values (the API rejects them with a 400).
        if any(isinstance(value, int) and value < 0 for value in lookup.values()):
            return None
        try:
            return endpoint.get(**lookup)
        except Exception as exc:  # noqa: BLE001 - pynetbox raises several types
            raise SyncError(f"look up {kind} {lookup}: {exc}") from exc

    def reconcile(
        self,
        kind: str,
        endpoint: Any,
        existing: Any,
        data: dict[str, Any],
        tags: Any = None,
        label: Any = None,
        compare_fields: Iterable[str] | None = None,
    ) -> Any:
        label = label if label is not None else kind
        data = dict(data)
        data["tags"] = self.tag_ids(tags)

        if existing is None:
            self.log("create", kind, label)
            if self.dry_run:
                return MockObject(**data)
            try:
                return endpoint.create(**data)
            except Exception as exc:  # noqa: BLE001
                raise SyncError(f"create {kind} {label}: {exc}") from exc

        fields = tuple(compare_fields) if compare_fields is not None else tuple(data)
        changes: dict[str, Any] = {}
        for field in fields:
            if field not in data:
                continue
            if not same_value(getattr(existing, field, None), data[field]):
                changes[field] = data[field]

        if changes:
            self.log("update", kind, label, ", ".join(sorted(changes)))
            if not self.dry_run:
                try:
                    existing.update(changes)
                except Exception as exc:  # noqa: BLE001
                    raise SyncError(f"update {kind} {label}: {exc}") from exc
        else:
            self.counts["unchanged"] += 1

        return existing

    def ensure(
        self,
        kind: str,
        endpoint: Any,
        lookup: dict[str, Any],
        data: dict[str, Any],
        tags: Any = None,
        label: Any = None,
    ) -> Any:
        existing = self.get_one(endpoint, lookup, kind)
        return self.reconcile(kind, endpoint, existing, data, tags=tags, label=label or lookup)

    def find_global_vlan(self, vid: int) -> Any:
        matches = [vlan for vlan in self.nb.ipam.vlans.filter(vid=vid) if vlan.site is None]
        if len(matches) > 1:
            raise SyncError(f"VLAN {vid}: multiple global VLANs found")
        return matches[0] if matches else None

    def resolve_vlan(self, site_slug: str, vid: int) -> Any:
        vlan = self.vlans.get((site_slug, vid)) or self.vlans.get((None, vid))
        if vlan is None:
            raise SyncError(f"VLAN {vid} is not declared for site {site_slug!r}")
        return vlan
