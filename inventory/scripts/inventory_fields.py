"""Field catalog for the inventory YAML files.

This is intentionally a small, code-owned model rather than a JSON Schema: the
language server uses it for diagnostics, key/value completion and hover text.
Keep it in sync with the sync and collector code when fields change.
"""

from __future__ import annotations

from typing import Any

Spec = dict[str, Any]


def string(**kwargs: Any) -> Spec:
    return {"kind": "string", **kwargs}


def slug(**kwargs: Any) -> Spec:
    return {"kind": "slug", **kwargs}


def cidr(**kwargs: Any) -> Spec:
    return {"kind": "cidr", **kwargs}


def mac(**kwargs: Any) -> Spec:
    return {"kind": "mac", **kwargs}


def integer(**kwargs: Any) -> Spec:
    return {"kind": "integer", **kwargs}


def number(**kwargs: Any) -> Spec:
    return {"kind": "number", **kwargs}


def boolean(**kwargs: Any) -> Spec:
    return {"kind": "boolean", **kwargs}


def array(items: Spec, **kwargs: Any) -> Spec:
    return {"kind": "array", "items": items, **kwargs}


def obj(
    fields: dict[str, Spec],
    *,
    allow_extra: bool = False,
    variant: str | None = None,
    **kwargs: Any,
) -> Spec:
    spec: Spec = {"kind": "object", "fields": fields, "allow_extra": allow_extra, **kwargs}
    if variant:
        spec["variant"] = variant
    return spec


def mapof(values: Spec, **kwargs: Any) -> Spec:
    return {"kind": "map", "values": values, **kwargs}


def enum(values: tuple[Any, ...], **kwargs: Any) -> Spec:
    return string(enum=values, **kwargs)


TAGS = array(string())
SOURCES = array(
    enum(
        (
            "manual",
            "generated",
            "unifi",
            "talos",
            "redfish",
            "ipmi",
            "snmp",
            "dtl",
        )
    )
)

DEVICE_STATUS = (
    "offline",
    "active",
    "planned",
    "staged",
    "failed",
    "inventory",
    "decommissioning",
)
MODULE_STATUS = (
    "offline",
    "active",
    "planned",
    "staged",
    "failed",
    "decommissioning",
)
SITE_STATUS = ("planned", "staging", "active", "decommissioning", "retired")
RACK_STATUS = ("reserved", "available", "planned", "active", "deprecated")
VLAN_STATUS = ("active", "reserved", "deprecated")
PREFIX_STATUS = ("container", "active", "reserved", "deprecated")
IP_STATUS = ("active", "reserved", "deprecated", "dhcp", "slaac")
CABLE_STATUS = ("connected", "planned", "decommissioning")
CONTACT_PRIORITY = ("", "primary", "secondary", "tertiary", "inactive")
DEVICE_AIRFLOW = (
    "front-to-rear",
    "rear-to-front",
    "left-to-right",
    "right-to-left",
    "side-to-rear",
    "rear-to-side",
    "bottom-to-top",
    "top-to-bottom",
    "passive",
    "mixed",
)
WEIGHT_UNITS = ("kg", "g", "lb", "oz")
SUBDEVICE_ROLES = ("", "parent", "child")
FACES = ("front", "rear")
INTERFACE_MODES = ("", "access", "tagged", "tagged-all", "q-in-q")
RACK_WIDTHS = (10, 19, 21, 23)


def _with_common(
    fields: dict[str, Spec],
    *,
    tags: bool = True,
    sources: bool = True,
) -> dict[str, Spec]:
    result = dict(fields)
    if tags:
        result["tags"] = TAGS
    if sources:
        result["sources"] = SOURCES
    return result


# ---------------------------------------------------------------------------
# Small objects shared by several sections


SITE_FIELDS = _with_common(
    {
        "name": string(required=True),
        "slug": slug(),
        "status": enum(SITE_STATUS),
        "facility": string(),
        "description": string(),
        "physical_address": string(),
        "shipping_address": string(),
        "latitude": number(nullable=True),
        "longitude": number(nullable=True),
        "comments": string(),
    }
)

MANUFACTURER_FIELDS = _with_common(
    {
        "name": string(required=True),
        "slug": slug(),
        "description": string(),
        "comments": string(),
    }
)

