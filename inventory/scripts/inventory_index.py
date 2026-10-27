"""Position-aware inventory index, lint rules and editor language features.

The index parses the manual and generated YAML layers without merging them into
NetBox objects. It keeps the source location of every value so the language
server can report diagnostics and resolve definitions, references, completions
and renames.

The sync job remains the authority for what is written to NetBox. This module
mirrors the semantic rules that matter while editing: references between the
layers, rack layout arithmetic, field types, duplicate natural keys, image
paths, VLAN membership, cable/uplink endpoints and module compatibility.
"""

from __future__ import annotations

import difflib
import ipaddress
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from inventory_fields import TOP_LEVEL_SPEC, spec_for_path
from inventory_util import slugify

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
IGNORED_INVENTORY_FILES = {"kustomization.yaml", "kustomization.yml"}
LAYERS = ("generated", "manual")


# ---------------------------------------------------------------------------
# Source positions and parsed YAML nodes


@dataclass(frozen=True, order=True)
class Position:
    line: int
    character: int


@dataclass(frozen=True)
class Range:
    start: Position
    end: Position


@dataclass(frozen=True)
class Location:
    path: Path
    range: Range


@dataclass
class Diagnostic:
    path: Path
    range: Range
    severity: str
    code: str
    message: str
    related: list[Location] = field(default_factory=list)


@dataclass
class Completion:
    label: str
    kind: str
    detail: str = ""
    documentation: str = ""


@dataclass
class TextEdit:
    range: Range
    new_text: str


@dataclass
class RenameInfo:
    range: Range
    placeholder: str


@dataclass
class YEntry:
    key: Any
    key_range: Range
    value: YNode | None


class YNode:
    span: Range
    value: Any


@dataclass
class YScalar(YNode):
    span: Range
    value: Any


@dataclass
class YSeq(YNode):
    span: Range
    items: list[YNode]
    value: list[Any]


@dataclass
class YMap(YNode):
    span: Range
    entries: list[YEntry]
    value: dict[Any, Any]

    def entry(self, key: Any) -> YEntry | None:
        for entry in reversed(self.entries):
            if str(entry.key) == str(key):
                return entry
        return None

    def get(self, key: Any) -> YNode | None:
        entry = self.entry(key)
        return entry.value if entry else None

    def key_range(self, key: Any) -> Range:
        entry = self.entry(key)
        return entry.key_range if entry else self.span

    def present_keys(self) -> set[str]:
        return {str(entry.key) for entry in self.entries}


@dataclass
class YamlDocument:
    path: Path
    text: str
    root: YMap | None
    layer: str = "manual"
    parse_error: tuple[str, Range] | None = None


@dataclass
class Definition:
    kind: str
    key: Any
    layer: str
    path: Path
    span: Range
    name_range: Range
    name_field: str | None
    value: dict[str, Any]
    node: YMap

    @property
    def location(self) -> Location:
        return Location(self.path, self.name_range)

    @property
    def display(self) -> str:
        if isinstance(self.key, tuple):
            return str(self.key[-1])
        return str(self.key)


@dataclass
class Reference:
    kind: str
    key: Any
    path: Path
    span: Range
    field: str
    yaml_path: tuple[Any, ...] = ()
    allow_rename: bool = True


@dataclass
class RackState:
    definition: Definition
    u_height: int
    used: int = 0
    positions: dict[str, tuple[int, int, str]] = field(default_factory=dict)
    occupancy: dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# YAML parsing


def _position(mark: Any) -> Position:
    return Position(int(mark.line), int(mark.column))


def _span(node: Any) -> Range:
    return Range(_position(node.start_mark), _position(node.end_mark))


def _contains(span: Range, position: Position) -> bool:
    return (
        (span.start.line, span.start.character)
        <= (position.line, position.character)
        <= (span.end.line, span.end.character)
    )


def _build_node(node: Any, loader: yaml.SafeLoader) -> YNode:
    if isinstance(node, yaml.MappingNode):
        entries: list[YEntry] = []
        values: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            try:
                key = loader.construct_object(key_node, deep=True)
            except Exception:  # noqa: BLE001 - malformed key
                key = str(getattr(key_node, "value", ""))
            value = _build_node(value_node, loader)
            entries.append(YEntry(key=key, key_range=_span(key_node), value=value))
            values[key] = value.value
        return YMap(span=_span(node), entries=entries, value=values)
    if isinstance(node, yaml.SequenceNode):
        items = [_build_node(item, loader) for item in node.value]
        return YSeq(span=_span(node), items=items, value=[item.value for item in items])
    try:
        value = loader.construct_object(node, deep=True)
    except Exception:  # noqa: BLE001 - fall back to the scalar text
        value = getattr(node, "value", None)
    return YScalar(span=_span(node), value=value)


def _parse_file(path: Path, text: str) -> list[YamlDocument]:
    try:
        nodes = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        span = (
            Range(_position(mark), _position(mark))
            if mark is not None
            else Range(Position(0, 0), Position(0, 0))
        )
        return [YamlDocument(path=path, text=text, root=None, parse_error=(str(exc), span))]

    documents: list[YamlDocument] = []
    loader = yaml.SafeLoader(text)
    for node in nodes:
        if node is None:
            continue
        root = _build_node(node, loader)
        if not isinstance(root, YMap):
            documents.append(
                YamlDocument(
                    path=path,
                    text=text,
                    root=None,
                    parse_error=("inventory document root must be a mapping", root.span),
                )
            )
            continue
        layer_node = root.get("layer")
        layer = str(layer_node.value) if isinstance(layer_node, YScalar) else "manual"
        documents.append(YamlDocument(path=path, text=text, root=root, layer=layer))
    return documents


def inventory_files(root: Path) -> list[Path]:
    """Return the YAML files that make up the inventory."""

    files: set[Path] = set()
    root = Path(root)
    for directory_name in ("manual", "generated"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for pattern in ("*.yaml", "*.yml"):
            for path in directory.rglob(pattern):
                relative = path.relative_to(root)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if path.name in IGNORED_INVENTORY_FILES:
                    continue
                try:
                    files.add(path.resolve())
                except OSError:
                    files.add(path)
    return sorted(files)


def _walk_scalars(node: YNode, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], YScalar]]:
    if isinstance(node, YScalar):
        yield path, node
        return
    if isinstance(node, YSeq):
        for index, item in enumerate(node.items):
            yield from _walk_scalars(item, path + (index,))
        return
    if isinstance(node, YMap):
        for entry in node.entries:
            if entry.value is not None:
                yield from _walk_scalars(entry.value, path + (entry.key,))


def _node_at(root: YNode | None, path: tuple[Any, ...]) -> YNode | None:
    node = root
    for segment in path:
        if node is None:
            return None
        if isinstance(node, YMap):
            entry = node.entry(segment)
            node = entry.value if entry else None
        elif isinstance(node, YSeq) and isinstance(segment, int):
            node = node.items[segment] if 0 <= segment < len(node.items) else None
        else:
            return None
    return node


def _text(node: YMap | None, key: str) -> str | None:
    if node is None:
        return None
    child = node.get(key)
    if isinstance(child, YScalar) and isinstance(child.value, str):
        return child.value
    return None


def _integer(node: YMap | None, key: str) -> int | None:
    if node is None:
        return None
    child = node.get(key)
    if isinstance(child, YScalar) and isinstance(child.value, int) and not isinstance(child.value, bool):
        return child.value
    return None


