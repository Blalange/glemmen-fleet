from __future__ import annotations

from types import SimpleNamespace

from collector.snmp_merge import merge_snmp_observations
from collector.identity import DeviceResolver
from collector.sources.snmp import _build_vlans
from collector.sources.snmp_oids import (
    BRIDGE_OIDS,
    interface_candidate_priority,
    interface_candidates,
    parse_bitmap,
)


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


def _bitmap(*ports: int) -> SimpleNamespace:
    data = bytearray((max(ports) + 7) // 8)
    for port in ports:
        data[(port - 1) // 8] |= 0x80 >> ((port - 1) % 8)
    return SimpleNamespace(asOctets=lambda: bytes(data))


def test_access_port_vlan_uses_pvid_not_stale_untagged_bitmap() -> None:
    # The Zyxel reports VLAN 1 as untagged on every port even though ports 1-27
    # are access ports in VLAN 4. The PVID and egress bitmap must win.
    tables = {
        "vlan_name": {
            f"{BRIDGE_OIDS['vlan_name']}.1": SimpleNamespace(prettyPrint=lambda: "1"),
            f"{BRIDGE_OIDS['vlan_name']}.4": SimpleNamespace(
                prettyPrint=lambda: "Server-tilbehor"
            ),
        },
        "pvid": {
            **{
                f"{BRIDGE_OIDS['pvid']}.{port}": SimpleNamespace(
                    prettyPrint=lambda: "4"
                )
                for port in range(1, 28)
            },
            f"{BRIDGE_OIDS['pvid']}.28": SimpleNamespace(prettyPrint=lambda: "1"),
        },
        "vlan_egress": {
            f"{BRIDGE_OIDS['vlan_egress']}.1": _bitmap(28),
            f"{BRIDGE_OIDS['vlan_egress']}.4": _bitmap(*range(1, 29)),
        },
        "vlan_untagged": {
            f"{BRIDGE_OIDS['vlan_untagged']}.1": _bitmap(*range(1, 29)),
            f"{BRIDGE_OIDS['vlan_untagged']}.4": _bitmap(*range(1, 28)),
        },
        "port_ifindex": {
            f"{BRIDGE_OIDS['port_ifindex']}.{port}": SimpleNamespace(
                prettyPrint=lambda port=port: str(port)
            )
            for port in range(1, 29)
        },
    }
    interfaces = [{"if_index": index} for index in range(1, 29)]
    _names, vlans = _build_vlans(tables, interfaces)
    assert vlans[1] == {"untagged": [4], "tagged": []}
    assert vlans[27] == {"untagged": [4], "tagged": []}
    assert vlans[28] == {"untagged": [1], "tagged": [4]}


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


def test_snmp_managed_switch_generates_ports_and_cables() -> None:
    manual = {
        "settings": {"site": "glemmen"},
        "devices": [
            {
                "name": "zyxel-gs1920-24",
                "snmp_ip": "192.168.4.2",
                "snmp_interfaces": True,
                "snmp_cables": True,
            },
            {
                "name": "glemmen160",
                "interfaces": [
                    {
                        "name": "iDRAC",
                        "type": "1000base-t",
                        "mac_address": "6c:3c:8c:75:45:06",
                    }
                ],
            },
            {
                "name": "glemmen150",
                "interfaces": [
                    {
                        "name": "iDRAC",
                        "type": "1000base-t",
                        "mac_address": "6c:3c:8c:75:39:2e",
                    }
                ],
            },
        ],
    }
    resolver = DeviceResolver(manual)
    devices = [
        {
            "name": "Dream Machine Pro B bygg",
            "platform": "unifi",
            "interfaces": [
                {"name": "lan", "type": "virtual", "mac_address": "24:5a:4c:76:79:d1"},
                {"name": "Port 7", "type": "1000base-t", "mac_address": "24:5a:4c:76:79:d8"},
                {"name": "Port 8", "type": "1000base-t", "mac_address": "24:5a:4c:76:79:d9"},
            ],
        }
    ]
    vlans = [
        {"vid": 1, "name": "1", "site": "glemmen"},
        {"vid": 4, "name": "Server-tilbehor", "site": "glemmen"},
    ]
    cables: list[dict] = []

    def port(if_index: int, name: str, **extra) -> dict:
        return {
            "if_index": if_index,
            "name": name,
            "alias": "",
            "descr": name,
            "type": 6,
            "mtu": 1500,
            "mac": "58:8b:f3:fe:4e:f2",
            "admin_status": 1,
            "oper_status": 1,
            "speed": 1000,
            "candidates": [f"Port {if_index}"],
            **extra,
        }

    observations = [
        {
            "ip": "192.168.4.2",
            "sys": {"name": "GS1920"},
            "base_mac": "58:8b:f3:fe:4e:f2",
            "serial": "",
            "vlans": {1: "1", 4: "Server-tilbehor"},
            "interface_vlans": {
                1: {"untagged": [4], "tagged": []},
                2: {"untagged": [4], "tagged": []},
                28: {"untagged": [1], "tagged": [4]},
            },
            "interfaces": [
                port(1, "swp00"),
                port(2, "swp01"),
                port(28, "swp27"),
                {
                    "if_index": 20000,
                    "name": "enif0",
                    "alias": "",
                    "descr": "enif0",
                    "type": 6,
                    "mtu": 0,
                    "mac": "58:8b:f3:fe:4e:f2",
                    "admin_status": 1,
                    "oper_status": 1,
                    "speed": 0,
                    "candidates": [],
                },
            ],
            "lldp": [
                {
                    "local_port": "28",
                    "local_desc": "",
                    "remote_chassis": "24:5a:4c:76:79:d1",
                    "remote_port": "0x245a4c7679d8",
                    "remote_port_desc": "eth7",
                    "remote_sys_name": "DreamMachineProBbygg",
                }
            ],
            "fdb": [
                {"mac": "6c:3c:8c:75:45:06", "bridge_port": 1, "if_index": 1, "interface": "Port 1"},
                {"mac": "6c:3c:8c:75:39:2e", "bridge_port": 2, "if_index": 2, "interface": "Port 2"},
                {"mac": "24:5a:4c:76:79:d8", "bridge_port": 28, "if_index": 28, "interface": "Port 28"},
            ],
        }
    ]

    merge_snmp_observations(
        devices, vlans, observations, resolver, manual, cables=cables
    )

    switch = next(device for device in devices if device["name"] == "zyxel-gs1920-24")
    assert [interface["name"] for interface in switch["interfaces"]] == [
        "Port 1",
        "Port 2",
        "Port 28",
    ]
    assert switch["interfaces"][0]["mode"] == "access"
    assert switch["interfaces"][0]["untagged_vlan"] == 4
    assert switch["interfaces"][2]["mode"] == "tagged"
    assert switch["interfaces"][2]["untagged_vlan"] == 1
    assert switch["interfaces"][2]["tagged_vlans"] == [4]

    by_port = {
        cable["a_interface"]: (cable["b_device"], cable["b_interface"])
        for cable in cables
        if cable["a_device"] == "zyxel-gs1920-24"
    }
    assert by_port == {
        "Port 1": ("glemmen160", "iDRAC"),
        "Port 2": ("glemmen150", "iDRAC"),
        "Port 28": ("Dream Machine Pro B bygg", "Port 8"),
    }
    assert switch["interfaces"][0]["description"] == "glemmen160 iDRAC"
    assert switch["interfaces"][2]["description"] == "Dream Machine Pro B bygg Port 8"


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
