"""Shared inventory schema constants for the collector and NetBox sync.

Keep inventory_fields.py and inventory_index.py in sync when adding or
renaming inventory fields.
"""

from __future__ import annotations

MANAGED_TAG_NAME = "GitOps"
MANAGED_TAG_SLUG = "gitops"
MANAGED_TAG_COLOR = "607d8b"
DEFAULT_COLOR = "9e9e9e"
DEFAULT_STATUS = "active"

SECTIONS = (
    "sites",
    "manufacturers",
    "platforms",
    "device_roles",
    "owner_groups",
    "owners",
    "contact_roles",
    "contacts",
    "device_types",
    "module_bay_types",
    "module_types",
    "racks",
    "vlans",
    "prefixes",
    "devices",
    "ip_addresses",
    "cables",
)

# Keys that only configure the collector or sync behaviour and are not NetBox
# objects. They are silently ignored by the inventory loader.
AUXILIARY_KEYS = {"settings", "unifi_models", "unifi_vlan_names"}
LAYER_ORDER = {"generated": 0, "manual": 1}
