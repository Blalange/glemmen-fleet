"""Merge SNMP observations into canonical generated device entries."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from inventory_keys import cable_key
from inventory_util import InventoryError, normalize_mac

from .identity import DeviceResolver, MappingError
from .log import get_logger
from .sources.snmp_oids import interface_candidate_priority, interface_candidates

log = get_logger(__name__)

_PORT_NAME_RE = re.compile(r"^Port\s+(\d+)$", re.IGNORECASE)


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
        "sources": ["snmp"],
    }
    vlans.append(entry)
    by_vid[vid] = entry


def _port_name(interface: dict[str, Any]) -> str:
    for candidate in interface.get("candidates") or []:
        match = _PORT_NAME_RE.match(str(candidate).strip())
        if match:
            return f"Port {int(match.group(1))}"
    return ""


def _port_sort_key(name: str) -> int:
    match = re.search(r"(\d+)", name)
    return int(match.group(1)) if match else 0


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _is_virtual(interface: dict[str, Any]) -> bool:
    return str(interface.get("type") or "").lower() in {"virtual", "lag"}


def _if_vlan_info(
    observation: dict[str, Any], if_index: Any
) -> dict[str, list[int]]:
    table = observation.get("interface_vlans") or {}
    info = table.get(if_index)
    if info is None:
        info = table.get(str(if_index))
    return info or {}


def _vlan_name(observation: dict[str, Any], vid: int) -> str:
    names = observation.get("vlans") or {}
    return str(names.get(vid) or names.get(str(vid)) or "")


def _interface_type(interface: dict[str, Any]) -> str:
    text = " ".join(
        str(interface.get(key) or "") for key in ("name", "descr", "alias")
    ).lower()
    if "sfp" in text or "fiber" in text:
        return "1000base-x-sfp"
    if int(interface.get("type") or 6) != 6:
        return "other"
    speed = int(interface.get("speed") or 0)
    if speed >= 10000:
        return "10gbase-x-sfpp"
    if speed >= 5000:
        return "5gbase-t"
    if speed >= 2500:
        return "2.5gbase-t"
    if speed >= 1000:
        return "1000base-t"
    if speed >= 100:
        return "100base-tx"
    if speed >= 10:
        return "10base-t"
    return "1000base-t"


def _vlan_fields(
    observation: dict[str, Any],
    snmp_interface: dict[str, Any],
    vlans: list[dict[str, Any]],
    vlan_by_vid: dict[int, dict[str, Any]],
    site: str,
) -> dict[str, Any]:
    vlan_info = _if_vlan_info(observation, snmp_interface.get("if_index"))
    untagged = [int(vid) for vid in vlan_info.get("untagged", []) if int(vid) > 0]
    tagged = [int(vid) for vid in vlan_info.get("tagged", []) if int(vid) > 0]
    for vid in untagged + tagged:
        _ensure_vlan(
            vlans,
            vlan_by_vid,
            vid,
            _vlan_name(observation, vid),
            site,
        )
    if untagged:
        untagged_vlan: int | None = untagged[0]
        if len(untagged) > 1:
            tagged.extend(vid for vid in untagged[1:] if vid not in tagged)
    else:
        untagged_vlan = None
    if tagged:
        mode = "tagged"
    elif untagged_vlan is not None:
        mode = "access"
    else:
        mode = ""
    return {
        "mode": mode,
        "untagged_vlan": untagged_vlan,
        "tagged_vlans": sorted(set(tagged)),
    }


def _auto_interfaces(
    observation: dict[str, Any],
    vlans: list[dict[str, Any]],
    vlan_by_vid: dict[int, dict[str, Any]],
    site: str,
    base_mac: str,
    mac_counts: Counter[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for snmp_interface in observation.get("interfaces", []) or []:
        name = _port_name(snmp_interface)
        if not name:
            continue
        admin_status = snmp_interface.get("admin_status")
        entry: dict[str, Any] = {
            "name": name,
            "type": _interface_type(snmp_interface),
            "enabled": admin_status != 2 if admin_status in {1, 2} else True,
            "sources": ["snmp"],
        }
        if snmp_interface.get("mtu"):
            entry["mtu"] = int(snmp_interface["mtu"])
        alias = str(snmp_interface.get("alias") or "").strip()
        if alias:
            entry["description"] = alias
        mac = str(snmp_interface.get("mac") or "")
        if mac and mac != base_mac and mac_counts[mac] == 1:
            entry["mac_address"] = mac
        entry.update(
            _vlan_fields(observation, snmp_interface, vlans, vlan_by_vid, site)
        )
        result.append(entry)
    return result


def _merge_interface(
    device: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    interfaces = device.setdefault("interfaces", [])
    existing = {
        str(interface.get("name")): interface
        for interface in interfaces
        if interface.get("name")
    }
    target = existing.get(entry["name"])
    if target is None:
        interfaces.append(entry)
        return entry
    for key, value in entry.items():
        if key == "sources":
            target[key] = sorted({*(target.get(key) or []), *value})
        elif key in {"mode", "untagged_vlan", "tagged_vlans", "enabled", "type", "mtu"}:
            target[key] = value
        elif value not in (None, "", []):
            target[key] = value
    return target


def _combined_catalog(
    devices: list[dict[str, Any]], manual: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Combine manual and generated devices into a name -> interfaces map."""
    catalog: dict[str, dict[str, Any]] = {}
    for source in (manual.get("devices") or [], devices):
        for raw in source:
            name = str(raw.get("name") or "")
            if not name:
                continue
            entry = catalog.setdefault(name, {"name": name, "interfaces": {}})
            if raw.get("platform"):
                entry["platform"] = str(raw["platform"])
            elif raw.get("unifi_serial") or raw.get("unifi_mac"):
                entry["platform"] = "unifi"
            for interface in raw.get("interfaces") or []:
                interface_name = str(interface.get("name") or "")
                if not interface_name:
                    continue
                target = entry["interfaces"].setdefault(
                    interface_name, {"name": interface_name}
                )
                for key, value in interface.items():
                    if value not in (None, "", []):
                        target[key] = value
    return catalog


