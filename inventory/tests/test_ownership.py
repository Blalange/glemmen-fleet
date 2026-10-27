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


def device_inventory(**device_fields: Any) -> dict[str, list[Any]]:
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
            **device_fields,
        }
    ]
    return inventory


def test_owner_groups_and_owners_merge_by_name() -> None:
    generated = [{"name": "Simon Bråten", "description": "from source"}]
    manual = [{"name": "Simon Bråten", "group": "Elev"}]
    merged = _merge_section("owners", generated, manual)
    assert merged == [
        {"name": "Simon Bråten", "description": "from source", "group": "Elev"}
    ]


def test_contact_roles_and_contacts_merge_by_natural_key() -> None:
    roles = _merge_section(
        "contact_roles",
        [{"name": "Technical contact", "slug": "technical-contact", "comments": "src"}],
        [{"name": "Technical contact", "slug": "technical-contact", "description": "manual"}],
    )
    contacts = _merge_section(
        "contacts",
        [{"name": "Simon Bråten", "description": "src"}],
        [{"name": "Simon Bråten", "title": "Elev"}],
    )
    assert roles == [
        {
            "name": "Technical contact",
            "slug": "technical-contact",
            "comments": "src",
            "description": "manual",
        }
    ]
    assert contacts == [
        {"name": "Simon Bråten", "description": "src", "title": "Elev"}
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
    inventory = device_inventory(owner="Simon Bråten")
    with pytest.raises(SyncError, match="unknown owner"):
        validate_inventory(inventory)


def test_contact_role_and_contact_references_validate() -> None:
    inventory = empty_inventory()
    inventory["contact_roles"] = [
        {"name": "Technical contact", "slug": "technical-contact"}
    ]
    inventory["contacts"] = [{"name": "Simon Bråten"}]
    validate_inventory(inventory)


def test_device_contact_requires_declared_contact() -> None:
    inventory = device_inventory(
        contacts=[{"contact": "Simon Bråten", "role": "technical-contact"}]
    )
    inventory["contact_roles"] = [
        {"name": "Technical contact", "slug": "technical-contact"}
    ]
    with pytest.raises(SyncError, match="unknown contact"):
        validate_inventory(inventory)


def test_device_contact_requires_declared_role() -> None:
    inventory = device_inventory(
        contacts=[{"contact": "Simon Bråten", "role": "technical-contact"}]
    )
    inventory["contacts"] = [{"name": "Simon Bråten"}]
    with pytest.raises(SyncError, match="unknown contact role"):
        validate_inventory(inventory)


def test_duplicate_device_contact_role_is_rejected() -> None:
    inventory = device_inventory(
        contacts=[
            {"contact": "Simon Bråten", "role": "technical-contact"},
            {"contact": "Simon Bråten", "role": "technical-contact"},
        ]
    )
    inventory["contact_roles"] = [
        {"name": "Technical contact", "slug": "technical-contact"}
    ]
    inventory["contacts"] = [{"name": "Simon Bråten"}]
    with pytest.raises(SyncError, match="duplicate contact"):
        validate_inventory(inventory)


def test_repository_ownership_and_contact_assignments() -> None:
    inventory = load_inventory(Path(__file__).resolve().parents[1])

    assert any(item.get("name") ==
               "Glemmen VGS" for item in inventory["owners"])
    assert any(
        item.get("name") == "Technical contact"
        and item.get("slug") == "technical-contact"
        for item in inventory["contact_roles"]
    )
    assert {item.get("name") for item in inventory["contacts"]} == {
        "Simon Bråten",
        "Eric",
    }
    assert {
        device.get("owner") for device in inventory["devices"]
    } == {"Glemmen VGS"}

    contacts_by_device = {
        device["name"]: {
            (entry["contact"], entry["role"]) for entry in device.get("contacts", [])
        }
        for device in inventory["devices"]
        if device.get("contacts")
    }
    simon = {("Simon Bråten", "technical-contact")}
    assert {
        name for name, contacts in contacts_by_device.items() if contacts == simon
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
    eric = {("Eric", "technical-contact")}
    assert {
        name for name, contacts in contacts_by_device.items() if contacts == eric
    } == {"LeanderWin10", "server70"}