def _boolean(node: YMap | None, key: str) -> bool | None:
    if node is None:
        return None
    child = node.get(key)
    if isinstance(child, YScalar) and isinstance(child.value, bool):
        return child.value
    return None


# ---------------------------------------------------------------------------
# Definitions and reference descriptors


SLUG_KINDS = {
    "site",
    "manufacturer",
    "platform",
    "device_role",
    "contact_role",
    "module_bay_type",
    "device_type",
    "module_type",
}
NAME_KINDS = {"owner_group", "owner", "contact", "device", "rack", "module_bay", "interface"}
VID_KINDS = {"vlan"}


def _reference_descriptor(path: tuple[Any, ...]) -> str | None:
    """Return the definition kind referenced by a scalar path, if any."""

    if not path:
        return None
    head = path[0]

    if head == "unifi_models" and len(path) == 2:
        return "device_type"

    if head == "settings" and len(path) == 2:
        return {"site": "site", "rack": "rack"}.get(str(path[1]))

    if head == "defaults" and len(path) == 2:
        return {
            "site": "site",
            "owner": "owner",
            "role": "device_role",
            "device_type": "device_type",
            "platform": "platform",
            "rack": "rack",
        }.get(str(path[1]))

    if len(path) < 3 or not isinstance(path[1], int):
        return None

    section = str(path[0])

    if section == "platforms" and len(path) == 3 and path[2] == "manufacturer":
        return "manufacturer"
    if section == "device_types" and len(path) == 3 and path[2] == "manufacturer":
        return "manufacturer"
    if section == "module_bay_types" and len(path) == 3 and path[2] == "manufacturer":
        return "manufacturer"
    if section == "owners" and len(path) == 3 and path[2] == "group":
        return "owner_group"

    if section == "module_types" and len(path) >= 3:
        if path[2] == "manufacturer" and len(path) == 3:
            return "manufacturer"
        if path[2] == "bay_types" and len(path) == 4:
            return "module_bay_type"

    if section == "racks" and len(path) >= 3:
        if path[2] == "site" and len(path) == 3:
            return "site"
        if path[2] == "layout" and len(path) == 5 and path[4] == "device":
            return "device"

    if section == "vlans" and len(path) == 3 and path[2] == "site":
        return "site"

    if section == "prefixes" and len(path) >= 3:
        if path[2] == "site" and len(path) == 3:
            return "site"
        if path[2] == "vlan" and len(path) == 3:
            return "vlan"

    if section == "cables" and len(path) == 3:
        return {
            "a_device": "device",
            "b_device": "device",
            "a_interface": "interface",
            "b_interface": "interface",
        }.get(str(path[2]))

    if section == "ip_addresses" and len(path) == 3:
        return {"device": "device", "interface": "interface"}.get(str(path[2]))

    if section == "devices" and len(path) >= 3:
        field_name = path[2]
        if field_name in {"site", "role", "device_type", "platform", "owner", "rack"} and len(path) == 3:
            return {
                "site": "site",
                "role": "device_role",
                "device_type": "device_type",
                "platform": "platform",
                "owner": "owner",
                "rack": "rack",
            }[str(field_name)]
        if field_name == "contacts" and len(path) == 5:
            return {"contact": "contact", "role": "contact_role"}.get(str(path[4]))
        if field_name == "interfaces" and len(path) >= 5:
            if path[4] == "untagged_vlan" and len(path) == 5:
                return "vlan"
            if path[4] == "tagged_vlans" and len(path) == 6:
                return "vlan"
        if field_name == "module_bays" and len(path) >= 5:
            if path[4] == "bay_type" and len(path) == 5:
                return "module_bay_type"
            if path[4] == "bay_types" and len(path) == 6:
                return "module_bay_type"
        if field_name == "modules" and len(path) == 5:
            return {"bay": "module_bay", "module_type": "module_type"}.get(str(path[4]))
        if field_name == "uplinks" and len(path) == 5 and path[4] == "switch":
            return "device"

    return None


def _effective_site(index: InventoryIndex, document: YamlDocument, path: tuple[Any, ...]) -> str | None:
    root = document.root
    if root is None:
        return None
    head = path[0] if path else None
    if head == "devices":
        device = _node_at(root, path[:2])
        if isinstance(device, YMap):
            value = _text(device, "site")
            if value:
                return value
            default = index.defaults.get("site")
            return str(default) if isinstance(default, str) else None
    if head == "racks":
        rack = _node_at(root, path[:2])
        if isinstance(rack, YMap):
            return _text(rack, "site")
    if head == "prefixes":
        prefix = _node_at(root, path[:2])
        if isinstance(prefix, YMap):
            return _text(prefix, "site")
    if head == "settings":
        settings = _node_at(root, ("settings",))
        if isinstance(settings, YMap):
            site = _text(settings, "site")
            if site:
                return site
    if head == "defaults":
        site = index.defaults.get("site")
        return str(site) if isinstance(site, str) else None
    return None


def _device_name(index: InventoryIndex, document: YamlDocument, path: tuple[Any, ...]) -> str | None:
    root = document.root
    if root is None:
        return None
    head = path[0] if path else None
    if head == "devices":
        device = _node_at(root, path[:2])
        if isinstance(device, YMap):
            return _text(device, "name")
    if head == "cables":
        cable = _node_at(root, path[:2])
        if isinstance(cable, YMap):
            if len(path) >= 3 and path[2] == "a_interface":
                return _text(cable, "a_device")
            if len(path) >= 3 and path[2] == "b_interface":
                return _text(cable, "b_device")
    if head == "ip_addresses":
        address = _node_at(root, path[:2])
        if isinstance(address, YMap):
            return _text(address, "device")
    return None


# ---------------------------------------------------------------------------
# Main index


