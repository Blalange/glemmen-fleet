"""SNMP OIDs and value parsers used by the network inventory source."""

from __future__ import annotations

import re
from typing import Any

SYSTEM_OIDS = {
    "descr": "1.3.6.1.2.1.1.1.0",
    "object_id": "1.3.6.1.2.1.1.2.0",
    "uptime": "1.3.6.1.2.1.1.3.0",
    "contact": "1.3.6.1.2.1.1.4.0",
    "name": "1.3.6.1.2.1.1.5.0",
    "location": "1.3.6.1.2.1.1.6.0",
}

IF_OIDS = {
    "index": "1.3.6.1.2.1.2.2.1.1",
    "descr": "1.3.6.1.2.1.2.2.1.2",
    "type": "1.3.6.1.2.1.2.2.1.3",
    "mtu": "1.3.6.1.2.1.2.2.1.4",
    "phys_address": "1.3.6.1.2.1.2.2.1.6",
    "admin_status": "1.3.6.1.2.1.2.2.1.7",
    "oper_status": "1.3.6.1.2.1.2.2.1.8",
    "name": "1.3.6.1.2.1.31.1.1.1.1",
    "high_speed": "1.3.6.1.2.1.31.1.1.1.15",
    "alias": "1.3.6.1.2.1.31.1.1.1.18",
}

BRIDGE_OIDS = {
    "base_address": "1.3.6.1.2.1.17.1.1.0",
    "port_ifindex": "1.3.6.1.2.1.17.1.4.1.2",
    "fdb_address": "1.3.6.1.2.1.17.4.3.1.1",
    "fdb_port": "1.3.6.1.2.1.17.4.3.1.2",
    "fdb_status": "1.3.6.1.2.1.17.4.3.1.3",
    "vlan_name": "1.3.6.1.2.1.17.7.1.4.3.1.1",
    "vlan_egress": "1.3.6.1.2.1.17.7.1.4.3.1.2",
    "vlan_untagged": "1.3.6.1.2.1.17.7.1.4.3.1.4",
    "pvid": "1.3.6.1.2.1.17.7.1.4.5.1.1",
}

ENTITY_OIDS = {
    "descr": "1.3.6.1.2.1.47.1.1.1.1.2",
    "serial": "1.3.6.1.2.1.47.1.1.1.1.11",
    "model": "1.3.6.1.2.1.47.1.1.1.1.13",
}

LLDP_OIDS = {
    "loc_port_id": "1.0.8802.1.1.2.1.3.7.1.3",
    "loc_port_desc": "1.0.8802.1.1.2.1.3.7.1.4",
    "rem_chassis_id": "1.0.8802.1.1.2.1.4.1.1.5",
    "rem_port_id": "1.0.8802.1.1.2.1.4.1.1.7",
    "rem_port_desc": "1.0.8802.1.1.2.1.4.1.1.8",
    "rem_sys_name": "1.0.8802.1.1.2.1.4.1.1.9",
}


def oid_suffix(oid: str) -> list[int]:
    return [int(part) for part in str(oid).strip(".").split(".") if part.isdigit()]


def value_text(value: Any) -> str:
    try:
        return str(value.prettyPrint()).strip().strip('"')
    except Exception:  # noqa: BLE001 - vendor MIBs can expose odd types
        return str(value).strip()


def value_int(value: Any, default: int = 0) -> int:
    try:
        return int(value_text(value))
    except (TypeError, ValueError):
        return default


def value_octets(value: Any) -> bytes:
    try:
        return bytes(value.asOctets())
    except Exception:  # noqa: BLE001
        return b""


def value_mac(value: Any) -> str | None:
    data = value_octets(value)
    if len(data) == 6:
        return ":".join(f"{byte:02x}" for byte in data)
    text = value_text(value).lower()
    if re.fullmatch(r"0x[0-9a-f]{12}", text):
        return ":".join(text[index : index + 2] for index in range(2, 14, 2))
    if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", text):
        return text
    return None


def parse_bitmap(value: Any) -> set[int]:
    """Parse an SNMP bridge-port bitmap. Port numbers are 1-based."""
    data = value_octets(value)
    ports: set[int] = set()
    for byte_index, byte in enumerate(data):
        for bit in range(8):
            if byte & (0x80 >> bit):
                ports.add(byte_index * 8 + bit + 1)
    return ports


def interface_candidates(if_name: str, if_alias: str, if_descr: str) -> list[str]:
    """Map common vendor interface names to the NetBox Port N convention."""
    candidates: list[str] = []
    for raw in (if_alias, if_name, if_descr):
        text = str(raw or "").strip()
        if not text:
            continue
        values: list[str] = []
        port_match = re.fullmatch(r"Port\s+(\d+)", text, re.IGNORECASE)
        if port_match:
            values.append(f"Port {int(port_match.group(1))}")
        subnet_match = re.fullmatch(r"0/(\d+)", text)
        if subnet_match:
            values.append(f"Port {int(subnet_match.group(1))}")
        eth_match = re.fullmatch(r"eth(\d+)", text, re.IGNORECASE)
        if eth_match:
            number = int(eth_match.group(1))
            values.append(f"Port {number if number > 0 else 1}")
        swp_match = re.fullmatch(r"swp(\d+)", text, re.IGNORECASE)
        if swp_match:
            values.append(f"Port {int(swp_match.group(1)) + 1}")
        for candidate in values:
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def interface_candidate_priority(if_name: str, if_alias: str, candidate: str) -> int:
    """Return 0 for explicit alias matches, 1 for vendor names, 2 otherwise."""
    alias = str(if_alias or "").strip()
    if re.fullmatch(r"Port\s+\d+", alias, re.IGNORECASE) and alias.lower() == candidate.lower():
        return 0
    name = str(if_name or "").strip()
    if re.fullmatch(r"(?:0/\d+|eth\d+|swp\d+)", name, re.IGNORECASE):
        return 1
    return 2
