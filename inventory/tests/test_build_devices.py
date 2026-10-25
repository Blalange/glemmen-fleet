from __future__ import annotations

from collector.build_devices import build_servers


def redfish_payload() -> dict:
    return {
        "system": {"SKU": "ABC1234", "UUID": "uuid-1"},
        "cpu": [
            {
                "bay": "CPU 1",
                "manufacturer": "intel",
                "model": "Intel Xeon E-2314",
                "cores": 4,
                "speed": 2.8,
                "architecture": "x86_64",
                "serial": "",
            }
        ],
        "memory": [
            {
                "bay": "DIMM A1",
                "manufacturer": "micron",
                "model": "TEST-DIMM",
                "size_gb": 16,
                "class": "DDR4",
                "ecc": True,
                "data_rate": 3200,
                "serial": "S1",
            }
        ],
        "disks": [
            {
                "bay": "Drive 1",
                "manufacturer": "seagate",
                "model": "TEST-DISK",
                "size_gb": 1000,
                "type": "HD",
                "speed": 7200,
                "serial": "D1",
            }
        ],
        "fans": [
            {"bay": "Fan 1", "name": "Fan 1", "rpm": 3600, "status": "active"}
        ],
        "psus": [],
    }


def test_manual_only_idrac_server_gets_redfish_modules() -> None:
    manual = {
        "settings": {"site": "glemmen", "default_tag": "production"},
        "devices": [
            {
                "name": "server110",
                "role": "server",
                "device_type": "dell-poweredge-r250",
                "idrac_ip": "192.168.4.99",
            }
        ],
    }
    servers = build_servers(
        [],
        manual,
        "USW Pro 24 PoE",
        module_types={},
        redfish_by_name={"server110": redfish_payload()},
    )
    assert len(servers) == 1
    server = servers[0]
    assert server["name"] == "server110"
    assert len(server["module_bays"]) == 4
    assert len(server["modules"]) == 4
    assert "interfaces" not in server
