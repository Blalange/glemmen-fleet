from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from inventory_schema import SECTIONS
from inventory_util import SyncError
from sync_inventory import _merge_section, load_inventory
from sync_validate import validate_inventory


def empty_inventory() -> dict[str, list[Any]]:
    return {section: [] for section in SECTIONS}


def test_owner_groups_and_owners_merge_by_name() -> None:
    generated = [{"name": "Simon Bråten", "description": "from source"}]
    manual = [{"name": "Simon Bråten", "group": "Elev"}]
    merged = _merge_section("owners", generated, manual)
    assert merged == [
        {"name": "Simon Bråten", "description": "from source", "group": "Elev"}
    ]


def test_owner_group_and_owner_references_validate() -> None:
    inventory = empty_inventory()
    inventory["owner_groups"] = [{"name": "Elev"}]
    inventory["owners"] = [{"name": "Simon Bråten", "group": "Elev"}]
    validate_inventory(inventory)


def test_owner_requires_declared_group() -> None:
    inventory = empty_inventory()
    inventory["owners"] = [{"name": "Simon Bråten", "group": "Elev"}]
    with pytest.raises(SyncError, match="unknown owner group"):
        validate_inventory(inventory)


def test_device_requires_declared_owner() -> None:
    inventory = empty_inventory()
    inventory["sites"] = [{"name": "Glemmen", "slug": "glemmen"}]
    inventory["manufacturers"] = [{"name": "Generic", "slug": "generic"}]
    inventory["device_roles"] = [{"name": "Server", "slug": "server"}]
    inventory["device_types"] = [
        {"manufacturer": "generic", "model": "Server", "slug": "generic-server"}
    ]
    inventory["devices"] = [
        {
            "name": "server1",
            "site": "glemmen",
            "role": "server",
            "device_type": "generic-server",
            "owner": "Simon Bråten",
        }
    ]
    with pytest.raises(SyncError, match="unknown owner"):
        validate_inventory(inventory)


def test_repository_owner_assignments() -> None:
    inventory = load_inventory(Path(__file__).resolve().parents[1])

    assert any(item.get("name") == "Elev" for item in inventory["owner_groups"])
    assert any(
        item.get("name") == "Simon Bråten" and item.get("group") == "Elev"
        for item in inventory["owners"]
    )
    assert {
        device["name"]
        for device in inventory["devices"]
        if device.get("owner") == "Simon Bråten"
    } == {
        "apc-ats",
        "glemmen50",
        "glemmen60",
        "glemmen80",
        "glemmen130",
        "glemmen150",
        "glemmen160",
        "glemmen170",
        "glemmen180",
        "storwize-01",
        "storwize-02",
        "zyxel-gs1920-24",
    }
    validate_inventory(inventory)
