"""Tests for the inventory semantic index and editor language features."""

from __future__ import annotations

from pathlib import Path

from inventory_index import InventoryIndex, Position
from sync_inventory import load_inventory
from sync_validate import collect_inventory_problems

BASE_TYPES = """\
layer: manual
sites:
  - name: Glemmen
    slug: glemmen
manufacturers:
  - name: Dell
    slug: dell
device_roles:
  - name: Server
    slug: server
device_types:
  - manufacturer: dell
    model: PowerEdge R250
    slug: dell-poweredge-r250
    u_height: 1
"""

BASE_DEVICES = """\
layer: manual
devices:
  - name: server01
    site: glemmen
    role: server
    device_type: dell-poweredge-r250
"""


def write_inventory(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "inventory"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def position_of(text: str, needle: str, offset: int = 0) -> Position:
    index = text.index(needle) + offset
    line = text[:index].count("\n")
    character = index - text.rfind("\n", 0, index) - 1
    return Position(line, character)


def codes(index: InventoryIndex) -> list[str]:
    return [diagnostic.code for diagnostic in index.diagnostics]


def test_repository_inventory_has_no_diagnostics() -> None:
    index = InventoryIndex(Path(__file__).resolve().parents[1])
    assert index.diagnostics == []


def test_unknown_reference_is_a_warning(tmp_path: Path) -> None:
    devices = BASE_DEVICES.replace("dell-poweredge-r250", "missing-device-type")
    root = write_inventory(
        tmp_path,
        {"manual/types.yaml": BASE_TYPES, "manual/devices.yaml": devices},
    )
    index = InventoryIndex(root)
    assert any(
        diagnostic.code == "unknown-reference"
        and diagnostic.severity == "warning"
        and "missing-device-type" in diagnostic.message
        for diagnostic in index.diagnostics
    )


def test_incomplete_rack_layout_is_an_error(tmp_path: Path) -> None:
    racks = """\
layer: manual
racks:
  - name: Glemmen rack
    site: glemmen
    u_height: 2
    layout:
      - device: server01
"""
    root = write_inventory(
        tmp_path,
        {
            "manual/types.yaml": BASE_TYPES,
            "manual/devices.yaml": BASE_DEVICES,
            "manual/racks.yaml": racks,
        },
    )
    index = InventoryIndex(root)
    errors = [diagnostic for diagnostic in index.diagnostics if diagnostic.severity == "error"]
    assert any(diagnostic.code == "rack-layout-incomplete" for diagnostic in errors)
    assert any("uses 1 of 2 units" in diagnostic.message for diagnostic in errors)


def test_complete_rack_layout_has_no_layout_error(tmp_path: Path) -> None:
    racks = """\
layer: manual
racks:
  - name: Glemmen rack
    site: glemmen
    u_height: 2
    layout:
      - device: server01
      - gap: 1
"""
    root = write_inventory(
        tmp_path,
        {
            "manual/types.yaml": BASE_TYPES,
            "manual/devices.yaml": BASE_DEVICES,
            "manual/racks.yaml": racks,
        },
    )
    index = InventoryIndex(root)
    assert "rack-layout-incomplete" not in codes(index)


def test_unknown_field_and_wrong_type_are_reported(tmp_path: Path) -> None:
    devices = """\
layer: manual
devices:
  - name: server01
    site: glemmen
    role: server
    device_type: dell-poweredge-r250
    bogus: true
    status: 42
"""
    root = write_inventory(
        tmp_path,
        {"manual/types.yaml": BASE_TYPES, "manual/devices.yaml": devices},
    )
    index = InventoryIndex(root)
    assert "unknown-field" in codes(index)
    assert "type-mismatch" in codes(index)


def test_definition_completion_and_rename(tmp_path: Path) -> None:
    devices = BASE_DEVICES
    root = write_inventory(
        tmp_path,
        {"manual/types.yaml": BASE_TYPES, "manual/devices.yaml": devices},
    )
    index = InventoryIndex(root)
    devices_path = root / "manual/devices.yaml"

    reference_position = position_of(devices, "dell-poweredge-r250", 3)
    definitions = index.definition_at(devices_path, reference_position)
    assert len(definitions) == 1
    assert definitions[0].path == root / "manual/types.yaml"
    assert definitions[0].range.start.line == 13

    completions = index.completions_at(
        devices_path,
        position_of(devices, "device_type:", len("device_type:")),
    )
    assert any(item.label == "dell-poweredge-r250" for item in completions)

    info = index.prepare_rename_at(devices_path, reference_position)
    assert info is not None
    assert info.placeholder == "dell-poweredge-r250"

    changes = index.rename_at(devices_path, reference_position, "dell-poweredge-r250-v2")
    assert changes is not None
    assert set(changes) == {root / "manual/types.yaml", root / "manual/devices.yaml"}
    assert any(
        edit.new_text == "dell-poweredge-r250-v2"
        for edits in changes.values()
        for edit in edits
    )


def test_rename_device_updates_generated_definition_and_rack_layout(tmp_path: Path) -> None:
    generated = """\
layer: generated
devices:
  - name: server01
    site: glemmen
    role: server
    device_type: dell-poweredge-r250
"""
    racks = """\
layer: manual
racks:
  - name: Glemmen rack
    site: glemmen
    u_height: 1
    layout:
      - device: server01
"""
    root = write_inventory(
        tmp_path,
        {
            "manual/types.yaml": BASE_TYPES,
            "manual/devices.yaml": BASE_DEVICES,
            "manual/racks.yaml": racks,
            "generated/devices.yaml": generated,
        },
    )
    index = InventoryIndex(root)
    devices_path = root / "manual/devices.yaml"
    position = position_of(BASE_DEVICES, "server01", 3)
    changes = index.rename_at(devices_path, position, "server02")
    assert changes is not None
    assert root / "generated/devices.yaml" in changes
    assert root / "manual/racks.yaml" in changes
    assert any(
        edit.new_text == "server02"
        for edits in changes.values()
        for edit in edits
    )


def test_completion_for_rack_layout_device(tmp_path: Path) -> None:
    racks = """\
layer: manual
racks:
  - name: Glemmen rack
    site: glemmen
    u_height: 1
    layout:
      - device: server01
"""
    root = write_inventory(
        tmp_path,
        {
            "manual/types.yaml": BASE_TYPES,
            "manual/devices.yaml": BASE_DEVICES,
            "manual/racks.yaml": racks,
        },
    )
    index = InventoryIndex(root)
    racks_path = root / "manual/racks.yaml"
    completions = index.completions_at(
        racks_path,
        position_of(racks, "device: server01", len("device: ")),
    )
    assert any(item.label == "server01" for item in completions)


def test_interface_boolean_and_cable_completions(tmp_path: Path) -> None:
    devices = """\
layer: manual
devices:
  - name: server01
    site: glemmen
    role: server
    device_type: dell-poweredge-r250
    interfaces:
      - name: Port 1
        type: 1000base-t
        enabled: true
cables:
  - a_device: server01
    a_interface: Port 1
    b_device: server01
    b_interface: Port 1
    type: cat6
"""
    root = write_inventory(
        tmp_path,
        {"manual/types.yaml": BASE_TYPES, "manual/devices.yaml": devices},
    )
    index = InventoryIndex(root)
    devices_path = root / "manual/devices.yaml"

    interface_completions = index.completions_at(
        devices_path,
        position_of(devices, "type: 1000base-t", len("type: ")),
    )
    interface_labels = {item.label for item in interface_completions}
    assert {"1000base-t", "10gbase-x-sfpp", "lag", "virtual"} <= interface_labels

    boolean_completions = index.completions_at(
        devices_path,
        position_of(devices, "enabled: true", len("enabled: ")),
    )
    assert [item.label for item in boolean_completions] == ["true", "false"]

    cable_completions = index.completions_at(
        devices_path,
        position_of(devices, "type: cat6", len("type: ")),
    )
    cable_labels = {item.label for item in cable_completions}
    assert {"cat6", "cat6a", "dac-passive", "smf-os2"} <= cable_labels


def test_component_template_and_module_profile_completions(tmp_path: Path) -> None:
    device_types = """\
layer: manual
device_types:
  - manufacturer: dell
    model: R250
    slug: dell-r250
    power_ports:
      - name: PSU 1
        type: iec-60320-c14
    console_ports:
      - name: Console
        type: rj-45
    interfaces:
      - name: Port 1
        type: 1000base-t
        poe_mode: pse
        poe_type: type2-ieee802.3at
module_types:
  - manufacturer: dell
    model: CPU
    slug: dell-cpu
    profile: CPU
"""
    root = write_inventory(tmp_path, {"manual/types.yaml": device_types})
    index = InventoryIndex(root)
    path = root / "manual/types.yaml"

    power = index.completions_at(
        path,
        position_of(device_types, "type: iec-60320-c14", len("type: ")),
    )
    assert "iec-60320-c14" in {item.label for item in power}

    console = index.completions_at(
        path,
        position_of(device_types, "type: rj-45", len("type: ")),
    )
    assert "rj-45" in {item.label for item in console}

    poe = index.completions_at(
        path,
        position_of(device_types, "poe_mode: pse", len("poe_mode: ")),
    )
    assert {item.label for item in poe} == {"pd", "pse"}

    profile = index.completions_at(
        path,
        position_of(device_types, "profile: CPU", len("profile: ")),
    )
    assert {"CPU", "Memory", "Hard disk", "Power supply"} <= {
        item.label for item in profile
    }


def test_top_level_key_completion_after_a_sequence(tmp_path: Path) -> None:
    text = """\
layer: manual
sites:
  - name: Glemmen
    slug: glemmen
"""
    root = write_inventory(tmp_path, {"manual/sites.yaml": text})
    index = InventoryIndex(root)
    path = root / "manual/sites.yaml"
    completions = index.completions_at(path, Position(len(text.splitlines()), 0))
    labels = {item.label for item in completions}
    assert {"devices", "device_types", "racks", "vlans"} <= labels


def test_shared_validator_and_lsp_report_the_same_semantic_problems(tmp_path: Path) -> None:
    devices = BASE_DEVICES.replace("dell-poweredge-r250", "missing-device-type")
    racks = """\
layer: manual
racks:
  - name: Glemmen rack
    site: glemmen
    u_height: 2
    layout:
      - device: server01
"""
    root = write_inventory(
        tmp_path,
        {
            "manual/types.yaml": BASE_TYPES,
            "manual/devices.yaml": devices,
            "manual/racks.yaml": racks,
        },
    )

    loader_problems = []
    inventory = load_inventory(root, problems=loader_problems)
    shared_problems = loader_problems + collect_inventory_problems(inventory)
    shared_codes = {problem.code for problem in shared_problems}
    assert {"rack-layout-incomplete", "unknown-reference"} <= shared_codes
    assert len(shared_problems) >= 2

    index = InventoryIndex(root)
    lsp_codes = {diagnostic.code for diagnostic in index.diagnostics}
    assert {"rack-layout-incomplete", "unknown-reference"} <= lsp_codes


def test_unsaved_override_is_indexed(tmp_path: Path) -> None:
    root = write_inventory(
        tmp_path,
        {"manual/types.yaml": BASE_TYPES, "manual/devices.yaml": BASE_DEVICES},
    )
    devices_path = (root / "manual/devices.yaml").resolve()
    overridden = BASE_DEVICES.replace("dell-poweredge-r250", "missing-device-type")
    index = InventoryIndex(root, overrides={devices_path: overridden})
    assert any(
        diagnostic.code == "unknown-reference" and diagnostic.path == devices_path
        for diagnostic in index.diagnostics
    )