PLATFORM_FIELDS = _with_common(
    {
        "name": string(required=True),
        "slug": slug(),
        "manufacturer": slug(description="Manufacturer slug."),
        "description": string(),
        "comments": string(),
    }
)

DEVICE_ROLE_FIELDS = _with_common(
    {
        "name": string(required=True),
        "slug": slug(),
        "color": string(description="Lowercase six-digit hex color."),
        "vm_role": boolean(),
        "description": string(),
        "comments": string(),
    }
)

OWNER_GROUP_FIELDS = {
    "name": string(required=True),
    "description": string(),
    "sources": SOURCES,
}

OWNER_FIELDS = {
    "name": string(required=True),
    "description": string(),
    "group": string(description="Owner group name."),
    "sources": SOURCES,
}

CONTACT_ROLE_FIELDS = _with_common(
    {
        "name": string(required=True),
        "slug": slug(),
        "description": string(),
        "comments": string(),
    }
)

CONTACT_FIELDS = _with_common(
    {
        "name": string(required=True),
        "title": string(),
        "phone": string(),
        "email": string(),
        "address": string(),
        "link": string(),
        "description": string(),
        "comments": string(),
    }
)

MODULE_BAY_TYPE_FIELDS = _with_common(
    {
        "name": string(required=True),
        "slug": slug(),
        "manufacturer": slug(description="Manufacturer slug."),
        "description": string(),
        "comments": string(),
    }
)

IMAGE_ATTACHMENT_FIELDS = {
    "name": string(required=True),
    "file": string(required=True, description="Path to the image file."),
    "description": string(),
}

IMAGES_FIELDS = {
    "front": string(description="Path to the front elevation image."),
    "rear": string(description="Path to the rear elevation image."),
    "other": array(obj(IMAGE_ATTACHMENT_FIELDS)),
}

INTERFACE_TEMPLATE_FIELDS = {
    "name": string(required=True),
    "label": string(),
    "type": string(description="NetBox interface type."),
    "channels": integer(minimum=1),
    "channel_id": integer(minimum=1),
    "enabled": boolean(),
    "mgmt_only": boolean(),
    "description": string(),
    "poe_mode": string(),
    "poe_type": string(),
    "rf_role": string(),
}

CONSOLE_PORT_TEMPLATE_FIELDS = {
    "name": string(required=True),
    "label": string(),
    "type": string(description="NetBox console port type."),
    "description": string(),
}

POWER_PORT_TEMPLATE_FIELDS = {
    "name": string(required=True),
    "label": string(),
    "type": string(description="NetBox power port or outlet type."),
    "maximum_draw": integer(minimum=0),
    "allocated_draw": integer(minimum=0),
    "description": string(),
}

FRONT_REAR_PORT_TEMPLATE_FIELDS = {
    "name": string(required=True),
    "label": string(),
    "type": string(description="NetBox pass-through port type."),
    "color": string(),
    "positions": integer(minimum=1),
    "description": string(),
}

MODULE_BAY_TEMPLATE_FIELDS = {
    "name": string(required=True),
    "label": string(),
    "position": {"kind": "any", "nullable": True},
    "enabled": boolean(),
    "description": string(),
}

DEVICE_BAY_TEMPLATE_FIELDS = {
    "name": string(required=True),
    "label": string(),
    "enabled": boolean(),
    "description": string(),
}

MODULE_TYPE_ATTRIBUTE_FIELDS = {
    "architecture": string(),
    "cores": integer(minimum=1),
    "speed": number(minimum=0),
    "size": integer(minimum=0),
    "capacity": integer(minimum=0),
    "class": enum(("DDR3", "DDR4", "DDR5")),
    "ecc": boolean(),
    "data_rate": integer(minimum=0),
    "type": enum(("HD", "SSD", "NVME")),
    "disk_type": enum(("HD", "SSD", "NVME")),
    "rpm": integer(minimum=0),
    "wattage": integer(minimum=0),
    "input_current": enum(("AC", "DC")),
    "input_voltage": number(minimum=0),
    "hot_swappable": boolean(),
    "connector_type": string(),
    "bandwidth": integer(minimum=0),
    "gpu": string(),
    "interface": string(),
    "memory": integer(minimum=0),
}

MODULE_TYPE_ATTRIBUTES = obj(
    MODULE_TYPE_ATTRIBUTE_FIELDS,
    allow_extra=True,
    description="Profile-specific NetBox module attributes.",
)


