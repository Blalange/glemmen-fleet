from __future__ import annotations

from types import SimpleNamespace

from collector.snmp_merge import merge_snmp_observations
from collector.identity import DeviceResolver
from collector.sources.snmp_oids import interface_candidate_priority, interface_candidates, parse_bitmap


def test_vendor_interface_names_map_to_ports() -> None:
    assert interface_candidates("swp00", "", "") == ["Port 1"]
    assert interface_candidates("0/1", "Port 1", "Slot: 0 Port: 1") == ["Port 1"]
    assert interface_candidates("eth9", "", "") == ["Port 9"]
    assert interface_candidates("eth1", "", "") == ["Port 1"]
    assert interface_candidate_priority("0/1", "Port 1", "Port 1") == 0
    assert interface_candidate_priority("eth9", "", "Port 9") == 1


def test_bridge_bitmap_parsing() -> None:
    value = SimpleNamespace(asOctets=lambda: b"\x80\x00\x20")
    assert parse_bitmap(value) == {1, 19}


def test_snmp_enriches_generated_interface_without_duplicate() -> None:
    resolver = DeviceResolver({"devices": []})
    resolver.register_generated_device(
        "USW Pro 24 PoE", {"snmp_name": {"USW Pro 24 PoE"}}
    )
    devices = [
        {
            "name": "USW Pro 24 PoE",
            "interfaces": [
                {
                    "name": "Port 1",
                    "enabled": True,
                    "mac_address": "ac:8b:a9:b5:f7:e9",
                    "tagged_vlans": [],
                }
            ],
        }
    ]
    vlans = [{"vid": 1, "name": "Default", "site": "glemmen"}]
    observations = [
        {
            "ip": "192.168.50.178",
            "sys": {"name": "USWPro24PoE"},
            "base_mac": "ac:8b:a9:b5:f7:e9",
            "serial": "AC8BA9B5F7E9",
            "vlans": {1: "default", 4: "Server tilbehor"},
            "interface_vlans": {1: {"untagged": [1], "tagged": [4]}},
            "interfaces": [
                {
                    "if_index": 1,
                    "name": "0/1",
                    "alias": "Port 1",
                    "descr": "Slot: 0 Port: 1",
                    "mac": "ac:8b:a9:b5:f7:e9",
                    "admin_status": 1,
                    "oper_status": 1,
                    "candidates": ["Port 1"],
                }
            ],
        }
    ]
    matches = merge_snmp_observations(devices, vlans, observations, resolver, {"settings": {"site": "glemmen"}})
    assert matches == {"192.168.50.178": "USW Pro 24 PoE"}
    assert devices[0]["sources"] == ["snmp"]
    assert devices[0]["interfaces"][0]["sources"] == ["snmp"]
    assert len(devices[0]["interfaces"]) == 1
    assert devices[0]["interfaces"][0]["untagged_vlan"] == 1
    assert devices[0]["interfaces"][0]["tagged_vlans"] == [4]
    assert devices[0]["serial"] == "AC8BA9B5F7E9"
    assert any(vlan["vid"] == 4 for vlan in vlans)


def test_manual_interface_values_are_not_overwritten() -> None:
    resolver = DeviceResolver(
        {
            "devices": [
                {
                    "name": "zyxel-gs1920-24",
                    "snmp_ip": "192.168.4.2",
                    "interfaces": [
                        {
                            "name": "Port 1",
                            "enabled": True,
                            "untagged_vlan": 4,
                            "tagged_vlans": [],
                        }
                    ],
                }
            ]
        }
    )
    devices: list[dict] = []
    vlans = [{"vid": 4, "name": "Server tilbehor", "site": "glemmen"}]
    observations = [
        {
            "ip": "192.168.4.2",
            "sys": {"name": "GS1920"},
            "base_mac": "58:8b:f3:fe:4e:f2",
            "serial": "",
            "vlans": {1: "1", 4: "Server-tilbehor"},
            "interface_vlans": {1: {"untagged": [1], "tagged": [4]}},
            "interfaces": [
                {
                    "if_index": 1,
                    "name": "swp00",
                    "alias": "",
                    "descr": "",
                    "mac": "58:8b:f3:fe:4e:f2",
                    "admin_status": 1,
                    "oper_status": 1,
                    "candidates": ["Port 1"],
                }
            ],
        }
    ]
    merge_snmp_observations(
        devices, vlans, observations, resolver, {"settings": {"site": "glemmen"}}
    )
    assert devices == []
