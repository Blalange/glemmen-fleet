"""Tests for the inventory semantic index and editor language features."""

from __future__ import annotations

from pathlib import Path

from inventory_index import InventoryIndex, Position

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
