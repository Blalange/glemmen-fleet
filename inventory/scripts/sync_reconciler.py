"""Compose the resource mixins into the NetBox reconciler."""

from __future__ import annotations

from inventory_schema import MANAGED_TAG_NAME
from sync_context import SyncContext
from sync_devices import DevicesMixin
from sync_images import ImagesMixin
from sync_meta import MetaMixin
from sync_network import NetworkMixin
from sync_prune import PruneMixin
from sync_templates import TemplatesMixin


class Reconciler(
    MetaMixin,
    ImagesMixin,
    TemplatesMixin,
    NetworkMixin,
    DevicesMixin,
    PruneMixin,
    SyncContext,
):
    def run(self) -> None:
        self.ensure_tag(MANAGED_TAG_NAME)
        self.sync_sites()
        self.sync_manufacturers()
        self.sync_platforms()
        self.sync_device_roles()
        self.sync_owner_groups()
        self.sync_owners()
        self.sync_module_bay_types()
        self.sync_device_types()
        self.sync_module_types()
        self.sync_racks()
        self.sync_vlans()
        self.sync_prefixes()
        self.sync_devices()
        self.sync_standalone_ips()
        self.sync_primary_ips()
        self.sync_cables()
        self.prune_all()
        self.summary()