# ---------------------------------------------------------------------------
# Top-level objects


DEVICE_TYPE_FIELDS = _with_common(
    {
        "manufacturer": slug(required=True, description="Manufacturer slug."),
        "model": string(required=True),
        "slug": slug(),
        "dtl": string(description="NetBox Device Type Library definition path."),
        "u_height": integer(minimum=0),
        "is_full_depth": boolean(),
        "part_number": string(),
        "airflow": enum(DEVICE_AIRFLOW),
        "weight": number(minimum=0, nullable=True),
        "weight_unit": enum(WEIGHT_UNITS),
        "description": string(),
        "comments": string(),
        "subdevice_role": enum(SUBDEVICE_ROLES),
        "images": obj(IMAGES_FIELDS),
        "interfaces": array(obj(INTERFACE_TEMPLATE_FIELDS)),
        "console_ports": array(obj(CONSOLE_PORT_TEMPLATE_FIELDS)),
        "console_server_ports": array(obj(CONSOLE_PORT_TEMPLATE_FIELDS)),
        "power_ports": array(obj(POWER_PORT_TEMPLATE_FIELDS)),
        "power_outlets": array(obj(POWER_PORT_TEMPLATE_FIELDS)),
        "rear_ports": array(obj(FRONT_REAR_PORT_TEMPLATE_FIELDS)),
        "front_ports": array(obj(FRONT_REAR_PORT_TEMPLATE_FIELDS)),
        "module_bays": array(obj(MODULE_BAY_TEMPLATE_FIELDS)),
        "device_bays": array(obj(DEVICE_BAY_TEMPLATE_FIELDS)),
    }
)

MODULE_TYPE_FIELDS = _with_common(
    {
        "manufacturer": slug(required=True, description="Manufacturer slug."),
        "model": string(required=True),
        "slug": slug(),
        "profile": string(required=True, description="NetBox module type profile name."),
        "dtl": string(description="NetBox Device Type Library definition path."),
        "part_number": string(),
        "description": string(),
        "comments": string(),
        "weight": number(minimum=0, nullable=True),
        "weight_unit": enum(WEIGHT_UNITS),
        "attributes": MODULE_TYPE_ATTRIBUTES,
        "bay_types": array(slug(description="Module bay type slug.")),
        "interfaces": array(obj(INTERFACE_TEMPLATE_FIELDS)),
        "console_ports": array(obj(CONSOLE_PORT_TEMPLATE_FIELDS)),
        "console_server_ports": array(obj(CONSOLE_PORT_TEMPLATE_FIELDS)),
        "power_ports": array(obj(POWER_PORT_TEMPLATE_FIELDS)),
        "power_outlets": array(obj(POWER_PORT_TEMPLATE_FIELDS)),
        "rear_ports": array(obj(FRONT_REAR_PORT_TEMPLATE_FIELDS)),
        "front_ports": array(obj(FRONT_REAR_PORT_TEMPLATE_FIELDS)),
        "module_bays": array(obj(MODULE_BAY_TEMPLATE_FIELDS)),
    }
)

RACK_LAYOUT_ENTRY_FIELDS = {
    "gap": integer(minimum=1, description="Number of empty rack units."),
    "empty": integer(minimum=1, description="Alias for gap."),
    "device": string(description="Device name."),
    "face": enum(FACES),
}

RACK_FIELDS = _with_common(
    {
        "name": string(required=True),
        "site": slug(required=True, description="Site slug."),
        "status": enum(RACK_STATUS),
        "width": integer(enum=RACK_WIDTHS),
        "u_height": integer(minimum=1),
        "desc_units": boolean(),
        "facility_id": string(),
        "serial": string(),
        "asset_tag": string(nullable=True),
        "description": string(),
        "comments": string(),
        "layout": array(
            obj(RACK_LAYOUT_ENTRY_FIELDS, variant="rack_layout"),
            description="Top-to-bottom rack layout.",
        ),
    }
)

VLAN_FIELDS = _with_common(
    {
        "vid": integer(required=True, minimum=1, maximum=4094),
        "name": string(required=True),
        "site": slug(description="Site slug; omit for a global VLAN."),
        "status": enum(VLAN_STATUS),
        "description": string(),
        "comments": string(),
    }
)