class InventoryIndex:
    def __init__(
        self,
        root: Path,
        overrides: dict[Path, str] | None = None,
        parsed_documents: list[YamlDocument] | None = None,
    ):
        self.root = Path(root).resolve()
        self.overrides = {
            Path(path).resolve(): text for path, text in (overrides or {}).items()
        }
        self.documents: list[YamlDocument] = list(parsed_documents or [])
        self.defaults: dict[str, Any] = {}
        self.definitions: dict[str, list[Definition]] = defaultdict(list)
        self.references: list[Reference] = []
        self.diagnostics: list[Diagnostic] = []
        self.rack_states: dict[tuple[str | None, str], RackState] = {}
        self.device_rack_positions: dict[str, tuple[str | None, str, int, int, str]] = {}
        if parsed_documents is None:
            self._load()
        self._collect_defaults()
        self._build_definitions()
        self._build_references()
        self._lint()

    # -- loading ---------------------------------------------------------

    def _load(self) -> None:
        for path in inventory_files(self.root):
            text = self.overrides.get(path)
            if text is None:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    self.documents.append(
                        YamlDocument(
                            path=path,
                            text="",
                            root=None,
                            parse_error=(str(exc), Range(Position(0, 0), Position(0, 0))),
                        )
                    )
                    continue
            self.documents.extend(_parse_file(path, text))

    def _collect_defaults(self) -> None:
        # The loader applies defaults from every document in file order.
        for document in self.documents:
            if document.root is None:
                continue
            defaults = document.root.get("defaults")
            if isinstance(defaults, YMap):
                self.defaults.update(defaults.value)

    # -- definitions -----------------------------------------------------

    def _add_definition(
        self,
        kind: str,
        key: Any,
        document: YamlDocument,
        node: YMap,
        name_field: str | None,
        name_node: YNode | None = None,
    ) -> None:
        name_range = name_node.span if name_node is not None else node.span
        self.definitions[kind].append(
            Definition(
                kind=kind,
                key=key,
                layer=document.layer,
                path=document.path,
                span=node.span,
                name_range=name_range,
                name_field=name_field,
                value=node.value,
                node=node,
            )
        )

    def _build_definitions(self) -> None:
        for document in self.documents:
            root = document.root
            if root is None:
                continue
            self._section_definitions(document, root, "sites", "site")
            self._section_definitions(document, root, "manufacturers", "manufacturer")
            self._section_definitions(document, root, "platforms", "platform")
            self._section_definitions(document, root, "device_roles", "device_role")
            self._section_definitions(document, root, "owner_groups", "owner_group")
            self._section_definitions(document, root, "owners", "owner")
            self._section_definitions(document, root, "contact_roles", "contact_role")
            self._section_definitions(document, root, "contacts", "contact")
            self._section_definitions(document, root, "module_bay_types", "module_bay_type")
            self._section_definitions(document, root, "device_types", "device_type")
            self._section_definitions(document, root, "module_types", "module_type")
            self._section_definitions(document, root, "racks", "rack")
            self._section_definitions(document, root, "vlans", "vlan")
            self._section_definitions(document, root, "devices", "device")
            self._nested_device_definitions(document, root)

    def _section_definitions(
        self,
        document: YamlDocument,
        root: YMap,
        section: str,
        kind: str,
    ) -> None:
        section_node = root.get(section)
        if not isinstance(section_node, YSeq):
            return
        for item in section_node.items:
            if not isinstance(item, YMap):
                continue
            if kind in SLUG_KINDS:
                if kind == "device_type" or kind == "module_type":
                    manufacturer = _text(item, "manufacturer") or ""
                    model = _text(item, "model") or ""
                    explicit = _text(item, "slug")
                    key = explicit or slugify(f"{manufacturer}-{model}")
                    name_field = "slug" if explicit else None
                    name_node = item.get("slug") if explicit else None
                else:
                    name = _text(item, "name") or ""
                    explicit = _text(item, "slug")
                    key = explicit or slugify(name)
                    name_field = "slug" if explicit else None
                    name_node = item.get("slug") if explicit else None
            elif kind in NAME_KINDS:
                if kind == "rack":
                    site = _text(item, "site")
                    name = _text(item, "name") or ""
                    key = (site, name)
                    name_field = "name"
                    name_node = item.get("name")
                elif kind == "module_bay" or kind == "interface":
                    continue
                else:
                    name = _text(item, "name") or ""
                    key = name
                    name_field = "name"
                    name_node = item.get("name")
            elif kind == "vlan":
                site = _text(item, "site")
                vid = _integer(item, "vid")
                key = (site, vid)
                name_field = "vid"
                name_node = item.get("vid")
            else:
                continue
            self._add_definition(kind, key, document, item, name_field, name_node)

    def _nested_device_definitions(self, document: YamlDocument, root: YMap) -> None:
        devices = root.get("devices")
        if not isinstance(devices, YSeq):
            return
        for device in devices.items:
            if not isinstance(device, YMap):
                continue
            device_name = _text(device, "name")
            if not device_name:
                continue
            for section, kind in (("module_bays", "module_bay"), ("interfaces", "interface")):
                entries = device.get(section)
                if not isinstance(entries, YSeq):
                    continue
                for entry in entries.items:
                    if not isinstance(entry, YMap):
                        continue
                    name = _text(entry, "name")
                    if not name:
                        continue
                    self._add_definition(
                        kind,
                        (device_name, name),
                        document,
                        entry,
                        "name",
                        entry.get("name"),
                    )

    # -- references ------------------------------------------------------

    def _add_reference(
        self,
        kind: str,
        key: Any,
        document: YamlDocument,
        node: YScalar,
        field_name: str,
        yaml_path: tuple[Any, ...],
        *,
        allow_rename: bool = True,
    ) -> None:
        value = node.value
        if value is None or value == "":
            return
        self.references.append(
            Reference(
                kind=kind,
                key=key,
                path=document.path,
                span=node.span,
                field=field_name,
                yaml_path=yaml_path,
                allow_rename=allow_rename,
            )
        )

    def _reference_key(
        self,
        kind: str,
        value: Any,
        document: YamlDocument,
        path: tuple[Any, ...],
    ) -> Any:
        if kind == "rack":
            return (_effective_site(self, document, path), str(value))
        if kind == "vlan":
            try:
                vid = int(value)
            except (TypeError, ValueError):
                return None
            return (_effective_site(self, document, path), vid)
        if kind == "module_bay":
            return (_device_name(self, document, path), str(value))
        if kind == "interface":
            return (_device_name(self, document, path), str(value))
        return value

    def _build_references(self) -> None:
        for document in self.documents:
            root = document.root
            if root is None:
                continue
            for path, scalar in _walk_scalars(root):
                kind = _reference_descriptor(path)
                if kind is None:
                    continue
                key = self._reference_key(kind, scalar.value, document, path)
                if key is None:
                    continue
                field_name = str(path[-1]) if path else ""
                self._add_reference(kind, key, document, scalar, field_name, path)

    # -- diagnostics -----------------------------------------------------

    def _diag(
        self,
        path: Path,
        span: Range,
        severity: str,
        code: str,
        message: str,
        related: list[Location] | None = None,
    ) -> None:
        self.diagnostics.append(
            Diagnostic(
                path=path,
                range=span,
                severity=severity,
                code=code,
                message=message,
                related=related or [],
            )
        )

    def _lint(self) -> None:
        for document in self.documents:
            if document.parse_error is not None:
                message, span = document.parse_error
                self._diag(document.path, span, "error", "yaml-parse", message)
                continue
            if document.root is not None:
                self._validate_node(document, document.root, TOP_LEVEL_SPEC, ())
                if document.layer not in LAYERS:
                    span = document.root.key_range("layer")
                    self._diag(
                        document.path,
                        span,
                        "error",
                        "invalid-layer",
                        f"Unknown layer {document.layer!r}; expected generated or manual.",
                    )
        self._lint_natural_key_duplicates()
        self._lint_references()
        self._lint_racks()
        self._lint_uplinks_and_cables()
        self._lint_module_compatibility()
        self._lint_images()
        self.diagnostics.sort(
            key=lambda diagnostic: (
                str(diagnostic.path),
                diagnostic.range.start.line,
                diagnostic.range.start.character,
                diagnostic.code,
            )
        )

    def _validate_node(
        self,
        document: YamlDocument,
        node: YNode,
        spec: dict[str, Any],
        path: tuple[Any, ...],
    ) -> None:
        kind = spec.get("kind")
        if kind == "any":
            return

        if isinstance(node, YScalar) and node.value is None:
            if spec.get("nullable") or not spec.get("required"):
                return
            self._diag(
                document.path,
                node.span,
                "error",
                "missing-value",
                f"Field {path[-1] if path else 'document'} must not be empty.",
            )
            return

        if kind == "object":
            if not isinstance(node, YMap):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a mapping.",
                )
                return
            fields = spec.get("fields", {})
            present: dict[str, YNode] = {}
            seen: set[str] = set()
            for entry in node.entries:
                key = str(entry.key)
                if key in seen:
                    self._diag(
                        document.path,
                        entry.key_range,
                        "error",
                        "duplicate-key",
                        f"Duplicate key {key!r} in {_display_path(path)}.",
                    )
                seen.add(key)
                if entry.value is not None:
                    present[key] = entry.value
            for key, field_spec in fields.items():
                child = present.get(key)
                if child is None:
                    if field_spec.get("required"):
                        self._diag(
                            document.path,
                            node.key_range(key),
                            "error",
                            "missing-field",
                            f"Missing required field {key!r} in {_display_path(path)}.",
                        )
                    continue
                self._validate_node(document, child, field_spec, path + (key,))
            if not spec.get("allow_extra", False):
                for entry in node.entries:
                    key = str(entry.key)
                    if key not in fields:
                        self._diag(
                            document.path,
                            entry.key_range,
                            "warning",
                            "unknown-field",
                            f"Unknown field {key!r} in {_display_path(path)}.",
                        )
            if spec.get("variant") == "rack_layout":
                has_gap = "gap" in present
                has_empty = "empty" in present
                has_device = "device" in present
                if sum((has_gap, has_empty, has_device)) != 1:
                    self._diag(
                        document.path,
                        node.span,
                        "error",
                        "rack-layout-entry",
                        "Rack layout entry must have exactly one of gap, empty or device.",
                    )
            return

        if kind == "array":
            if not isinstance(node, YSeq):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a list.",
                )
                return
            item_spec = spec.get("items")
            if isinstance(item_spec, dict):
                for index, item in enumerate(node.items):
                    self._validate_node(document, item, item_spec, path + (index,))
            return

        if kind == "map":
            if not isinstance(node, YMap):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a mapping.",
                )
                return
            value_spec = spec.get("values")
            if isinstance(value_spec, dict):
                for entry in node.entries:
                    if entry.value is not None:
                        self._validate_node(document, entry.value, value_spec, path + (entry.key,))
            return

        if kind == "string":
            if not isinstance(node, YScalar) or not isinstance(node.value, str):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a string.",
                )
                return
        elif kind == "slug":
            if not isinstance(node, YScalar) or not isinstance(node.value, str):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a slug string.",
                )
                return
            if not SLUG_RE.fullmatch(node.value):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "invalid-slug",
                    f"{_display_path(path)} must be a lowercase slug, got {node.value!r}.",
                )
                return
        elif kind == "cidr":
            if not isinstance(node, YScalar) or not isinstance(node.value, str):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be an IP address in CIDR notation.",
                )
                return
            try:
                ipaddress.ip_interface(node.value)
            except ValueError:
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "invalid-address",
                    f"{_display_path(path)} contains an invalid IP address {node.value!r}.",
                )
                return
        elif kind == "mac":
            if not isinstance(node, YScalar) or not isinstance(node.value, str):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a MAC address.",
                )
                return
            if not MAC_RE.fullmatch(node.value):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "invalid-mac",
                    f"{_display_path(path)} contains an invalid MAC address {node.value!r}.",
                )
                return
        elif kind == "integer":
            if (
                not isinstance(node, YScalar)
                or not isinstance(node.value, int)
                or isinstance(node.value, bool)
            ):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be an integer.",
                )
                return
        elif kind == "number":
            if (
                not isinstance(node, YScalar)
                or not isinstance(node.value, (int, float))
                or isinstance(node.value, bool)
            ):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a number.",
                )
                return
        elif kind == "boolean":
            if not isinstance(node, YScalar) or not isinstance(node.value, bool):
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "type-mismatch",
                    f"{_display_path(path)} must be a boolean.",
                )
                return
        else:
            return

        if isinstance(node, YScalar):
            value = node.value
            allowed = spec.get("enum")
            if allowed is not None and value not in allowed:
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "invalid-value",
                    f"{_display_path(path)} must be one of {', '.join(map(str, allowed))}; got {value!r}.",
                )
            minimum = spec.get("minimum")
            if minimum is not None and isinstance(value, (int, float)) and value < minimum:
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "out-of-range",
                    f"{_display_path(path)} must be at least {minimum}; got {value!r}.",
                )
            maximum = spec.get("maximum")
            if maximum is not None and isinstance(value, (int, float)) and value > maximum:
                self._diag(
                    document.path,
                    node.span,
                    "error",
                    "out-of-range",
                    f"{_display_path(path)} must be at most {maximum}; got {value!r}.",
                )

    def _lint_natural_key_duplicates(self) -> None:
        for kind, definitions in self.definitions.items():
            groups: dict[tuple[str, Any], list[Definition]] = defaultdict(list)
            for definition in definitions:
                groups[(definition.layer, definition.key)].append(definition)
            for (layer, key), items in groups.items():
                if len(items) < 2:
                    continue
                related = [item.location for item in items[1:]]
                for item in items:
                    self._diag(
                        item.path,
                        item.name_range,
                        "error",
                        "duplicate-definition",
                        f"Duplicate {kind} definition {item.display!r} in the {layer} layer.",
                        related=[location for location in related if location != item.location],
                    )

    def _resolve(self, reference: Reference) -> tuple[Definition | None, list[Definition]]:
        candidates = [
            definition
            for definition in self.definitions.get(reference.kind, [])
            if definition.key == reference.key
        ]
        if reference.kind == "rack" and isinstance(reference.key, tuple) and not candidates:
            site, name = reference.key
            if site is None:
                candidates = [
                    definition
                    for definition in self.definitions.get("rack", [])
                    if isinstance(definition.key, tuple) and definition.key[1] == name
                ]
        if reference.kind == "vlan" and isinstance(reference.key, tuple) and not candidates:
            site, vid = reference.key
            if site is not None:
                candidates = [
                    definition
                    for definition in self.definitions.get("vlan", [])
                    if definition.key == (None, vid)
                ]
            else:
                candidates = [
                    definition
                    for definition in self.definitions.get("vlan", [])
                    if isinstance(definition.key, tuple) and definition.key[1] == vid
                ]
        if not candidates:
            return None, []
        manual = [definition for definition in candidates if definition.layer == "manual"]
        if manual:
            return manual[0], manual
        return candidates[0], candidates

    def _lint_references(self) -> None:
        for reference in self.references:
            definition, candidates = self._resolve(reference)
            if definition is None:
                if self._reference_allows_missing(reference):
                    continue
                self._diag(
                    reference.path,
                    reference.span,
                    "warning",
                    "unknown-reference",
                    _unknown_reference_message(reference, self.definitions.get(reference.kind, [])),
                )
            elif len(candidates) > 1 and len({candidate.key for candidate in candidates}) > 1:
                self._diag(
                    reference.path,
                    reference.span,
                    "warning",
                    "ambiguous-reference",
                    f"Reference {reference.key!r} matches more than one {reference.kind} definition.",
                    related=[candidate.location for candidate in candidates],
                )

    def _reference_allows_missing(self, reference: Reference) -> bool:
        """Interfaces owned by SNMP are not present in the YAML layer."""

        if reference.kind != "interface":
            return False
        document = self._document(reference.path)
        if document is None or document.root is None:
            return False
        device_name = _device_name(self, document, reference.yaml_path)
        if not device_name:
            return False
        return self._device_interface_names(device_name) is None

    def _device_definitions(self, name: str) -> list[Definition]:
        return [d for d in self.definitions.get("device", []) if d.key == name]

    def _device_field(self, name: str, field_name: str) -> Any:
        for definition in self._device_definitions(name):
            if field_name in definition.value:
                return definition.value[field_name]
        return None

    def _device_interface_names(self, name: str) -> set[str] | None:
        """Combine interfaces from all layers; None means SNMP owns them."""

        names: set[str] = set()
        found = False
        for definition in self._device_definitions(name):
            if _boolean(definition.node, "snmp_interfaces"):
                return None
            interfaces = definition.node.get("interfaces")
            if isinstance(interfaces, YSeq):
                found = True
                for interface in interfaces.items:
                    if isinstance(interface, YMap):
                        interface_name = _text(interface, "name")
                        if interface_name:
                            names.add(interface_name)
        return names if found else set()

    def _lint_racks(self) -> None:
        # Build layout positions and check that every non-empty layout uses the
        # complete rack height.
        for rack in self.definitions.get("rack", []):
            if not isinstance(rack.key, tuple):
                continue
            site, rack_name = rack.key
            u_height = _integer(rack.node, "u_height") or 42
            layout = rack.node.get("layout")
            state = RackState(definition=rack, u_height=u_height)
            self.rack_states[(site, rack_name)] = state
            if not isinstance(layout, YSeq) or not layout.items:
                continue
            cursor = u_height
            seen: set[str] = set()
            for index, entry in enumerate(layout.items):
                if not isinstance(entry, YMap):
                    continue
                gap_node = entry.get("gap") or entry.get("empty")
                if isinstance(gap_node, YScalar) and isinstance(gap_node.value, int):
                    cursor -= gap_node.value
                    if cursor < 0:
                        self._diag(
                            rack.path,
                            entry.span,
                            "error",
                            "rack-layout-overflow",
                            f"Rack {rack_name!r} layout exceeds its {u_height}U height.",
                        )
                    continue
                device_node = entry.get("device")
                if not isinstance(device_node, YScalar) or not isinstance(device_node.value, str):
                    continue
                device_name = device_node.value
                if device_name in seen:
                    self._diag(
                        rack.path,
                        device_node.span,
                        "error",
                        "rack-layout-duplicate-device",
                        f"Device {device_name!r} appears more than once in rack {rack_name!r}.",
                    )
                    continue
                seen.add(device_name)
                device = self._definition("device", device_name)
                if device is None:
                    self._diag(
                        rack.path,
                        device_node.span,
                        "error",
                        "rack-layout-unknown-device",
                        f"Device {device_name!r} is not defined.",
                    )
                    continue
                device_type_slug = self._device_field(device_name, "device_type") or (
                    str(self.defaults.get("device_type"))
                    if isinstance(self.defaults.get("device_type"), str)
                    else None
                )
                device_type = self._definition("device_type", device_type_slug) if device_type_slug else None
                if device_type is None:
                    field_node = device.node.get("device_type") or device_node
                    self._diag(
                        rack.path,
                        device_node.span,
                        "error",
                        "rack-layout-unknown-device-type",
                        f"Device {device_name!r} has no defined device type.",
                        related=[Location(device.path, field_node.span)],
                    )
                    continue
                height = _integer(device_type.node, "u_height")
                if height is None:
                    height = 1
                if height == 0:
                    self._diag(
                        rack.path,
                        device_node.span,
                        "error",
                        "rack-layout-zero-height",
                        f"Device {device_name!r} has u_height 0 and cannot be placed in a rack layout.",
                    )
                    continue
                position = cursor - height + 1
                if position < 1:
                    self._diag(
                        rack.path,
                        entry.span,
                        "error",
                        "rack-layout-does-not-fit",
                        f"Device {device_name!r} ({height}U) does not fit in rack {rack_name!r}.",
                    )
                    continue
                face = _text(entry, "face") or _text(device.node, "face") or "front"
                state.positions[device_name] = (position, position + height - 1, face)
                state.used += height
                for unit in range(position, position + height):
                    state.occupancy[unit] = device_name
                self.device_rack_positions[device_name] = (
                    site,
                    rack_name,
                    position,
                    position + height - 1,
                    face,
                )
                cursor = position - 1
            if cursor != 0:
                used = u_height - cursor
                if used < u_height:
                    message = (
                        f"Rack {rack_name!r} layout uses {used} of {u_height} units; "
                        f"add {u_height - used} empty unit(s)."
                    )
                else:
                    message = (
                        f"Rack {rack_name!r} layout uses {used} of {u_height} units; "
                        f"it overflows by {used - u_height} unit(s)."
                    )
                self._diag(
                    rack.path,
                    layout.span,
                    "error",
                    "rack-layout-incomplete",
                    message,
                )

        # Devices that declare rack/position directly are checked against the
        # same occupancy map. Devices placed by a layout are skipped here.
        for device in self.definitions.get("device", []):
            if not isinstance(device.key, str):
                continue
            if device.key in self.device_rack_positions:
                continue
            rack_name = _text(device.node, "rack")
            position = _integer(device.node, "position")
            face = _text(device.node, "face")
            if not rack_name and (position is not None or face):
                self._diag(
                    device.path,
                    device.node.key_range("position" if position is not None else "face"),
                    "error",
                    "rack-position-without-rack",
                    f"Device {device.key!r} has position/face without a rack.",
                )
                continue
            if not rack_name or position is None:
                continue
            if not face:
                self._diag(
                    device.path,
                    device.node.key_range("position"),
                    "error",
                    "rack-position-without-face",
                    f"Device {device.key!r} has a rack position without a face.",
                )
            site = self._device_field(device.key, "site") or (
                str(self.defaults.get("site")) if isinstance(self.defaults.get("site"), str) else None
            )
            state = self.rack_states.get((site, rack_name))
            if state is None:
                continue
            device_type_slug = self._device_field(device.key, "device_type")
            device_type = self._definition("device_type", device_type_slug) if device_type_slug else None
            height = _integer(device_type.node, "u_height") if device_type else None
            height = height or 1
            end = position + height - 1
            if position < 1 or end > state.u_height:
                self._diag(
                    device.path,
                    device.node.key_range("position"),
                    "error",
                    "rack-position-out-of-bounds",
                    f"Device {device.key!r} ({height}U at {position}) does not fit in rack {rack_name!r} ({state.u_height}U).",
                )
                continue
            overlaps = [
                (unit, occupant)
                for unit, occupant in state.occupancy.items()
                if position <= unit <= end
            ]
            if overlaps:
                occupant = overlaps[0][1]
                related_definition = self._definition("device", occupant)
                self._diag(
                    device.path,
                    device.node.key_range("position"),
                    "error",
                    "rack-position-overlap",
                    f"Device {device.key!r} overlaps {occupant!r} in rack {rack_name!r}.",
                    related=[related_definition.location] if related_definition else [],
                )
            for unit in range(position, end + 1):
                state.occupancy[unit] = device.key
            self.device_rack_positions[device.key] = (site, rack_name, position, end, face or "front")

    def _lint_uplinks_and_cables(self) -> None:
        for document in self.documents:
            root = document.root
            if root is None:
                continue
            devices = root.get("devices")
            if isinstance(devices, YSeq):
                for device in devices.items:
                    if not isinstance(device, YMap):
                        continue
                    device_name = _text(device, "name")
                    if not device_name:
                        continue
                    uplinks = device.get("uplinks")
                    if isinstance(uplinks, YSeq):
                        for uplink in uplinks.items:
                            if not isinstance(uplink, YMap):
                                continue
                            switch_name = _text(uplink, "switch")
                            port = _integer(uplink, "port")
                            if not switch_name or port is None:
                                continue
                            switch = self._definition("device", switch_name)
                            if switch is None:
                                continue
                            switch_interfaces = self._device_interface_names(switch_name)
                            if switch_interfaces is None:
                                continue
                            port_name = f"Port {port}"
                            if port_name not in switch_interfaces:
                                self._diag(
                                    document.path,
                                    uplink.key_range("port"),
                                    "warning",
                                    "unknown-interface",
                                    f"Switch {switch_name!r} has no interface {port_name!r}.",
                                    related=[switch.location],
                                )


    def _lint_module_compatibility(self) -> None:
        for device in self.definitions.get("device", []):
            bays = device.node.get("module_bays")
            modules = device.node.get("modules")
            if not isinstance(bays, YSeq) or not isinstance(modules, YSeq):
                continue
            bay_types: dict[str, set[str]] = {}
            for bay in bays.items:
                if not isinstance(bay, YMap):
                    continue
                name = _text(bay, "name")
                if not name:
                    continue
                types: set[str] = set()
                single = _text(bay, "bay_type")
                if single:
                    types.add(single)
                multiple = bay.get("bay_types")
                if isinstance(multiple, YSeq):
                    types.update(str(item.value) for item in multiple.items if isinstance(item.value, str))
                bay_types[name] = types
            for module in modules.items:
                if not isinstance(module, YMap):
                    continue
                bay_name = _text(module, "bay")
                module_type_slug = _text(module, "module_type")
                if not bay_name or not module_type_slug:
                    continue
                module_type = self._definition("module_type", module_type_slug)
                if module_type is None:
                    continue
                allowed = module_type.value.get("bay_types")
                if not isinstance(allowed, list):
                    continue
                allowed_set = {str(value) for value in allowed if isinstance(value, str)}
                bay_set = bay_types.get(bay_name, set())
                if allowed_set and bay_set and not (allowed_set & bay_set):
                    self._diag(
                        device.path,
                        module.key_range("module_type"),
                        "warning",
                        "incompatible-module",
                        f"Module type {module_type_slug!r} is not compatible with bay {bay_name!r}.",
                        related=[module_type.location],
                    )

    def _lint_images(self) -> None:
        for device_type in self.definitions.get("device_type", []):
            images = device_type.node.get("images")
            if not isinstance(images, YMap):
                continue
            for field_name in ("front", "rear"):
                node = images.get(field_name)
                if (
                    isinstance(node, YScalar)
                    and isinstance(node.value, str)
                    and not self._image_exists(node.value)
                ):
                    self._diag(
                        device_type.path,
                        node.span,
                        "warning",
                        "missing-image",
                        f"Image file {node.value!r} does not exist.",
                    )
            other = images.get("other")
            if isinstance(other, YSeq):
                for attachment in other.items:
                    if not isinstance(attachment, YMap):
                        continue
                    file_node = attachment.get("file")
                    if (
                        isinstance(file_node, YScalar)
                        and isinstance(file_node.value, str)
                        and not self._image_exists(file_node.value)
                    ):
                        self._diag(
                            device_type.path,
                            file_node.span,
                            "warning",
                            "missing-image",
                            f"Image file {file_node.value!r} does not exist.",
                        )

    def _image_exists(self, value: str) -> bool:
        if value.startswith(("http://", "https://")):
            return True
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        return path.is_file()

    # -- lookups ---------------------------------------------------------

    def _definition(self, kind: str, key: Any) -> Definition | None:
        candidates = [d for d in self.definitions.get(kind, []) if d.key == key]
        manual = [d for d in candidates if d.layer == "manual"]
        if manual:
            return manual[0]
        return candidates[0] if candidates else None

    def _definitions_for(self, kind: str, key: Any) -> list[Definition]:
        return [d for d in self.definitions.get(kind, []) if d.key == key]

    def definition_at(self, path: Path, position: Position) -> list[Location]:
        definition = self._definition_at(path, position)
        if definition is not None:
            return [definition.location]
        reference = self._reference_at(path, position)
        if reference is None:
            return []
        resolved, candidates = self._resolve(reference)
        definitions = candidates if len({candidate.key for candidate in candidates}) > 1 else ([resolved] if resolved else [])
        return [definition.location for definition in definitions]

    def _definition_at(self, path: Path, position: Position) -> Definition | None:
        for definitions in self.definitions.values():
            for definition in definitions:
                if definition.name_field is None:
                    continue
                if definition.path == path and _contains(definition.name_range, position):
                    return definition
        return None

    def _reference_at(self, path: Path, position: Position) -> Reference | None:
        for reference in self.references:
            if reference.path == path and _contains(reference.span, position):
                return reference
        return None

    def resolve_at(self, path: Path, position: Position) -> tuple[Definition | None, Reference | None]:
        reference = self._reference_at(path, position)
        if reference is not None:
            resolved, _ = self._resolve(reference)
            return resolved, reference
        definition = self._definition_at(path, position)
        return definition, None

    def hover_at(self, path: Path, position: Position) -> str | None:
        definition, reference = self.resolve_at(path, position)
        if definition is None and reference is None:
            return None
        if definition is None and reference is not None:
            suggestions = difflib.get_close_matches(
                str(reference.key),
                [str(item.key) for item in self.definitions.get(reference.kind, [])],
                n=5,
                cutoff=0.45,
            )
            text = f"**Not defined:** `{reference.key}`\n\nNo `{reference.kind}` definition was found."
            if suggestions:
                text += "\n\nDid you mean: " + ", ".join(f"`{value}`" for value in suggestions)
            return text

        assert definition is not None
        lines = [f"**{definition.kind.replace('_', ' ').title()}** `{definition.display}`"]
        value = definition.value
        if definition.kind == "device_type":
            manufacturer = value.get("manufacturer")
            model = value.get("model")
            lines.append(f"{manufacturer} {model}")
            if "u_height" in value:
                lines.append(f"{value['u_height']}U")
            if value.get("part_number"):
                lines.append(f"Part number: `{value['part_number']}`")
            if value.get("description"):
                lines.append(str(value["description"]))
        elif definition.kind == "module_type":
            lines.append(
                f"{value.get('manufacturer')} {value.get('model')} "
                f"({value.get('profile')})"
            )
            attributes = value.get("attributes")
            if isinstance(attributes, dict) and attributes:
                lines.append(
                    "Attributes: "
                    + ", ".join(f"`{key}={item}`" for key, item in attributes.items())
                )
            bay_types = value.get("bay_types")
            if isinstance(bay_types, list) and bay_types:
                lines.append("Bay types: " + ", ".join(f"`{item}`" for item in bay_types))
        elif definition.kind == "rack":
            site, name = definition.key
            lines.append(f"Site: `{site}`")
            state = self.rack_states.get((site, name))
            if state is not None:
                lines.append(f"Height: {state.u_height}U; placed: {state.used}U")
            if value.get("description"):
                lines.append(str(value["description"]))
        elif definition.kind == "vlan":
            site, vid = definition.key
            lines.append(f"VID {vid} in site `{site or 'global'}`")
            if value.get("description"):
                lines.append(str(value["description"]))
        elif definition.kind in {"site", "manufacturer", "platform", "device_role", "owner", "contact"}:
            if value.get("description"):
                lines.append(str(value["description"]))
            if value.get("manufacturer"):
                lines.append(f"Manufacturer: `{value['manufacturer']}`")
        elif definition.kind == "device":
            role = value.get("role")
            device_type = value.get("device_type")
            site = value.get("site") or self.defaults.get("site")
            lines.append(f"Role: `{role}`, type: `{device_type}`, site: `{site}`")
            position = self.device_rack_positions.get(definition.display)
            if position is not None:
                _, rack_name, start, end, face = position
                lines.append(f"Rack: `{rack_name}` {start}-{end} ({face})")
            if value.get("description"):
                lines.append(str(value["description"]))
        elif definition.kind == "module_bay":
            device, name = definition.key
            lines.append(f"Device: `{device}`, bay: `{name}`")
            if value.get("description"):
                lines.append(str(value["description"]))
        elif definition.kind == "interface":
            device, name = definition.key
            lines.append(f"Device: `{device}`, interface: `{name}`")
            if value.get("description"):
                lines.append(str(value["description"]))
        else:
            if value.get("description"):
                lines.append(str(value["description"]))

        if reference is not None:
            lines.append(f"Referenced from `{reference.field}`.")
        lines.append(f"Defined in `{definition.path.relative_to(self.root)}`.")
        return "\n\n".join(lines)

    def completions_at(self, path: Path, position: Position) -> list[Completion]:
        document = self._document(path)
        if document is None or document.root is None:
            return []
        context_path, node, context = self._context_at(document.root, position)
        if node is None:
            node = _node_at(document.root, context_path)
        lines = document.text.splitlines()
        prefix = lines[position.line][: position.character] if position.line < len(lines) else ""

        if context == "key":
            line_indent = len(prefix) - len(prefix.lstrip())
            if isinstance(node, YMap) and prefix.lstrip().startswith("-"):
                # The cursor is on a new "- " line inside a mapping item. If it
                # is less indented than the item's keys, it is a new item of
                # the parent sequence.
                first_key_indent = min(
                    (entry.key_range.start.character for entry in node.entries),
                    default=line_indent + 1,
                )
                if line_indent < first_key_indent:
                    parent_spec = spec_for_path(context_path[:-1]) or {}
                    item_spec = (
                        parent_spec.get("items", {})
                        if parent_spec.get("kind") == "array"
                        else {}
                    )
                    if item_spec.get("kind") == "object":
                        return self._key_completions(document, item_spec, None)
                    if item_spec:
                        return self._value_completions(
                            document,
                            context_path[:-1] + (0,),
                            item_spec,
                        )
            if isinstance(node, YMap):
                # A new sequence item is outside the sequence's YAML span. If
                # the cursor is indented under the last sequence-valued key,
                # offer item completions instead of the parent object's keys.
                last_sequence = None
                for entry in node.entries:
                    if entry.key_range.start.line < position.line and isinstance(entry.value, YSeq):
                        last_sequence = entry
                if last_sequence is not None:
                    key_indent = last_sequence.key_range.start.character
                    if prefix.lstrip().startswith("-") or line_indent > key_indent:
                        sequence_path = context_path + (last_sequence.key,)
                        spec = spec_for_path(sequence_path) or {}
                        item_spec = spec.get("items", {}) if spec.get("kind") == "array" else {}
                        if item_spec.get("kind") == "object":
                            return self._key_completions(document, item_spec, None)
                        return self._value_completions(
                            document,
                            sequence_path + (len(last_sequence.value.items),),
                            item_spec,
                        )
            if isinstance(node, YSeq):
                spec = spec_for_path(context_path) or {}
                item_spec = spec.get("items", {}) if spec.get("kind") == "array" else {}
                if re.match(r"^\s*-\s*", prefix) or item_spec.get("kind") == "object":
                    return self._key_completions(document, item_spec, None)
                return self._value_completions(document, context_path + (len(node.items),), item_spec)
            spec_path = context_path
            if not isinstance(node, YMap):
                spec_path = context_path[:-1] if context_path else ()
            spec = spec_for_path(spec_path) or TOP_LEVEL_SPEC
            map_node = _node_at(document.root, spec_path)
            if spec.get("kind") == "array":
                spec = spec.get("items", {})
            return self._key_completions(document, spec, map_node if isinstance(map_node, YMap) else None)

        spec = spec_for_path(context_path)
        if spec is None:
            # A scalar list item still uses the field path for reference lookup.
            spec = spec_for_path(context_path[:-1])
        if (
            spec is not None
            and spec.get("kind") == "array"
            and context_path
            and context_path[-1] in {"tagged_vlans", "bay_types"}
        ):
            item_spec = spec.get("items", {})
            return self._value_completions(document, context_path + (0,), item_spec)
        return self._value_completions(document, context_path, spec)

    def _context_at(
        self,
        node: YNode,
        position: Position,
    ) -> tuple[tuple[Any, ...], YNode | None, str]:
        return self._context_node(node, position, ())

    def _context_node(
        self,
        node: YNode,
        position: Position,
        path: tuple[Any, ...],
    ) -> tuple[tuple[Any, ...], YNode | None, str]:
        if isinstance(node, YMap):
            for entry in node.entries:
                if entry.value is not None and _contains(entry.value.span, position):
                    return self._context_node(entry.value, position, path + (entry.key,))
                if entry.key_range is not None and _contains(entry.key_range, position):
                    return path + (entry.key,), entry.value, "key"
            for entry in reversed(node.entries):
                if entry.key_range.start.line == position.line and entry.key_range.start.character <= position.character:
                    return path + (entry.key,), entry.value, "value"
                if entry.key_range.start.line < position.line:
                    return path, node, "key"
            return path, node, "key"
        if isinstance(node, YSeq):
            for index, item in enumerate(node.items):
                if _contains(item.span, position):
                    return self._context_node(item, position, path + (index,))
            return path, node, "key"
        return path, node, "value"

    def _key_completions(
        self,
        document: YamlDocument,
        spec: dict[str, Any],
        node: YNode | None,
    ) -> list[Completion]:
        if spec.get("kind") == "array":
            spec = spec.get("items", {})
        if spec.get("kind") != "object":
            return []
        present = node.present_keys() if isinstance(node, YMap) else set()
        return [
            Completion(
                label=key,
                kind="field",
                detail=_spec_type(field_spec),
                documentation=str(field_spec.get("description", "")),
            )
            for key, field_spec in spec.get("fields", {}).items()
            if key not in present
        ]

    def _value_completions(
        self,
        document: YamlDocument,
        path: tuple[Any, ...],
        spec: dict[str, Any] | None,
    ) -> list[Completion]:
        if spec is None:
            spec = {}
        allowed = spec.get("enum")
        if allowed is not None:
            return [
                Completion(label=str(value), kind="enum", detail="Allowed value")
                for value in allowed
                if str(value) != ""
            ]

        kind = _reference_descriptor(path)
        if kind is not None:
            return self._reference_completions(document, path, kind)

        if any(str(part) == "images" for part in path) and path and path[-1] in {"front", "rear"}:
            return self._image_completions()
        if (
            len(path) >= 3
            and str(path[-3]) == "other"
            and path[-1] == "file"
        ):
            return self._image_completions()

        if (
            len(path) >= 3
            and path[0] == "devices"
            and path[2] == "uplinks"
            and path[-1] == "port"
        ):
            parent = _node_at(document.root, path[:-1])
            if isinstance(parent, YMap):
                switch_name = _text(parent, "switch")
                switch = self._definition("device", switch_name) if switch_name else None
                if switch is not None:
                    interfaces = self._device_interface_names(switch_name) or set()
                    ports: list[Completion] = []
                    for name in sorted(interfaces):
                        match = re.fullmatch(r"Port (\d+)", name)
                        if match:
                            ports.append(
                                Completion(
                                    label=match.group(1),
                                    kind="value",
                                    detail=f"{switch_name} {name}",
                                )
                            )
                    return ports
        return []

    def _reference_completions(
        self,
        document: YamlDocument,
        path: tuple[Any, ...],
        kind: str,
    ) -> list[Completion]:
        site = _effective_site(self, document, path)
        device_name = _device_name(self, document, path)
        definitions = self.definitions.get(kind, [])
        if kind == "rack" and site is not None:
            definitions = [d for d in definitions if isinstance(d.key, tuple) and d.key[0] in {site, None}]
        if kind == "vlan" and site is not None:
            definitions = [
                d
                for d in definitions
                if isinstance(d.key, tuple) and d.key[0] in {site, None}
            ]
        if kind == "module_bay" and device_name is not None:
            definitions = [
                d
                for d in definitions
                if isinstance(d.key, tuple) and d.key[0] == device_name
            ]
        if kind == "interface" and device_name is not None:
            definitions = [
                d
                for d in definitions
                if isinstance(d.key, tuple) and d.key[0] == device_name
            ]
        if kind == "module_bay_type":
            definitions = self.definitions.get("module_bay_type", [])
        if kind == "device_type" and path[:1] == ("unifi_models",):
            definitions = self.definitions.get("device_type", [])

        results: list[Completion] = []
        seen: set[str] = set()
        for definition in definitions:
            label = definition.display
            if label in seen:
                continue
            seen.add(label)
            results.append(
                Completion(
                    label=label,
                    kind="reference",
                    detail=_definition_detail(definition),
                    documentation=str(definition.value.get("description", "")),
                )
            )
        return results

    def _image_completions(self) -> list[Completion]:
        images = self.root / "images"
        if not images.is_dir():
            return []
        return [
            Completion(
                label=f"images/{path.name}",
                kind="file",
                detail="Image file",
            )
            for path in sorted(images.iterdir())
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ]

    def _document(self, path: Path) -> YamlDocument | None:
        for document in self.documents:
            if document.path == path:
                return document
        return None

    # -- rename ----------------------------------------------------------

    def prepare_rename_at(self, path: Path, position: Position) -> RenameInfo | None:
        definition, reference = self.resolve_at(path, position)
        if definition is None:
            return None
        if definition.layer != "manual":
            return None
        if definition.name_field is None:
            return None
        span = reference.span if reference is not None else definition.name_range
        placeholder = (
            str(definition.key[-1]) if isinstance(definition.key, tuple) else str(definition.key)
        )
        return RenameInfo(range=span, placeholder=placeholder)

    def rename_at(self, path: Path, position: Position, new_name: str) -> dict[Path, list[TextEdit]] | None:
        definition, _reference = self.resolve_at(path, position)
        if definition is None or definition.layer != "manual" or definition.name_field is None:
            return None

        new_key: Any
        new_text = new_name
        if definition.kind in SLUG_KINDS:
            if not SLUG_RE.fullmatch(new_name):
                return None
            new_key = new_name
        elif definition.kind in NAME_KINDS:
            if not new_name.strip():
                return None
            new_key = new_name
        elif definition.kind in VID_KINDS:
            try:
                vid = int(new_name)
            except ValueError:
                return None
            if not 1 <= vid <= 4094:
                return None
            new_key = (definition.key[0], vid)
            new_text = str(vid)
        else:
            return None

        if definition.kind in {"rack", "module_bay", "interface"}:
            new_key = (definition.key[0], new_key)

        if new_key == definition.key:
            return {definition.path: [TextEdit(definition.name_range, new_text)]}

        for other in self.definitions.get(definition.kind, []):
            if other.key == new_key and other.key != definition.key:
                return None

        changes: dict[Path, list[TextEdit]] = defaultdict(list)
        for other in self.definitions.get(definition.kind, []):
            if other.key == definition.key and other.name_field is not None:
                changes[other.path].append(TextEdit(other.name_range, new_text))
        for reference in self.references:
            if not reference.allow_rename or reference.kind != definition.kind:
                continue
            if _reference_matches(reference, definition):
                reference_text = str(new_key[-1]) if isinstance(new_key, tuple) else str(new_key)
                changes[reference.path].append(TextEdit(reference.span, reference_text))
        return dict(changes)


