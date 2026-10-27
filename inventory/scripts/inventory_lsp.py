#!/usr/bin/env python3
"""Language server for the Glemmen inventory YAML files.

Run it over stdio from the thin VS Code client:

    uv run --with "pygls>=2.1,<3" --with lsprotocol --with PyYAML \
      python inventory/scripts/inventory_lsp.py

The server provides semantic diagnostics, go-to-definition, hover,
completion and rename for ``inventory/manual`` and ``inventory/generated``.
Field shape validation and value completion are driven by
``inventory_fields.py``; cross-layer references and rack rules are driven by
``inventory_index.py``.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from inventory_index import (
    Completion,
    Diagnostic,
    InventoryIndex,
    Position,
    Range,
    YamlDocument,
    _parse_file,
)
from inventory_keys import inventory_files
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import from_fs_path, to_fs_path

SERVER = LanguageServer("glemmen-inventory", "0.1.0")
INDEX: InventoryIndex | None = None
OPEN_DOCUMENTS: dict[Path, str] = {}
PARSED_CACHE: dict[Path, tuple[str, list[YamlDocument]]] = {}
CLI_INVENTORY: Path | None = None
SOURCE_NAME = "glemmen-inventory"


def _lsp_position(position: Position) -> types.Position:
    return types.Position(line=position.line, character=position.character)


def _lsp_range(span: Range) -> types.Range:
    return types.Range(start=_lsp_position(span.start), end=_lsp_position(span.end))


def _position_from_lsp(position: types.Position) -> Position:
    return Position(line=position.line, character=position.character)


def _uri(path: Path) -> str:
    return from_fs_path(str(path)) or str(path)


def _path_from_uri(uri: str) -> Path | None:
    value = to_fs_path(uri)
    if value is None:
        return None
    return Path(value).resolve()


def _document_path(uri: str) -> Path | None:
    return _path_from_uri(uri)


def _find_inventory_root() -> Path:
    if CLI_INVENTORY is not None:
        return CLI_INVENTORY
    workspace = SERVER.workspace.root_path or os.getcwd()
    root = Path(workspace).resolve()
    nested = root / "inventory"
    if (nested / "manual").is_dir() and (nested / "generated").is_dir():
        return nested
    if (root / "manual").is_dir() and (root / "generated").is_dir():
        return root
    return nested


def _to_lsp_diagnostic(diagnostic: Diagnostic) -> types.Diagnostic:
    severity = (
        types.DiagnosticSeverity.Error
        if diagnostic.severity == "error"
        else types.DiagnosticSeverity.Warning
    )
    related = [
        types.DiagnosticRelatedInformation(
            location=types.Location(uri=_uri(location.path), range=_lsp_range(location.range)),
            message="Related definition",
        )
        for location in diagnostic.related
    ]
    return types.Diagnostic(
        range=_lsp_range(diagnostic.range),
        message=diagnostic.message,
        severity=severity,
        code=diagnostic.code,
        source=SOURCE_NAME,
        related_information=related or None,
    )


def _diagnostics_for(path: Path) -> list[types.Diagnostic]:
    if INDEX is None:
        return []
    return [
        _to_lsp_diagnostic(diagnostic)
        for diagnostic in INDEX.diagnostics
        if diagnostic.path == path
    ]


def _publish(uri: str) -> None:
    path = _document_path(uri)
    if path is None:
        return
    SERVER.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=_diagnostics_for(path))
    )


def _publish_all() -> None:
    for path in list(OPEN_DOCUMENTS):
        _publish(_uri(path))


def _parsed_documents(root: Path) -> list[YamlDocument]:
    """Parse only changed files; reuse parsed documents for the rest."""

    documents: list[YamlDocument] = []
    files = inventory_files(root)
    current = set(files)
    for path in files:
        text = OPEN_DOCUMENTS.get(path)
        if text is None:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                documents.append(
                    YamlDocument(
                        path=path,
                        text="",
                        root=None,
                        parse_error=(str(exc), Range(Position(0, 0), Position(0, 0))),
                    )
                )
                continue
        cached = PARSED_CACHE.get(path)
        if cached is not None and cached[0] == text:
            documents.extend(cached[1])
            continue
        parsed = _parse_file(path, text)
        PARSED_CACHE[path] = (text, parsed)
        documents.extend(parsed)
    for path in list(PARSED_CACHE):
        if path not in current:
            PARSED_CACHE.pop(path, None)
    return documents


def _rebuild() -> None:
    global INDEX
    try:
        root = _find_inventory_root()
        INDEX = InventoryIndex(
            root,
            overrides=OPEN_DOCUMENTS,
            parsed_documents=_parsed_documents(root),
        )
    except Exception as exc:  # noqa: BLE001 - keep the editor alive
        INDEX = None
        SERVER.window_show_message(
            types.ShowMessageParams(
                type=types.MessageType.Error,
                message=f"Glemmen inventory: failed to build index: {exc}",
            )
        )
        return
    _publish_all()


def _completion_kind(completion: Completion) -> types.CompletionItemKind:
    return {
        "field": types.CompletionItemKind.Field,
        "enum": types.CompletionItemKind.EnumMember,
        "reference": types.CompletionItemKind.Reference,
        "file": types.CompletionItemKind.File,
        "value": types.CompletionItemKind.Value,
        "suggestion": types.CompletionItemKind.Value,
        "boolean": types.CompletionItemKind.Value,
    }.get(completion.kind, types.CompletionItemKind.Text)


@SERVER.feature(types.INITIALIZED)
def initialized(ls: LanguageServer, params: types.InitializedParams) -> None:
    _rebuild()


@SERVER.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams) -> None:
    path = _document_path(params.text_document.uri)
    if path is None:
        return
    document = SERVER.workspace.get_text_document(params.text_document.uri)
    OPEN_DOCUMENTS[path] = document.source
    _rebuild()


@SERVER.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams) -> None:
    path = _document_path(params.text_document.uri)
    if path is None:
        return
    document = SERVER.workspace.get_text_document(params.text_document.uri)
    OPEN_DOCUMENTS[path] = document.source
    _rebuild()


@SERVER.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams) -> None:
    path = _document_path(params.text_document.uri)
    if path is None:
        return
    document = SERVER.workspace.get_text_document(params.text_document.uri)
    OPEN_DOCUMENTS[path] = document.source
    _rebuild()


@SERVER.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams) -> None:
    path = _document_path(params.text_document.uri)
    if path is None:
        return
    OPEN_DOCUMENTS.pop(path, None)
    SERVER.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
    )
    _rebuild()


@SERVER.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def did_change_watched_files(
    ls: LanguageServer,
    params: types.DidChangeWatchedFilesParams,
) -> None:
    _rebuild()


@SERVER.feature(types.TEXT_DOCUMENT_DEFINITION)
def definition(
    ls: LanguageServer,
    params: types.DefinitionParams,
) -> list[types.Location] | None:
    path = _document_path(params.text_document.uri)
    if path is None or INDEX is None:
        return None
    locations = INDEX.definition_at(path, _position_from_lsp(params.position))
    if not locations:
        return None
    return [
        types.Location(uri=_uri(location.path), range=_lsp_range(location.range))
        for location in locations
    ]


@SERVER.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams) -> types.Hover | None:
    path = _document_path(params.text_document.uri)
    if path is None or INDEX is None:
        return None
    text = INDEX.hover_at(path, _position_from_lsp(params.position))
    if text is None:
        return None
    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=text,
        )
    )


@SERVER.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=[":", "-", " ", "/", "."]),
)
def completion(
    ls: LanguageServer,
    params: types.CompletionParams,
) -> types.CompletionList | None:
    path = _document_path(params.text_document.uri)
    if path is None or INDEX is None:
        return None
    completions = INDEX.completions_at(path, _position_from_lsp(params.position))
    items = [
        types.CompletionItem(
            label=item.label,
            kind=_completion_kind(item),
            detail=item.detail or None,
            documentation=(
                types.MarkupContent(kind=types.MarkupKind.Markdown, value=item.documentation)
                if item.documentation
                else None
            ),
            insert_text=item.label,
            sort_text=item.label,
        )
        for item in completions
    ]
    return types.CompletionList(is_incomplete=False, items=items)


@SERVER.feature(types.TEXT_DOCUMENT_PREPARE_RENAME)
def prepare_rename(
    ls: LanguageServer,
    params: types.PrepareRenameParams,
) -> types.PrepareRenamePlaceholder | None:
    path = _document_path(params.text_document.uri)
    if path is None or INDEX is None:
        return None
    info = INDEX.prepare_rename_at(path, _position_from_lsp(params.position))
    if info is None:
        return None
    return types.PrepareRenamePlaceholder(
        range=_lsp_range(info.range),
        placeholder=info.placeholder,
    )


@SERVER.feature(types.TEXT_DOCUMENT_RENAME)
def rename(ls: LanguageServer, params: types.RenameParams) -> types.WorkspaceEdit | None:
    path = _document_path(params.text_document.uri)
    if path is None or INDEX is None:
        return None
    changes = INDEX.rename_at(
        path,
        _position_from_lsp(params.position),
        params.new_name,
    )
    if changes is None:
        return None
    return types.WorkspaceEdit(
        changes={
            _uri(target): [
                types.TextEdit(range=_lsp_range(edit.range), new_text=edit.new_text)
                for edit in edits
            ]
            for target, edits in changes.items()
        }
    )


def main(argv: list[str] | None = None) -> int:
    global CLI_INVENTORY
    parser = argparse.ArgumentParser(description="Glemmen inventory language server")
    parser.add_argument(
        "--inventory",
        default=os.environ.get("NETBOX_INVENTORY_DIR"),
        help="inventory directory (default: auto-detect <workspace>/inventory)",
    )
    args, _unknown = parser.parse_known_args(argv)
    if args.inventory:
        CLI_INVENTORY = Path(args.inventory).resolve()
    SERVER.start_io()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
