from __future__ import annotations

import pytest
from collector.identity import DeviceResolver, MappingError


def manual_devices() -> dict:
    return {
        "devices": [
            {
                "name": "glemmen150",
                "talos_node": "glemmen150",
                "service_tag": "FL51JZ3",
                "redfish_ip": "192.168.4.150",
            },
            {
                "name": "glemmen160",
                "talos_node": "glemmen160",
                "service_tag": "DL51JZ3",
                "redfish_ip": "192.168.4.91",
            },
        ]
    }


def test_service_tag_selects_canonical_device() -> None:
    resolver = DeviceResolver(manual_devices())
    device = resolver.resolve(
        {"service_tag": {"FL51JZ3"}, "redfish_ip": {"192.168.4.150"}},
        source="redfish",
        label="192.168.4.150",
    )
    assert device is not None
    assert device["name"] == "glemmen150"


def test_conflicting_ip_and_service_tag_is_an_error() -> None:
    manual = manual_devices()
    manual["devices"][0].pop("redfish_ip", None)
    manual["devices"][1]["redfish_ip"] = "192.168.4.150"
    resolver = DeviceResolver(manual)
    with pytest.raises(MappingError, match="conflicting canonical device mappings"):
        resolver.resolve(
            {"service_tag": {"FL51JZ3"}, "redfish_ip": {"192.168.4.150"}},
            source="redfish",
            label="192.168.4.150",
        )


def test_observation_hardware_claim_can_resolve_later_source() -> None:
    resolver = DeviceResolver(
        {"devices": [{"name": "server110", "redfish_ip": "192.168.4.99"}]}
    )
    device = resolver.resolve(
        {"redfish_ip": {"192.168.4.99"}}, source="redfish", label="192.168.4.99"
    )
    assert device is not None
    resolver.add_observation_claims(
        device, {"service_tag": {"ABC1234"}, "system_uuid": {"uuid-1"}}
    )
    assert (
        resolver.resolve(
            {"service_tag": {"ABC1234"}}, source="talos", label="server110"
        )["name"]
        == "server110"
    )
