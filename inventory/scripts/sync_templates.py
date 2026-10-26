"""Reconcile device-type and module-type component templates."""

from __future__ import annotations

from typing import Any

from sync_catalog import COMPONENT_SCHEMAS

TEMPLATE_ENDPOINTS = {
    "interfaces": ("interface_templates", "interface_template"),
    "console_ports": ("console_port_templates", "console_port_template"),
    "console_server_ports": (
        "console_server_port_templates",
        "console_server_port_template",
    ),
    "power_ports": ("power_port_templates", "power_port_template"),
    "power_outlets": ("power_outlet_templates", "power_outlet_template"),
    "rear_ports": ("rear_port_templates", "rear_port_template"),
    "front_ports": ("front_port_templates", "front_port_template"),
    "module_bays": ("module_bay_templates", "module_bay_template"),
    "device_bays": ("device_bay_templates", "device_bay_template"),
}


class TemplatesMixin:
    def sync_device_type_components(self, device_type: Any, item: dict[str, Any]) -> None:
        self._sync_components(
            owner=device_type,
            item=item,
            owner_field="device_type",
            owner_lookup="device_type_id",
            is_module_type=False,
        )

    def sync_module_type_components(self, module_type: Any, item: dict[str, Any]) -> None:
        self._sync_components(
            owner=module_type,
            item=item,
            owner_field="module_type",
            owner_lookup="module_type_id",
            is_module_type=True,
        )

    def _sync_components(
        self,
        *,
        owner: Any,
        item: dict[str, Any],
        owner_field: str,
        owner_lookup: str,
        is_module_type: bool,
    ) -> None:
        for section, (endpoint_name, kind) in TEMPLATE_ENDPOINTS.items():
            if is_module_type and section == "device_bays":
                continue
            components = item.get(section)
            if not isinstance(components, list) or not components:
                continue
            endpoint = getattr(self.nb.dcim, endpoint_name, None)
            if endpoint is None:
                continue
            allowed = COMPONENT_SCHEMAS[section][1]
            for raw in components:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                if not name:
                    continue
                data: dict[str, Any] = {
                    owner_field: owner.id,
                    "name": name,
                }
                for key in allowed:
                    if key == "name" or key not in raw:
                        continue
                    value = raw[key]
                    if value is None or value == "":
                        continue
                    data[key] = value
                if section in {"interfaces", "front_ports", "rear_ports"} and not data.get("type"):
                    continue
                self.ensure(
                    kind,
                    endpoint,
                    {owner_lookup: owner.id, "name": name},
                    data,
                    label=f"{getattr(owner, 'slug', owner.id)}/{name}",
                    manage_tags=False,
                )
