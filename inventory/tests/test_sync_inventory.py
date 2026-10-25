from __future__ import annotations

from sync_inventory import _merge_section


def test_manual_layer_wins_for_device_fields() -> None:
    generated = [
        {"name": "server110", "site": "glemmen", "role": "server", "status": "offline"}
    ]
    manual = [
        {"name": "server110", "site": "glemmen", "role": "server", "status": "failed"}
    ]
    merged = _merge_section("devices", generated, manual)
    assert merged[0]["status"] == "failed"


def test_nested_modules_merge_by_bay() -> None:
    generated = [
        {
            "name": "glemmen130",
            "modules": [
                {"bay": "CPU 1", "module_type": "old", "description": "from source"}
            ],
        }
    ]
    manual = [
        {
            "name": "glemmen130",
            "modules": [{"bay": "CPU 1", "description": "manual override"}],
        }
    ]
    merged = _merge_section("devices", generated, manual)
    assert merged[0]["modules"][0]["module_type"] == "old"
    assert merged[0]["modules"][0]["description"] == "manual override"
