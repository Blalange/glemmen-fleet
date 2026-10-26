"""Hardware normalization and module-type construction."""

from __future__ import annotations

import re
from typing import Any

from inventory_util import slugify


def disk_manufacturer(model: str) -> str:
    model = model.upper()
    if model.startswith("ST"):
        return "seagate"
    if model.startswith("WD") or model.startswith("WDC"):
        return "western-digital"
    return "generic"

def normalize_manufacturer(name: str) -> str:
    value = name.strip().lower()
    if "intel" in value:
        return "intel"
    if "micron" in value:
        return "micron"
    if "amd" in value:
        return "amd"
    if "dell" in value:
        return "dell"
    if "ibm" in value or "lenovo" in value:
        return "ibm"
    if "seagate" in value or value.startswith("st"):
        return "seagate"
    if "western digital" in value or value.startswith("wd") or value.startswith("wdc"):
        return "western-digital"
    return "generic"

def clean_model(model: str) -> str:
    value = re.sub(r"\((?:R|TM|r|tm)\)", "", str(model))
    return re.sub(r"\s+", " ", value).strip()

def register_module_type(
    catalog: dict[str, dict[str, Any]],
    *,
    manufacturer: str,
    model: str,
    profile: str,
    attributes: dict[str, Any],
    bay_types: list[str],
    part_number: str = "",
    description: str = "",
    sources: list[str] | None = None,
) -> str:
    model = clean_model(model)
    if model.lower().startswith(manufacturer.lower() + " "):
        slug = slugify(model)
    else:
        slug = slugify(f"{manufacturer}-{model}")
    entry = catalog.setdefault(
        slug,
        {
            "manufacturer": manufacturer,
            "model": model,
            "slug": slug,
            "profile": profile,
            "attributes": attributes,
            "bay_types": bay_types,
            "part_number": part_number or model,
            "description": description,
        },
    )
    source_names = {
        str(source)
        for source in sources or []
        if source is not None and str(source)
    }
    if source_names:
        entry["sources"] = sorted({*entry.get("sources", []), *source_names})
    return slug

def memory_class(device_type: str) -> str:
    if device_type in {"dell-poweredge-r250", "dell-poweredge-r630"}:
        return "DDR4"
    return "DDR3"

def infer_memory_manufacturer(model: str, fallback: str) -> str:
    value = model.upper()
    if value.startswith("9ASF"):
        return "micron"
    if value.startswith("M393"):
        return "samsung"
    if value.startswith(("HMA", "HMT")):
        return "sk-hynix"
    return fallback

def disk_bay_type(device_type: str) -> str:
    if device_type in {"dell-poweredge-r630", "ibm-system-x3650-m3"}:
        return "drive-2-5"
    return "drive-3-5"

def add_module_bay(
    module_bays: list[dict[str, Any]],
    bay_name: str,
    bay_type: str,
    description: str,
    *,
    sources: list[str] | None = None,
) -> None:
    if any(bay["name"] == bay_name for bay in module_bays):
        return
    entry: dict[str, Any] = {
        "name": bay_name,
        "position": str(len(module_bays) + 1),
        "bay_type": bay_type,
        "description": description,
    }
    if sources:
        entry["sources"] = sorted(
            {
                str(source)
                for source in sources
                if source is not None and str(source)
            }
        )
    module_bays.append(entry)

def add_module_component(
    module_bays: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    bay_name: str,
    bay_type: str,
    bay_description: str,
    module_type: str,
    *,
    status: str = "active",
    serial: str = "",
    description: str = "",
    sources: list[str] | None = None,
) -> None:
    add_module_bay(
        module_bays, bay_name, bay_type, bay_description, sources=sources
    )
    entry: dict[str, Any] = {
        "bay": bay_name,
        "module_type": module_type,
        "status": status,
        "serial": serial,
        "description": description,
    }
    if sources:
        entry["sources"] = sorted(
            {
                str(source)
                for source in sources
                if source is not None and str(source)
            }
        )
    modules.append(entry)