PREFIX_FIELDS = _with_common(
    {
        "prefix": string(required=True, description="IPv4 or IPv6 prefix in CIDR notation."),
        "site": slug(description="Site slug."),
        "vlan": integer(description="VLAN ID."),
        "status": enum(PREFIX_STATUS),
        "description": string(),
        "comments": string(),
        "is_pool": boolean(),
    }
)

CONTACT_ASSIGNMENT_FIELDS = {
    "contact": string(required=True, description="Contact name."),
    "role": slug(required=True, description="Contact role slug."),
    "priority": enum(CONTACT_PRIORITY),
    "tags": TAGS,
    "sources": SOURCES,
}

IP_ASSIGNMENT_FIELDS = {
    "address": cidr(required=True, description="IP address in CIDR notation."),
    "status": enum(IP_STATUS),
    "dns_name": string(),
    "description": string(),
    "role": string(description="NetBox IP address role."),
    "primary": boolean(description="Use as the device primary IP address."),
    "tags": TAGS,
    "sources": SOURCES,
}

INTERFACE_FIELDS = {
    "name": string(required=True),
    "type": string(description="NetBox interface type."),
    "enabled": boolean(),
    "mgmt_only": boolean(),
    "description": string(),
    "mtu": integer(minimum=1, nullable=True),
    "mac_address": mac(nullable=True),
    "mode": enum(INTERFACE_MODES),
    "untagged_vlan": integer(minimum=1, maximum=4094, nullable=True),
    "tagged_vlans": array(integer(minimum=1, maximum=4094)),
    "ip_addresses": array(obj(IP_ASSIGNMENT_FIELDS)),
    "tags": TAGS,
    "sources": SOURCES,
}

MODULE_BAY_FIELDS = {
    "name": string(required=True),
    "position": {"kind": "any", "nullable": True},
    "bay_type": slug(description="Module bay type slug."),
    "bay_types": array(slug(description="Module bay type slug.")),
    "label": string(),
    "enabled": boolean(),
    "description": string(),
    "tags": TAGS,
    "sources": SOURCES,
}

MODULE_FIELDS = {
    "bay": string(required=True, description="Module bay name."),
    "module_type": slug(required=True, description="Module type slug."),
    "status": enum(MODULE_STATUS),
    "serial": string(),
    "asset_tag": string(nullable=True),
    "description": string(),
    "tags": TAGS,
    "sources": SOURCES,
}

UPLINK_FIELDS = {
    "interface": string(description="Device interface name."),
    "switch": string(required=True, description="Switch device name."),
    "port": integer(required=True, minimum=1, description="Switch port number."),
    "type": string(description="NetBox cable type."),
}

CABLE_FIELDS = _with_common(
    {
        "a_device": string(required=True),
        "a_interface": string(required=True),
        "b_device": string(required=True),
        "b_interface": string(required=True),
        "type": string(description="NetBox cable type."),
        "status": enum(CABLE_STATUS),
        "label": string(),
        "description": string(),
        "color": string(description="Lowercase six-digit hex color."),
    }
)

DEVICE_FIELDS = _with_common(
    {
        "name": string(required=True),
        "site": slug(description="Site slug, usually inherited from defaults."),
        "role": slug(description="Device role slug."),
        "device_type": slug(description="Device type slug."),
        "status": enum(DEVICE_STATUS),
        "platform": slug(description="Platform slug."),
        "rack": string(description="Rack name."),
        "position": integer(minimum=1, nullable=True),
        "face": enum(FACES),
        "serial": string(),
        "asset_tag": string(nullable=True),
        "owner": string(description="Owner name, usually inherited from defaults."),
        "description": string(),
        "comments": string(),
        "contacts": array(obj(CONTACT_ASSIGNMENT_FIELDS)),
        "interfaces": array(obj(INTERFACE_FIELDS)),
        "module_bays": array(obj(MODULE_BAY_FIELDS)),
        "modules": array(obj(MODULE_FIELDS)),
        "uplinks": array(obj(UPLINK_FIELDS)),
        "talos_node": string(description="Collector identity locator: Talos node name."),
        "service_tag": string(description="Collector identity: Dell service tag."),
        "system_uuid": string(description="Collector identity: hardware system UUID."),
        "unifi_ip": string(description="Collector identity locator: UniFi client IP."),
        "unifi_mac": mac(description="Collector identity: UniFi client MAC address."),
        "unifi_serial": string(description="Collector identity: UniFi serial number."),
        "redfish_ip": string(description="Collector locator: Redfish BMC address."),
        "ipmi_ip": string(description="Collector locator: IPMI BMC address."),
        "snmp_ip": string(description="Collector locator: SNMP management address."),
        "snmp_interfaces": boolean(description="Let SNMP own the physical interface list."),
        "snmp_cables": boolean(description="Let SNMP generate cables from LLDP/FDB data."),
        "oob_ip": string(description="Out-of-band management address."),
    }
)

