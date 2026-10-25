"""SNMP network-device source: system facts, interfaces, VLANs and L2 tables."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from inventory_util import InventoryError

from .snmp_oids import (
    BRIDGE_OIDS,
    ENTITY_OIDS,
    IF_OIDS,
    LLDP_OIDS,
    SYSTEM_OIDS,
    interface_candidates,
    oid_suffix,
    parse_bitmap,
    value_int,
    value_mac,
    value_text,
)

try:
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
        walk_cmd,
    )

    _PYSNMP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    _PYSNMP_AVAILABLE = False


def _require_pysnmp() -> None:
    if not _PYSNMP_AVAILABLE:
        raise InventoryError(
            "SNMP collection requires pysnmp. Run the collector with "
            "--with pysnmp==7.1.22 or pass --no-snmp."
        )


def _oid_text(value: Any) -> str:
    oid = getattr(value, "get_oid", None)
    if callable(oid):
        return str(oid())
    return str(value)


async def _get(engine: Any, auth: Any, transport: Any, context: Any, oid: str) -> Any | None:
    try:
        error, status, _index, binds = await get_cmd(
            engine, auth, transport, context, ObjectType(ObjectIdentity(oid))
        )
    except Exception:  # noqa: BLE001 - vendor agents can fail on unsupported OIDs
        return None
    if error or status or not binds:
        return None
    return binds[0][1]


async def _walk(engine: Any, auth: Any, transport: Any, context: Any, oid: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        async for error, status, _index, binds in walk_cmd(
            engine,
            auth,
            transport,
            context,
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if error or status:
                break
            for bind in binds:
                result[_oid_text(bind[0])] = bind[1]
    except Exception:  # noqa: BLE001
        return result
    return result


def _table(rows: dict[str, Any], base_oid: str) -> dict[int, Any]:
    result: dict[int, Any] = {}
    prefix = base_oid.rstrip(".") + "."
    for oid, value in rows.items():
        if not oid.startswith(prefix):
            continue
        suffix = oid_suffix(oid[len(prefix) :])
        if len(suffix) == 1:
            result[suffix[0]] = value
    return result


def _table_by_suffix(rows: dict[str, Any], base_oid: str) -> dict[tuple[int, ...], Any]:
    result: dict[tuple[int, ...], Any] = {}
    prefix = base_oid.rstrip(".") + "."
    for oid, value in rows.items():
        if oid.startswith(prefix):
            result[tuple(oid_suffix(oid[len(prefix) :]))] = value
    return result


def _first_text(rows: dict[int, Any]) -> str:
    for value in rows.values():
        text = value_text(value)
        if text:
            return text
    return ""


def _build_interfaces(tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_index: dict[int, dict[str, Any]] = {}
    for key in ("name", "alias", "descr", "type", "mtu", "phys_address", "admin_status", "oper_status", "high_speed"):
        for index, value in _table(tables.get(key, {}), IF_OIDS[key]).items():
            row = by_index.setdefault(index, {"if_index": index})
            row[key] = value
    interfaces: list[dict[str, Any]] = []
    for index, row in sorted(by_index.items()):
        interfaces.append(
            {
                "if_index": index,
                "name": value_text(row.get("name")),
                "alias": value_text(row.get("alias")),
                "descr": value_text(row.get("descr")),
                "type": value_int(row.get("type")),
                "mtu": value_int(row.get("mtu")),
                "mac": value_mac(row.get("phys_address")),
                "admin_status": value_int(row.get("admin_status")),
                "oper_status": value_int(row.get("oper_status")),
                "speed": value_int(row.get("high_speed")),
                "candidates": interface_candidates(
                    value_text(row.get("name")),
                    value_text(row.get("alias")),
                    value_text(row.get("descr")),
                ),
            }
        )
    return interfaces


def _build_vlans(tables: dict[str, dict[str, Any]], interfaces: list[dict[str, Any]]) -> tuple[dict[int, str], dict[int, dict[str, list[int]]]]:
    names = {
        vid: value_text(value)
        for vid, value in _table(tables.get("vlan_name", {}), BRIDGE_OIDS["vlan_name"]).items()
    }
    pvid = {
        port: value_int(value)
        for port, value in _table(tables.get("pvid", {}), BRIDGE_OIDS["pvid"]).items()
    }
    egress = {
        vid: parse_bitmap(value)
        for vid, value in _table(tables.get("vlan_egress", {}), BRIDGE_OIDS["vlan_egress"]).items()
    }
    untagged = {
        vid: parse_bitmap(value)
        for vid, value in _table(tables.get("vlan_untagged", {}), BRIDGE_OIDS["vlan_untagged"]).items()
    }
    port_ifindex = {
        port: value_int(value)
        for port, value in _table(tables.get("port_ifindex", {}), BRIDGE_OIDS["port_ifindex"]).items()
    }
    ifindex_set = {interface["if_index"] for interface in interfaces}
    by_ifindex: dict[int, dict[str, list[int]]] = {}
    for bridge_port, ifindex in port_ifindex.items():
        if ifindex not in ifindex_set:
            continue
        pvid_vid = pvid.get(bridge_port)
        untagged_vids = {vid for vid, ports in untagged.items() if bridge_port in ports}
        egress_vids = {vid for vid, ports in egress.items() if bridge_port in ports}
        tagged_vids = {vid for vid in egress_vids if vid not in untagged_vids}
        if pvid_vid:
            untagged_vids.add(pvid_vid)
            tagged_vids.discard(pvid_vid)
        by_ifindex[ifindex] = {
            "untagged": sorted(vid for vid in untagged_vids if vid > 0),
            "tagged": sorted(vid for vid in tagged_vids if vid > 0),
        }
    return names, by_ifindex


def _build_lldp(tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    local_ports = {
        port: value_text(value)
        for port, value in _table(tables.get("lldp_loc_port_id", {}), LLDP_OIDS["loc_port_id"]).items()
    }
    local_descs = {
        port: value_text(value)
        for port, value in _table(tables.get("lldp_loc_port_desc", {}), LLDP_OIDS["loc_port_desc"]).items()
    }
    rows: dict[tuple[int, ...], dict[str, Any]] = {}
    for row_key, table_key, oid in (
        ("chassis_id", "lldp_rem_chassis_id", "rem_chassis_id"),
        ("port_id", "lldp_rem_port_id", "rem_port_id"),
        ("port_desc", "lldp_rem_port_desc", "rem_port_desc"),
        ("sys_name", "lldp_rem_sys_name", "rem_sys_name"),
    ):
        for suffix, value in _table_by_suffix(tables.get(table_key, {}), LLDP_OIDS[oid]).items():
            rows.setdefault(suffix, {})[row_key] = value_text(value)
    result: list[dict[str, Any]] = []
    for suffix, row in sorted(rows.items()):
        local_port = suffix[1] if len(suffix) >= 3 else (suffix[0] if suffix else 0)
        result.append(
            {
                "local_port": local_ports.get(local_port, ""),
                "local_desc": local_descs.get(local_port, ""),
                "remote_chassis": row.get("chassis_id", ""),
                "remote_port": row.get("port_id", ""),
                "remote_port_desc": row.get("port_desc", ""),
                "remote_sys_name": row.get("sys_name", ""),
            }
        )
    return result


def _build_fdb(tables: dict[str, dict[str, Any]], interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    addresses = _table_by_suffix(tables.get("fdb_address", {}), BRIDGE_OIDS["fdb_address"])
    ports = _table_by_suffix(tables.get("fdb_port", {}), BRIDGE_OIDS["fdb_port"])
    port_ifindex = {
        port: value_int(value)
        for port, value in _table(tables.get("port_ifindex", {}), BRIDGE_OIDS["port_ifindex"]).items()
    }
    iface_by_index = {interface["if_index"]: interface for interface in interfaces}
    result: list[dict[str, Any]] = []
    for suffix, mac_value in sorted(addresses.items()):
        bridge_port = value_int(ports.get(suffix))
        ifindex = port_ifindex.get(bridge_port)
        interface = iface_by_index.get(ifindex or -1, {})
        result.append(
            {
                "mac": value_mac(mac_value) or "",
                "bridge_port": bridge_port,
                "if_index": ifindex,
                "interface": (interface.get("candidates") or [""])[0],
            }
        )
    return result


async def _poll_target(
    target: dict[str, Any],
    *,
    community: str,
    timeout: float,
    retries: int,
) -> dict[str, Any] | None:
    ip = str(target.get("ip") or "")
    if not ip:
        return None
    engine = SnmpEngine()
    try:
        auth = CommunityData(community, mpModel=1)
        transport = await UdpTransportTarget.create((ip, 161), timeout=timeout, retries=retries)
        context = ContextData()
        system: dict[str, str] = {}
        for key, oid in SYSTEM_OIDS.items():
            value = await _get(engine, auth, transport, context, oid)
            if value is not None:
                system[key] = value_text(value)
        if not system.get("descr") and not system.get("name"):
            print(f"WARNING: SNMP {ip} did not return system information", file=sys.stderr)
            return None
        table_oids = {
            **IF_OIDS,
            **{f"entity_{key}": oid for key, oid in ENTITY_OIDS.items()},
            **{key: oid for key, oid in BRIDGE_OIDS.items() if key != "base_address"},
            **{f"lldp_{key}": oid for key, oid in LLDP_OIDS.items()},
        }
        values = await asyncio.gather(
            *(_walk(engine, auth, transport, context, oid) for oid in table_oids.values())
        )
        tables = dict(zip(table_oids.keys(), values))
        interfaces = _build_interfaces(tables)
        vlan_names, interface_vlans = _build_vlans(tables, interfaces)
        base_mac_value = await _get(engine, auth, transport, context, BRIDGE_OIDS["base_address"])
        base_mac = value_mac(base_mac_value) if base_mac_value is not None else None
        serial = _first_text(_table(tables.get("entity_serial", {}), ENTITY_OIDS["serial"]))
        model = _first_text(_table(tables.get("entity_model", {}), ENTITY_OIDS["model"]))
        return {
            "ip": ip,
            "target_name": str(target.get("name") or ""),
            "sys": system,
            "base_mac": base_mac or "",
            "serial": serial,
            "model": model,
            "interfaces": interfaces,
            "vlans": vlan_names,
            "interface_vlans": interface_vlans,
            "lldp": _build_lldp(tables),
            "fdb": _build_fdb(tables, interfaces),
        }
    except Exception as exc:  # noqa: BLE001 - a source failure must not stop the inventory
        print(f"WARNING: SNMP {ip}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    finally:
        engine.close_dispatcher()


async def collect_snmp(
    targets: list[dict[str, Any]],
    *,
    community: str,
    timeout: float = 2.0,
    retries: int = 1,
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    _require_pysnmp()
    semaphore = asyncio.Semaphore(concurrency)

    async def run(target: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            return await _poll_target(
                target, community=community, timeout=timeout, retries=retries
            )

    results = await asyncio.gather(*(run(target) for target in targets))
    return [result for result in results if result]


def write_snmp_report(path: Path, observations: list[dict[str, Any]], matches: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"matches": matches, "observations": observations},
            indent=2,
            sort_keys=True,
            default=list,
        )
        + "\n",
        encoding="utf-8",
    )