def add_bmc_components(
    module_bays: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    bmc: dict[str, Any],
    device_type: str,
) -> None:
    """Add CPU, memory, disk, fan and PSU modules discovered through a BMC."""
    bay_disk_type = disk_bay_type(device_type)
    source = str(bmc.get("source") or "redfish")

    for cpu in bmc.get("cpu") or []:
        cpu_model = clean_model(cpu["model"])
        module_type = register_module_type(
            catalog,
            manufacturer=cpu["manufacturer"],
            model=cpu_model,
            profile="CPU",
            attributes={
                "cores": cpu["cores"],
                "speed": cpu["speed"],
                "architecture": cpu["architecture"],
            },
            bay_types=["cpu-socket"],
            part_number=cpu_model,
            description="Processor",
            sources=[source],
        )
        add_module_component(
            module_bays,
            modules,
            cpu["bay"],
            "cpu-socket",
            "Processor socket.",
            module_type,
            serial=cpu["serial"],
            description=cpu_model,
            sources=[source],
        )

    for memory in bmc.get("memory") or []:
        klass = str(memory["class"]).upper()
        bay_type = f"dimm-{klass.lower()}" if klass in {"DDR3", "DDR4", "DDR5"} else "dimm-ddr4"
        module_type = register_module_type(
            catalog,
            manufacturer=memory["manufacturer"],
            model=memory["model"],
            profile="Memory",
            attributes={
                "size": memory["size_gb"],
                "class": klass if klass in {"DDR3", "DDR4", "DDR5"} else "DDR4",
                "ecc": memory["ecc"],
                "data_rate": memory["data_rate"],
            },
            bay_types=[bay_type],
            part_number=memory["model"],
            description="Memory module",
            sources=[source],
        )
        add_module_component(
            module_bays,
            modules,
            memory["bay"],
            bay_type,
            "Memory module slot.",
            module_type,
            serial=memory["serial"],
            description=f"{memory['size_gb']} GB {klass} {memory['data_rate']} MT/s",
            sources=[source],
        )

    for disk in bmc.get("disks") or []:
        attributes: dict[str, Any] = {"size": disk["size_gb"], "type": disk["type"]}
        if disk["speed"]:
            attributes["speed"] = disk["speed"]
        module_type = register_module_type(
            catalog,
            manufacturer=disk["manufacturer"],
            model=disk["model"],
            profile="Hard disk",
            attributes=attributes,
            bay_types=[bay_disk_type],
            part_number=disk["model"],
            description="Disk drive",
            sources=[source],
        )
        add_module_component(
            module_bays,
            modules,
            disk["bay"],
            bay_disk_type,
            "Drive bay.",
            module_type,
            serial=disk["serial"],
            description=f"{disk['size_gb']} GB {disk['type']}",
            sources=[source],
        )

    for fan in bmc.get("fans") or []:
        fan_rpm = int(fan["rpm"]) if fan.get("rpm") is not None else 3600
        module_type = register_module_type(
            catalog,
            manufacturer=fan.get("manufacturer") or "dell",
            model="System Board Fan",
            profile="Fan",
            attributes={"rpm": fan_rpm},
            bay_types=["fan"],
            part_number="System Board Fan",
            description="Chassis cooling fan",
            sources=[source],
        )
        add_module_component(
            module_bays,
            modules,
            fan["bay"],
            "fan",
            "Chassis cooling fan.",
            module_type,
            status=fan["status"],
            description=fan["name"],
            sources=[source],
        )

    for psu in bmc.get("psus") or []:
        attributes: dict[str, Any] = {
            "input_current": "AC",
            "input_voltage": 230,
            "hot_swappable": True,
        }
        if psu["wattage"]:
            attributes["wattage"] = psu["wattage"]
        module_type = register_module_type(
            catalog,
            manufacturer=psu["manufacturer"],
            model=psu["model"],
            profile="Power supply",
            attributes=attributes,
            bay_types=["power-supply"],
            part_number=psu["model"],
            description="Hot-swap power supply",
            sources=[source],
        )
        add_module_component(
            module_bays,
            modules,
            psu["bay"],
            "power-supply",
            "Hot-swap power supply bay.",
            module_type,
            status=psu["status"],
            serial=psu["serial"],
            description=f"{psu['wattage']} W" if psu["wattage"] else "Power supply",
            sources=[source],
        )