def _mac_index(
    catalog: dict[str, dict[str, Any]]
) -> dict[str, list[tuple[str, str]]]:
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for device_name, device in catalog.items():
        for interface_name, interface in device["interfaces"].items():
            raw = interface.get("mac_address")
            if not raw:
                continue
            try:
                mac = normalize_mac(raw).lower()
            except InventoryError:
                continue
            pair = (device_name, interface_name)
            if pair not in index[mac]:
                index[mac].append(pair)
    return index


def _interface_penalty(interface: dict[str, Any]) -> int:
    penalty = 0
    if _is_virtual(interface):
        penalty += 4
    name = str(interface.get("name") or "").lower()
    if name in {"lan", "bond0"}:
        penalty += 2
    return penalty


def _resolve_mac_endpoint(
    raw_mac: Any,
    catalog: dict[str, dict[str, Any]],
    mac_index: dict[str, list[tuple[str, str]]],
    *,
    exclude_device: str | None = None,
) -> tuple[str, str] | None:
    try:
        mac = normalize_mac(raw_mac).lower()
    except InventoryError:
        return None
    matches = [
        (device_name, interface_name)
        for device_name, interface_name in mac_index.get(mac, [])
        if device_name != exclude_device
        and not _is_virtual(
            catalog[device_name]["interfaces"][interface_name]
        )
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    matches.sort(
        key=lambda item: (
            _interface_penalty(catalog[item[0]]["interfaces"][item[1]]),
            item[0],
            item[1],
        )
    )
    best = _interface_penalty(catalog[matches[0][0]]["interfaces"][matches[0][1]])
    second = _interface_penalty(
        catalog[matches[1][0]]["interfaces"][matches[1][1]]
    )
    if best < second:
        return matches[0]
    return None


def _match_device_by_name(
    name: Any, catalog: dict[str, dict[str, Any]]
) -> str | None:
    target = _normalized_name(name)
    if not target:
        return None
    matches = [
        device_name
        for device_name in catalog
        if _normalized_name(device_name) == target
    ]
    return matches[0] if len(matches) == 1 else None


def _device_for_lldp(
    entry: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    mac_index: dict[str, list[tuple[str, str]]],
    switch_name: str,
) -> str | None:
    matched = _match_device_by_name(entry.get("remote_sys_name"), catalog)
    if matched and matched != switch_name:
        return matched
    try:
        mac = normalize_mac(entry.get("remote_chassis")).lower()
    except InventoryError:
        return None
    devices = {
        device_name
        for device_name, _interface_name in mac_index.get(mac, [])
        if device_name != switch_name
    }
    if len(devices) == 1:
        return next(iter(devices))
    return None


def _remote_interface_name(
    device_name: str,
    entry: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    mac_index: dict[str, list[tuple[str, str]]],
) -> str:
    device = catalog.get(device_name, {})
    interfaces = device.get("interfaces", {})
    is_unifi = str(device.get("platform") or "").lower() == "unifi"
    for raw in (entry.get("remote_port_desc"), entry.get("remote_port_id")):
        text = str(raw or "").strip()
        candidates = interface_candidates(text, "", "")
        # UniFi OS names its switch ports eth0..ethN while the controller and
        # NetBox call them Port 1..Port N+1. Try that mapping first.
        if is_unifi:
            match = re.fullmatch(r"eth(\d+)", text, re.IGNORECASE)
            if match:
                unifi_candidate = f"Port {int(match.group(1)) + 1}"
                if unifi_candidate not in candidates:
                    candidates.insert(0, unifi_candidate)
        for candidate in candidates:
            interface = interfaces.get(candidate)
            if interface is not None and not _is_virtual(interface):
                return candidate
    try:
        mac = normalize_mac(entry.get("remote_port_id")).lower()
    except InventoryError:
        mac = ""
    if mac:
        for matched_device, interface_name in mac_index.get(mac, []):
            if matched_device != device_name:
                continue
            interface = interfaces.get(interface_name)
            if interface is not None and not _is_virtual(interface):
                return interface_name
    for raw in (entry.get("remote_port_desc"), entry.get("remote_port_id")):
        text = str(raw or "").strip()
        interface = interfaces.get(text)
        if interface is not None and not _is_virtual(interface):
            return text
    return ""


def _lldp_local_port(entry: dict[str, Any]) -> str:
    raw = str(entry.get("local_port") or "").strip()
    if raw.isdigit():
        return f"Port {int(raw)}"
    candidates = interface_candidates(
        raw, "", str(entry.get("local_desc") or "")
    )
    return candidates[0] if candidates else ""


def _cable(
    switch_name: str,
    port: str,
    remote_device: str,
    remote_interface: str,
    method: str,
) -> dict[str, Any]:
    return {
        "a_device": switch_name,
        "a_interface": port,
        "b_device": remote_device,
        "b_interface": remote_interface,
        "type": "cat6",
        "status": "connected",
        "description": f"Auto-discovered via SNMP {method}",
        "sources": ["snmp"],
    }


def _auto_cables(
    observation: dict[str, Any],
    switch_name: str,
    switch_interfaces: dict[str, dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    mac_index: dict[str, list[tuple[str, str]]],
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    # LLDP is the most reliable source for a direct neighbour (for example
    # the uplink to the UDM Pro).
    for entry in observation.get("lldp", []) or []:
        port = _lldp_local_port(entry)
        if port not in switch_interfaces:
            continue
        remote_device = _device_for_lldp(entry, catalog, mac_index, switch_name)
        if not remote_device or remote_device == switch_name:
            continue
        remote_interface = _remote_interface_name(
            remote_device, entry, catalog, mac_index
        )
        if not remote_interface:
            continue
        found[port] = _cable(
            switch_name, port, remote_device, remote_interface, "LLDP"
        )

    # The forwarding table fills in access ports where LLDP is not spoken by
    # the attached device (iDRACs). A MAC that is seen on several ports or a
    # port with more than one candidate endpoint is ambiguous and is skipped.
    by_port: dict[str, dict[tuple[str, str], set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for entry in observation.get("fdb", []) or []:
        port = str(entry.get("interface") or "")
        if not port and entry.get("if_index") is not None:
            for interface in observation.get("interfaces", []) or []:
                if int(interface.get("if_index") or 0) == int(entry["if_index"]):
                    port = _port_name(interface)
                    break
        if port not in switch_interfaces:
            continue
        vlan_info = _if_vlan_info(observation, entry.get("if_index"))
        if vlan_info.get("tagged"):
            # A trunk port also learns MACs of devices behind downstream
            # switches, so FDB data cannot prove a direct connection.
            continue
        endpoint = _resolve_mac_endpoint(
            entry.get("mac"),
            catalog,
            mac_index,
            exclude_device=switch_name,
        )
        if endpoint is None:
            continue
        by_port[port][endpoint].add(str(entry.get("mac") or ""))

    endpoint_ports: dict[tuple[str, str], set[str]] = defaultdict(set)
    for port, endpoints in by_port.items():
        for endpoint in endpoints:
            endpoint_ports[endpoint].add(port)

    for port in sorted(by_port, key=_port_sort_key):
        if port in found:
            continue
        unique = [
            endpoint
            for endpoint in by_port[port]
            if len(endpoint_ports[endpoint]) == 1
        ]
        if len(unique) != 1:
            continue
        remote_device, remote_interface = unique[0]
        found[port] = _cable(
            switch_name, port, remote_device, remote_interface, "FDB"
        )

    for port, cable in found.items():
        interface = switch_interfaces.get(port)
        if interface is not None:
            interface["description"] = (
                f"{cable['b_device']} {cable['b_interface']}"
            )

    return [found[port] for port in sorted(found, key=_port_sort_key)]


def _append_cables(
    cables: list[dict[str, Any]], generated: list[dict[str, Any]]
) -> int:
    existing = {cable_key(cable): cable for cable in cables}
    added = 0
    for cable in generated:
        key = cable_key(cable)
        current = existing.get(key)
        if current is not None:
            current["sources"] = sorted(
                {*(current.get("sources") or []), *cable.get("sources", [])}
            )
            continue
        cables.append(cable)
        existing[key] = cable
        added += 1
    return added


def merge_snmp_observations(
    devices: list[dict[str, Any]],
    vlans: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    resolver: DeviceResolver,
    manual: dict[str, Any],
    *,
    cables: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Apply SNMP data to generated device/interface/cable entries.

    Manual values always win. By default SNMP only fills missing fields. A
    device can opt in to full interface generation with ``snmp_interfaces`` and
    to cable discovery from LLDP/FDB with ``snmp_cables``.
    """
    site = str(manual.get("settings", {}).get("site") or "glemmen")
    devices_by_name = {str(device.get("name")): device for device in devices}
    vlan_by_vid = {
        int(vlan["vid"]): vlan for vlan in vlans if vlan.get("vid") is not None
    }
    catalog = _combined_catalog(devices, manual)
    mac_index = _mac_index(catalog)
    matches: dict[str, str] = {}
    added_cables = 0

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
        if device is not None:
            device["sources"] = sorted(
                {*(device.get("sources") or []), "snmp"}
            )
        manual_device = resolver.manual_by_name.get(name) or {}
        mac_counts = Counter(
            str(interface.get("mac"))
            for interface in observation.get("interfaces", [])
            if interface.get("mac")
        )
        base_mac = str(observation.get("base_mac") or "")

        if manual_device.get("snmp_interfaces"):
            if device is None:
                device = {
                    "name": name,
                    "site": site,
                    "sources": ["snmp"],
                    "interfaces": [],
                }
                devices.append(device)
                devices_by_name[name] = device
            for entry in _auto_interfaces(
                observation, vlans, vlan_by_vid, site, base_mac, mac_counts
            ):
                _merge_interface(device, entry)
            if manual_device.get("snmp_cables") and cables is not None:
                switch_interfaces = {
                    str(interface.get("name")): interface
                    for interface in device.get("interfaces", []) or []
                    if interface.get("name")
                }
                generated_cables = _auto_cables(
                    observation,
                    name,
                    switch_interfaces,
                    catalog,
                    mac_index,
                )
                added_cables += _append_cables(cables, generated_cables)
            serial = str(observation.get("serial") or "")
            if (
                serial
                and not manual_device.get("serial")
                and not device.get("serial")
            ):
                device["serial"] = serial
            continue

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

            vlan_info = _if_vlan_info(observation, snmp_interface.get("if_index"))
            if (
                vlan_info
                and not manual_interface.get("untagged_vlan")
                and not manual_interface.get("tagged_vlans")
            ):
                untagged = [
                    int(vid)
                    for vid in vlan_info.get("untagged", [])
                    if int(vid) > 0
                ]
                tagged = [
                    int(vid) for vid in vlan_info.get("tagged", []) if int(vid) > 0
                ]
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
        needs_serial = (
            bool(serial)
            and not manual_device.get("serial")
            and not (device or {}).get("serial")
        )
        if device is None and not interface_updates and not needs_serial:
            continue
        if device is None:
            device = {"name": name, "sources": ["snmp"], "interfaces": []}
            devices.append(device)
            devices_by_name[name] = device
            existing = {}
        if needs_serial:
            device["serial"] = serial

        for target_name, update in interface_updates:
            interface = existing.get(target_name)
            if interface is None:
                interface = {"name": target_name, "sources": ["snmp"]}
                device.setdefault("interfaces", []).append(interface)
                existing[target_name] = interface
            else:
                inherited = interface.get("sources") or device.get("sources") or []
                interface["sources"] = sorted({*inherited, "snmp"})
            interface.update(update)

    log.debug(
        "SNMP merge: observations=%d, matched=%d, vlans=%d, cables_added=%d",
        len(observations),
        len(matches),
        len(vlans),
        added_cables,
    )
    return matches
