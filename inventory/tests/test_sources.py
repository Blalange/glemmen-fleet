from __future__ import annotations

from typing import Any

from sync_context import SyncContext
from sync_inventory import _merge_section, _stamp_sources


def test_manual_item_and_nested_components_get_manual_source() -> None:
    item: dict[str, Any] = {
        "name": "server1",
        "interfaces": [
            {
                "name": "eth0",
                "ip_addresses": [{"address": "192.0.2.10/24"}],
            }
        ],
        "module_bays": [{"name": "Drive 1"}],
        "modules": [{"bay": "Drive 1"}],
    }

    _stamp_sources(item, "manual")

    assert item["sources"] == ["manual"]
    assert item["interfaces"][0]["sources"] == ["manual"]
    assert item["interfaces"][0]["ip_addresses"][0]["sources"] == ["manual"]
    assert item["module_bays"][0]["sources"] == ["manual"]
    assert item["modules"][0]["sources"] == ["manual"]


def test_generated_components_keep_specific_sources() -> None:
    item: dict[str, Any] = {
        "name": "glemmen60",
        "sources": ["talos", "ipmi"],
        "interfaces": [{"name": "eno1"}],
        "modules": [{"bay": "PSU 1", "sources": ["ipmi"]}],
    }

    _stamp_sources(item, "generated")

    assert item["sources"] == ["ipmi", "talos"]
    assert item["interfaces"][0]["sources"] == ["ipmi", "talos"]
    assert item["modules"][0]["sources"] == ["ipmi"]


def test_layer_merge_unions_sources() -> None:
    generated = [
        {"name": "glemmen60", "sources": ["talos", "ipmi"]},
    ]
    manual = [
        {"name": "glemmen60", "sources": ["manual"]},
    ]

    merged = _merge_section("devices", generated, manual)

    assert merged[0]["sources"] == ["ipmi", "manual", "talos"]


def test_tags_for_combines_inventory_tags_and_sources() -> None:
    context = SyncContext(None, {})  # type: ignore[arg-type]

    assert context.tags_for(
        {"tags": ["production"], "sources": ["talos", "manual"]}
    ) == ["manual", "production", "talos"]
