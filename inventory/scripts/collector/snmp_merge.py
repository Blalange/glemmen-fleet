"""Merge SNMP observations into canonical generated device entries."""

from __future__ import annotations

from collections import Counter
from typing import Any

from inventory_util import InventoryError

from .identity import DeviceResolver, MappingError
from .log import get_logger
from .sources.snmp_oids import interface_candidate_priority, interface_candidates

log = get_logger(__name__)


def _ensure_vlan(
    vlans: list[dict[str, Any]],
    by_vid: dict[int, dict[str, Any]],
    vid: int,
    name: str,
    site: str,
) -> None:
    if vid in by_vid:
        return
    entry = {
        "vid": vid,
        "name": name or f"VLAN {vid}",
        "site": site,
        "status": "active",
        "description": "SNMP-discovered VLAN",
    }
    vlans.append(entry)
    by_vid[vid] = entry


def merge_snmp_observations(
    devices: list[dict[str, Any]],
    vlans: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    resolver: DeviceResolver,
    manual: dict[str, Any],
) -> dict[str, str]:
    """Apply stable SNMP data to generated device/interface entries.

    Manual values always win. If a manual-only device has no missing fields,
    no generated entry is created; SNMP data remains available in the report.
    """
    site = str(manual.get("settings", {}).get("site") or "glemmen")
    devices_by_name = {str(device.get("name")): device for device in devices}
    vlan_by_vid = {int(vlan["vid"]): vlan for vlan in vlans if vlan.get("vid") is not None}
    matches: dict[str, str] = {}

    for observation in observations:
        address = str(observation.get("ip") or "")
        system = observation.get("sys") or {}
        refs = {
            "snmp_ip": {address},
            "snmp_name": {system.get("name")},
            "snmp_mac": {observation.get("base_mac")},
            "snmp_serial": {observation.get("serial")},
        }
        try:
            resolved = resolver.resolve(refs, source="snmp", label=address)
        except MappingError as exc:
            raise InventoryError(str(exc)) from exc
        if resolved is None:
            log.warning("SNMP %s is not mapped to a device", address)
            continue
        name = str(resolved.get("name") or "")
        if not name:
            continue
        matches[address] = name
        log.debug("SNMP %s -> device %s", address, name)

        device = devices_by_name.get(name)
        manual_device = resolver.manual_by_name.get(name) or {}
        known_names = {
            str(interface.get("name"))
            for interface in (device or {}).get("interfaces", []) or []
            if interface.get("name")
        }
        known_names.update(
            str(interface.get("name"))
            for interface in manual_device.get("interfaces", []) or []
            if interface.get("name")
        )
        existing = {
            str(interface.get("name")): interface
            for interface in (device or {}).get("interfaces", []) or []
            if interface.get("name")
        }
        manual_interfaces = {
            str(interface.get("name")): interface
            for interface in manual_device.get("interfaces", []) or []
            if interface.get("name")
        }

        mac_counts = Counter(
            str(interface.get("mac"))
            for interface in observation.get("interfaces", [])
            if interface.get("mac")
        )
        base_mac = str(observation.get("base_mac") or "")

        # Build candidate -> observation mappings. Explicit aliases win over
        # vendor names, and internal/aggregate interfaces must not overwrite a
        # real physical port.
        by_candidate: dict[str, list[tuple[int, int, int, dict[str, Any]]]] = {}
        for snmp_interface in observation.get("interfaces", []):
            candidates = snmp_interface.get("candidates") or interface_candidates(
                str(snmp_interface.get("name") or ""),
                str(snmp_interface.get("alias") or ""),
                str(snmp_interface.get("descr") or ""),
            )
            for candidate in candidates:
                if candidate not in known_names:
                    continue
                priority = interface_candidate_priority(
                    str(snmp_interface.get("name") or ""),
                    str(snmp_interface.get("alias") or ""),
                    candidate,
                )
                by_candidate.setdefault(candidate, []).append(
                    (
                        priority,
                        0 if snmp_interface.get("oper_status") == 1 else 1,
                        int(snmp_interface.get("if_index") or 0),
                        snmp_interface,
                    )
                )

        interface_updates: list[tuple[str, dict[str, Any]]] = []
        for target_name, candidates in by_candidate.items():
            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            best_priority = candidates[0][0]
            best = [item for item in candidates if item[0] == best_priority]
            if len(best) > 1 and len({item[2] for item in best}) > 1:
                # Ambiguous physical-name match (for example eth0 and eth1 on
                # an AP). Prefer the operational interface only when unique.
                operational = [item for item in best if item[1] == 0]
                if len(operational) != 1:
                    continue
                snmp_interface = operational[0][3]
            else:
                snmp_interface = best[0][3]

            manual_interface = manual_interfaces.get(target_name, {})
            update: dict[str, Any] = {}
            admin_status = snmp_interface.get("admin_status")
            if admin_status in {1, 2} and "enabled" not in manual_interface:
                update["enabled"] = admin_status == 1

            mac = snmp_interface.get("mac")
            if (
                mac
                and mac != base_mac
                and mac_counts[mac] == 1
                and not manual_interface.get("mac_address")
            ):
                update["mac_address"] = mac

            vlan_info = observation.get("interface_vlans", {}).get(snmp_interface.get("if_index"))
            if (
                vlan_info
                and not manual_interface.get("untagged_vlan")
                and not manual_interface.get("tagged_vlans")
            ):
                untagged = [int(vid) for vid in vlan_info.get("untagged", []) if int(vid) > 0]
                tagged = [int(vid) for vid in vlan_info.get("tagged", []) if int(vid) > 0]
                for vid in untagged + tagged:
                    _ensure_vlan(
                        vlans,
                        vlan_by_vid,
                        vid,
                        observation.get("vlans", {}).get(vid, ""),
                        site,
                    )
                if untagged:
                    update["untagged_vlan"] = untagged[0]
                    if len(untagged) > 1:
                        tagged.extend(vid for vid in untagged[1:] if vid not in tagged)
                if tagged:
                    update["tagged_vlans"] = sorted(set(tagged))
                    update["mode"] = "tagged"
                elif untagged:
                    update["mode"] = "access"
            if update:
                interface_updates.append((target_name, update))

        serial = str(observation.get("serial") or "")
        needs_serial = bool(serial) and not manual_device.get("serial") and not (device or {}).get("serial")
        if device is None and not interface_updates and not needs_serial:
            continue
        if device is None:
            device = {"name": name, "interfaces": []}
            devices.append(device)
            devices_by_name[name] = device
            existing = {}
        if needs_serial:
            device["serial"] = serial

        for target_name, update in interface_updates:
            interface = existing.get(target_name)
            if interface is None:
                interface = {"name": target_name}
                device.setdefault("interfaces", []).append(interface)
                existing[target_name] = interface
            interface.update(update)

    log.debug(
        "SNMP merge: observations=%d, matched=%d, vlans=%d",
        len(observations),
        len(matches),
        len(vlans),
    )
    return matches
