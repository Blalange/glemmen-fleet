"""Validate the inventory YAML files against the editor schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "inventory.schema.json"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def inventory_files() -> list[Path]:
    files: set[Path] = set()
    for directory in (ROOT / "manual", ROOT / "generated"):
        files.update(directory.rglob("*.yaml"))
        files.update(directory.rglob("*.yml"))
    return sorted(files)


def test_inventory_schema_is_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(load_schema())


def test_inventory_yaml_matches_schema() -> None:
    validator = jsonschema.Draft202012Validator(load_schema())
    errors: list[str] = []
    for path in inventory_files():
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if document is None:
                continue
            for error in validator.iter_errors(document):
                errors.append(
                    f"{path.relative_to(ROOT)}: {error.json_path}: {error.message}"
                )
    assert not errors, "\n".join(errors)
