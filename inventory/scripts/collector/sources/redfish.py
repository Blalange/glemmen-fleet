"""Dell iDRAC Redfish source adapter."""

from __future__ import annotations

import sys
from typing import Any

import requests

from ..hardware import normalize_manufacturer


def _redfish_session(username: str, password: str) -> requests.Session:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.auth = (username, password)
    session.verify = False
    session.headers.update({"Accept": "application/json", "User-Agent": "glemmen-fleet-netbox/3.0"})
    return session

def _redfish_get(session: requests.Session, base: str, path: str) -> dict | None:
    try:
        response = session.get(f"{base}{path}", timeout=30)
    except requests.RequestException as exc:
        print(f"WARNING: Redfish {base}{path}: {exc}", file=sys.stderr)
        return None
    if response.status_code != 200:
        print(f"WARNING: Redfish {base}{path}: HTTP {response.status_code}", file=sys.stderr)
        return None
    try:
        return response.json()
    except ValueError:
        return None

def read_redfish(base: str, username: str, password: str) -> dict[str, Any] | None:
    """Read CPU, memory, disk, fan and PSU data from a Dell iDRAC."""
    session = _redfish_session(username, password)
    system = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1")
    if not system:
        return None
    result: dict[str, Any] = {"system": system, "cpu": [], "memory": [], "disks": [], "fans": [], "psus": []}

    processors = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1/Processors")
    for index, member in enumerate((processors or {}).get("Members", []), 1):
        data = _redfish_get(session, base, member["@odata.id"])
        if not data:
            continue
        oem = ((data.get("Oem") or {}).get("Dell") or {}).get("DellProcessor") or {}
        result["cpu"].append(
            {
                "bay": data.get("Name") or f"CPU {index}",
                "manufacturer": normalize_manufacturer(str(data.get("Manufacturer") or "Intel")),
                "model": str(data.get("Model") or f"CPU {index}"),
                "cores": int(data.get("TotalCores") or data.get("TotalEnabledCores") or 0),
                "speed": float(oem.get("CurrentClockSpeedMhz") or data.get("OperatingSpeedMHz") or data.get("MaxSpeedMHz") or 0) / 1000,
                "architecture": str(data.get("InstructionSet") or "x86_64").lower(),
                "serial": str(data.get("SerialNumber") or ""),
            }
        )

    memory = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1/Memory")
    for index, member in enumerate((memory or {}).get("Members", []), 1):
        data = _redfish_get(session, base, member["@odata.id"])
        if not data or not data.get("CapacityMiB"):
            continue
        result["memory"].append(
            {
                "bay": str(data.get("DeviceLocator") or data.get("Name") or f"DIMM {index}"),
                "manufacturer": normalize_manufacturer(str(data.get("Manufacturer") or "")),
                "model": str(data.get("PartNumber") or data.get("Name") or f"DIMM {index}"),
                "size_gb": int(data.get("CapacityMiB") or 0) // 1024,
                "class": str(data.get("MemoryDeviceType") or "DDR4"),
                "ecc": str(data.get("ErrorCorrection") or "").lower() not in {"", "no", "none"},
                "data_rate": int((data.get("OperatingSpeedMhz") or (data.get("AllowedSpeedsMHz") or [0])[0]) or 0),
                "serial": str(data.get("SerialNumber") or ""),
            }
        )

    storage = _redfish_get(session, base, "/redfish/v1/Systems/System.Embedded.1/Storage")
    seen_disks: set[str] = set()
    for controller_ref in (storage or {}).get("Members", []):
        controller = _redfish_get(session, base, controller_ref["@odata.id"])
        for drive_ref in (controller or {}).get("Drives", []):
            data = _redfish_get(session, base, drive_ref["@odata.id"])
            if not data:
                continue
            key = str(data.get("SerialNumber") or data.get("Id") or "")
            if key in seen_disks:
                continue
            seen_disks.add(key)
            media = str(data.get("MediaType") or "HDD").upper()
            media_type = {"HDD": "HD", "SSD": "SSD", "NVME": "NVME"}.get(media, media)
            result["disks"].append(
                {
                    "bay": f"Drive {len(result['disks']) + 1}",
                    "manufacturer": normalize_manufacturer(str(data.get("Manufacturer") or data.get("Model") or "")),
                    "model": str(data.get("Model") or f"Disk {len(result['disks']) + 1}"),
                    "size_gb": int(int(data.get("CapacityBytes") or 0) / 1_000_000_000),
                    "type": media_type,
                    "speed": int(data.get("RotationSpeedRPM") or 0),
                    "serial": str(data.get("SerialNumber") or ""),
                    "protocol": str(data.get("Protocol") or ""),
                }
            )

    thermal = _redfish_get(session, base, "/redfish/v1/Chassis/System.Embedded.1/Thermal")
    for index, fan in enumerate((thermal or {}).get("Fans", []), 1):
        result["fans"].append(
            {
                "bay": f"Fan {index}",
                "name": str(fan.get("Name") or fan.get("FanName") or f"Fan {index}"),
                "rpm": int(fan.get("Reading") or 0),
                "status": "active" if (fan.get("Status") or {}).get("State") != "Absent" else "inventory",
            }
        )

    power = _redfish_get(session, base, "/redfish/v1/Chassis/System.Embedded.1/Power")
    for index, psu in enumerate((power or {}).get("PowerSupplies", []), 1):
        result["psus"].append(
            {
                "bay": f"PSU {index}",
                "manufacturer": normalize_manufacturer(str(psu.get("Manufacturer") or "Dell")),
                "model": str(psu.get("Model") or f"Power Supply {index}"),
                "serial": str(psu.get("SerialNumber") or ""),
                "wattage": int(psu.get("PowerCapacityWatts") or 0),
                "status": "active" if (psu.get("Status") or {}).get("State") != "Absent" else "inventory",
            }
        )

    return result
