from __future__ import annotations

from collector.sources import ipmi
from collector.sources.ipmi import parse_fru, parse_fru_psus, parse_sdr


def test_parse_fru_extracts_system_identity() -> None:
    system = parse_fru(
        """
FRU Device Description : Builtin FRU Device (ID 0)
 Board Mfg             : Dell Inc.
 Board Product         : PowerEdge R515
 Board Serial          : JVCJGZ1
 Product Manufacturer  : Dell Inc.
 Product Name          : PowerEdge R515
 Product Serial        : JVCJGZ1
"""
    )
    assert system == {
        "Manufacturer": "Dell Inc.",
        "Model": "PowerEdge R515",
        "SerialNumber": "JVCJGZ1",
        "SKU": "JVCJGZ1",
    }


def test_parse_fru_prefers_board_product_when_product_name_is_truncated() -> None:
    system = parse_fru(
        """
FRU Device Description : Builtin FRU Device (ID 0)
 Board Mfg             : DELL
 Board Product         : PowerEdge R515
 Product Manufacturer  : DELL
 Product Name          : D
 Product Serial        : JVCJGZ1
"""
    )
    assert system["Model"] == "PowerEdge R515"
    assert system["SerialNumber"] == "JVCJGZ1"


def test_parse_fru_psus_extracts_power_supply_inventory() -> None:
    psus = parse_fru_psus(
        """
FRU Device Description : Builtin FRU Device (ID 0)
 Board Mfg             : DELL
 Board Product         : PowerEdge R515
 Product Serial        : JVCJGZ1

FRU Device Description : PS 1 (ID 2)
 Board Mfg             : DELL
 Board Product         : PWR SPLY,750W,RDNT,DELTA
 Board Serial          : CN1797236R09BE

FRU Device Description : PS 2 (ID 3)
 Board Mfg             : DELL
 Board Product         : PWR SPLY,750W,RDNT,DELTA
 Board Serial          : CN1797236R09BH
""",
        manufacturer="dell",
    )
    assert psus == [
        {
            "bay": "PSU 1",
            "manufacturer": "dell",
            "model": "PWR SPLY,750W,RDNT,DELTA",
            "serial": "CN1797236R09BE",
            "wattage": 750,
            "status": "active",
        },
        {
            "bay": "PSU 2",
            "manufacturer": "dell",
            "model": "PWR SPLY,750W,RDNT,DELTA",
            "serial": "CN1797236R09BH",
            "wattage": 750,
            "status": "active",
        },
    ]


def test_parse_sdr_extracts_fans_and_power_supplies() -> None:
    fans, psus = parse_sdr(
        """
 Fan1 RPM         | 4560 RPM          | ok
 Fan2 RPM         | 4500 RPM          | ok
 PS1 Status       | 0x01              | ok
 PS2 Status       | 0x01              | lnr
 Inlet Temp       | 20 degrees C      | ok
""",
        manufacturer="dell",
    )
    assert fans == [
        {
            "bay": "Fan1",
            "name": "Fan1 RPM",
            "rpm": 4560,
            "status": "active",
            "manufacturer": "dell",
        },
        {
            "bay": "Fan2",
            "name": "Fan2 RPM",
            "rpm": 4500,
            "status": "active",
            "manufacturer": "dell",
        },
    ]
    assert psus == [
        {
            "bay": "PSU 1",
            "manufacturer": "dell",
            "model": "Power Supply",
            "serial": "",
            "wattage": 0,
            "status": "active",
        },
        {
            "bay": "PSU 2",
            "manufacturer": "dell",
            "model": "Power Supply",
            "serial": "",
            "wattage": 0,
            "status": "failed",
        },
    ]


def test_read_ipmi_combines_fru_and_sensor_data(monkeypatch) -> None:
    outputs = iter(
        [
            "Product Name : PowerEdge R515\nProduct Serial : JVCJGZ1\n",
            "Fan1 RPM | 4560 RPM | ok\nPS1 Status | 0x01 | ok\n",
        ]
    )
    monkeypatch.setattr(ipmi.shutil, "which", lambda name: "/usr/bin/ipmitool")
    monkeypatch.setattr(ipmi, "_run", lambda *args: next(outputs))

    data = ipmi.read_ipmi("192.168.4.60", "readonly", "readonly")

    assert data is not None
    assert data["system"]["Model"] == "PowerEdge R515"
    assert data["fans"][0]["bay"] == "Fan1"
    assert data["psus"][0]["bay"] == "PSU 1"


def test_collect_ipmi_maps_result_to_manual_device(monkeypatch) -> None:
    from collector.cli import _collect_ipmi
    from collector.identity import DeviceResolver

    payload = {
        "system": {"Model": "PowerEdge R515", "SerialNumber": "JVCJGZ1"},
        "cpu": [],
        "memory": [],
        "disks": [],
        "fans": [{"bay": "Fan1", "name": "Fan1 RPM", "rpm": 4560, "status": "active"}],
        "psus": [],
    }
    monkeypatch.setattr("collector.cli.read_ipmi", lambda *args: payload)
    manual = {
        "devices": [
            {
                "name": "glemmen60",
                "ipmi_ip": "192.168.4.60",
                "service_tag": "JVCJGZ1",
            }
        ]
    }

    result = _collect_ipmi(
        manual, DeviceResolver(manual), username="readonly", password="readonly"
    )

    assert result == {"glemmen60": {**payload, "source": "ipmi"}}