DEVICE_DEFAULT_FIELDS = {
    "site": slug(description="Default site slug."),
    "role": slug(description="Default device role slug."),
    "device_type": slug(description="Default device type slug."),
    "status": enum(DEVICE_STATUS),
    "platform": slug(description="Default platform slug."),
    "rack": string(description="Default rack name."),
    "face": enum(FACES),
    "serial": string(),
    "asset_tag": string(nullable=True),
    "owner": string(description="Default owner name."),
    "description": string(),
    "comments": string(),
    "tags": TAGS,
    "contacts": array(obj(CONTACT_ASSIGNMENT_FIELDS)),
    "talos_node": string(),
    "service_tag": string(),
    "system_uuid": string(),
    "unifi_ip": string(),
    "unifi_mac": mac(),
    "unifi_serial": string(),
    "redfish_ip": string(),
    "ipmi_ip": string(),
    "snmp_ip": string(),
    "snmp_interfaces": boolean(),
    "snmp_cables": boolean(),
    "oob_ip": string(),
}

STANDALONE_IP_FIELDS = {
    "address": cidr(required=True, description="IP address in CIDR notation."),
    "device": string(description="Device name to assign the address to."),
    "interface": string(description="Interface name on the device."),
    "status": enum(IP_STATUS),
    "dns_name": string(),
    "description": string(),
    "role": string(description="NetBox IP address role."),
    "primary": boolean(description="Use as the device primary IP address."),
    "tags": TAGS,
    "sources": SOURCES,
}

SETTINGS_FIELDS = {
    "site": string(description="Default site slug for collected devices."),
    "default_tag": string(description="Tag added to every collected device."),
    "rack": string(description="Default rack name used by the collector."),
}

TOP_LEVEL_SPEC = obj(
    {
        "layer": enum(("manual", "generated"), required=True),
        "settings": obj(SETTINGS_FIELDS),
        "unifi_models": mapof(slug(), description="UniFi model code to device-type slug."),
        "unifi_vlan_names": mapof(string(), description="UniFi VLAN ID to corrected name."),
        "defaults": obj(DEVICE_DEFAULT_FIELDS),
        "sites": array(obj(SITE_FIELDS)),
        "manufacturers": array(obj(MANUFACTURER_FIELDS)),
        "platforms": array(obj(PLATFORM_FIELDS)),
        "device_roles": array(obj(DEVICE_ROLE_FIELDS)),
        "owner_groups": array(obj(OWNER_GROUP_FIELDS)),
        "owners": array(obj(OWNER_FIELDS)),
        "contact_roles": array(obj(CONTACT_ROLE_FIELDS)),
        "contacts": array(obj(CONTACT_FIELDS)),
        "module_bay_types": array(obj(MODULE_BAY_TYPE_FIELDS)),
        "device_types": array(obj(DEVICE_TYPE_FIELDS)),
        "module_types": array(obj(MODULE_TYPE_FIELDS)),
        "racks": array(obj(RACK_FIELDS)),
        "vlans": array(obj(VLAN_FIELDS)),
        "prefixes": array(obj(PREFIX_FIELDS)),
        "devices": array(obj(DEVICE_FIELDS)),
        "ip_addresses": array(obj(STANDALONE_IP_FIELDS)),
        "cables": array(obj(CABLE_FIELDS)),
    }
)


def spec_for_path(path: tuple[Any, ...]) -> Spec | None:
    """Return the spec at a YAML path, ignoring map key names."""

    spec: Spec = TOP_LEVEL_SPEC
    for segment in path:
        if not isinstance(spec, dict):
            return None
        kind = spec.get("kind")
        if kind == "object":
            if not isinstance(segment, str):
                return None
            spec = spec["fields"].get(segment)
            if spec is None:
                return None
        elif kind == "array":
            if not isinstance(segment, int):
                return None
            spec = spec["items"]
        elif kind == "map":
            spec = spec["values"]
        else:
            return None
    return spec