# ---------------------------------------------------------------------------
# Helpers used by the index


def _display_path(path: tuple[Any, ...]) -> str:
    if not path:
        return "inventory document"
    return "/".join(str(part) for part in path)


def _spec_type(spec: dict[str, Any]) -> str:
    kind = spec.get("kind", "any")
    if kind == "array":
        return f"list[{_spec_type(spec.get('items', {}))}]"
    if kind == "object":
        return "mapping"
    if kind == "map":
        return "mapping"
    return str(kind)


def _definition_detail(definition: Definition) -> str:
    value = definition.value
    if definition.kind == "device_type":
        return f"{value.get('manufacturer')} {value.get('model')} ({value.get('u_height', 1)}U)"
    if definition.kind == "module_type":
        return f"{value.get('manufacturer')} {value.get('model')} ({value.get('profile')})"
    if definition.kind == "rack":
        return f"{definition.key[0] or 'global'}/{definition.key[1]}"
    if definition.kind == "vlan":
        return f"{definition.key[1]} {value.get('name', '')}".strip()
    if definition.kind == "device":
        return f"{value.get('role', '')} / {value.get('device_type', '')}".strip(" /")
    if definition.kind == "module_bay":
        return f"{definition.key[0]} / {definition.key[1]}"
    if definition.kind == "interface":
        return f"{definition.key[0]} / {definition.key[1]}"
    return str(value.get("description", ""))


def _unknown_reference_message(reference: Reference, definitions: list[Definition]) -> str:
    suggestions = difflib.get_close_matches(
        str(reference.key),
        [str(definition.key) for definition in definitions],
        n=3,
        cutoff=0.45,
    )
    message = f"Unknown {reference.kind.replace('_', ' ')} {reference.key!r}."
    if suggestions:
        message += " Did you mean " + ", ".join(repr(value) for value in suggestions) + "?"
    return message


def _reference_matches(reference: Reference, definition: Definition) -> bool:
    if reference.key == definition.key:
        return True
    if definition.kind == "rack" and isinstance(reference.key, tuple) and isinstance(definition.key, tuple):
        return reference.key[0] is None and reference.key[1] == definition.key[1]
    return False


