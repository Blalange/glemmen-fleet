from __future__ import annotations

from collector.sources.unifi import build_unifi


def test_unifi_serial_pins_manual_canonical_name() -> None:
    devices = [
        {
            "mac": "AA:BB:CC:DD:EE:FF",
            "name": "UniFi Switch",
            "serial": "SER1",
            "type": "usw",
            "model": "US24PRO",
            "state": 1,
            "port_table": [],
        }
    ]
    manual = {
        "settings": {"site": "glemmen", "default_tag": "production"},
        "unifi_models": {"US24PRO": "ubiquiti-usw-pro-24-poe"},
        "device_types_by_slug": {
            "ubiquiti-usw-pro-24-poe": {"model": "UniFi Switch 24 Pro PoE Gen2"}
        },
        "devices": [
            {
                "name": "Core Switch",
                "unifi_serial": "SER1",
            }
        ],
    }
    unifi_devices, _cables, _vlans, _prefixes, _names = build_unifi(
        devices, [], manual
    )
    assert unifi_devices[0]["name"] == "Core Switch"
    assert unifi_devices[0]["sources"] == ["unifi"]
