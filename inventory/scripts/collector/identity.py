"""Device identity resolution across manual, Talos, UniFi and Redfish sources."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Any

from inventory_util import InventoryError, normalize_mac


class MappingError(InventoryError):
    """A source observation maps to more than one canonical device."""


def normalize_ref(kind: str, value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if kind == "mac":
        return normalize_mac(text)
    if kind in {"unifi_ip", "idrac_ip"}:
        try:
            if "/" in text:
                return str(ipaddress.ip_interface(text).ip)
            return str(ipaddress.ip_address(text))
        except ValueError:
            return text.lower()
    if kind == "system_uuid":
        return text.lower()
    if kind in {"service_tag", "serial", "snmp_serial"}:
        return text.upper()
    if kind == "snmp_name":
        return re.sub(r"[^a-z0-9]", "", text.lower())
    return text


class DeviceResolver:
    """Map source identifiers to canonical manual devices.

    Manual devices are the identity registry. Source observations may provide
    hardware identifiers (service tag, system UUID, MAC) or locators (IP,
    Talos node). Hardware identifiers are preferred, and conflicting claims are
    errors rather than silently assigning data to the wrong device.
    """

    _FIELDS = {
        "talos_node": "talos_node",
        "unifi_ip": "unifi_ip",
        "idrac_ip": "idrac_ip",
        "service_tag": "service_tag",
        "system_uuid": "system_uuid",
        "unifi_mac": "unifi_mac",
        "unifi_serial": "unifi_serial",
        "snmp_ip": "snmp_ip",
        "snmp_mac": "snmp_mac",
        "snmp_serial": "snmp_serial",
        "snmp_name": "snmp_name",
    }

    def __init__(self, manual: dict[str, Any]) -> None:
        self.manual_by_name: dict[str, dict[str, Any]] = {}
        self.generated_by_name: dict[str, dict[str, Any]] = {}
        self.claims: dict[tuple[str, str], str] = {}
        for device in manual["devices"]:
            name = str(device.get("name") or "")
            if not name:
                continue
            self.manual_by_name[name] = device
            self.add_claim("name", name, name)
            for field, kind in self._FIELDS.items():
                value = device.get(field)
                if value:
                    self.add_claim(kind, value, name)

    def add_claim(self, kind: str, value: Any, device_name: str) -> None:
        normalized = normalize_ref(kind, value)
        if not normalized:
            return
        key = (kind, normalized)
        existing = self.claims.get(key)
        if existing and existing != device_name:
            raise MappingError(
                f"{kind} {value!r} is claimed by both {existing!r} and {device_name!r}"
            )
        self.claims[key] = device_name

    def register_generated_device(
        self, name: str, refs: dict[str, Iterable[Any]]
    ) -> None:
        """Register a canonical device that exists only in a source snapshot."""
        if not name:
            return
        self.generated_by_name.setdefault(name, {"name": name, "source_only": True})
        self.add_claim("name", name, name)
        self.add_claim("snmp_name", name, name)
        for kind, values in refs.items():
            for value in values:
                if value:
                    self.add_claim(kind, value, name)

    def add_observation_claims(
        self, device: dict[str, Any], refs: dict[str, Iterable[Any]]
    ) -> None:
        name = str(device.get("name") or "")
        if not name:
            return
        for kind, values in refs.items():
            if kind not in {"service_tag", "system_uuid", "mac", "serial"}:
                continue
            for value in values:
                if value:
                    self.add_claim(kind, value, name)

    def resolve(
        self,
        refs: dict[str, Iterable[Any]],
        *,
        source: str,
        label: str,
    ) -> dict[str, Any] | None:
        candidates: set[str] = set()
        details: list[str] = []
        for kind, values in refs.items():
            for value in values:
                normalized = normalize_ref(kind, value)
                if not normalized:
                    continue
                name = self.claims.get((kind, normalized))
                if name:
                    candidates.add(name)
                    details.append(f"{kind}={value!r}->{name}")
        if len(candidates) > 1:
            raise MappingError(
                f"{source} {label}: conflicting canonical device mappings: "
                + ", ".join(details)
            )
        if not candidates:
            return None
        name = next(iter(candidates))
        return self.manual_by_name.get(name) or self.generated_by_name.get(name)
