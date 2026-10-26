"""Fetch device and module type definitions from the NetBox Device Type Library.

Definitions and elevation images are read from the community DTL repository at
sync time and cached outside Git. The inventory only stores an optional
``dtl: <path>`` reference; no DTL data is committed to this repository.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml

DEFAULT_REPO = "netbox-community/devicetype-library"
DEFAULT_REF = "master"
USER_AGENT = "glemmen-fleet-netbox-sync/1.0"


class DtlError(Exception):
    """A Device Type Library lookup or download failed."""


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


class DtlCatalog:
    def __init__(
        self,
        *,
        repo: str = DEFAULT_REPO,
        ref: str = DEFAULT_REF,
        timeout: float = 15.0,
        cache_dir: Path | str | None = None,
        cache_ttl: float = 24 * 60 * 60,
        auto_lookup: bool = True,
        auto_module_types: bool = False,
        token: str | None = None,
    ) -> None:
        self.repo = repo.strip("/")
        self.ref = ref
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.auto_lookup = auto_lookup
        self.auto_module_types = auto_module_types
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._definition_cache: dict[str, dict[str, Any] | None] = {}
        self._tree_cache: list[str] | None = None

    @classmethod
    def from_env(cls) -> "DtlCatalog":
        return cls(
            repo=os.environ.get("DTL_REPO", DEFAULT_REPO),
            ref=os.environ.get("DTL_REF", DEFAULT_REF),
            timeout=float(os.environ.get("DTL_TIMEOUT", "15")),
            cache_dir=os.environ.get("DTL_CACHE_DIR") or "/tmp/dtl-cache",
            cache_ttl=float(os.environ.get("DTL_CACHE_TTL_HOURS", "24")) * 60 * 60,
            auto_lookup=os.environ.get("DTL_AUTO_LOOKUP", "true").lower()
            in {"1", "true", "yes", "on"},
            auto_module_types=os.environ.get("DTL_AUTO_MODULE_TYPES", "true").lower()
            in {"1", "true", "yes", "on"},
            token=os.environ.get("DTL_TOKEN") or os.environ.get("GITHUB_TOKEN"),
        )

    def _fresh(self, path: Path | None) -> bool:
        if path is None or not path.is_file():
            return False
        if self.cache_ttl <= 0:
            return True
        return time.time() - path.stat().st_mtime <= self.cache_ttl

    def _definition_path(self, path: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / "definitions" / path

    def _definition_cache_path(self, path: str) -> Path | None:
        return self._definition_path(path)

    def _read_definition(self, path: str, *, fresh_only: bool = True) -> dict[str, Any] | None:
        cache_path = self._definition_cache_path(path)
        if cache_path is None or not cache_path.is_file():
            return None
        if fresh_only and not self._fresh(cache_path):
            return None
        try:
            data = yaml.safe_load(cache_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        return data if isinstance(data, dict) else None

    def _write_definition(self, path: str, data: dict[str, Any]) -> None:
        cache_path = self._definition_cache_path(path)
        if cache_path is None:
            return
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        except OSError as exc:
            print(f"WARNING: DTL cache {cache_path}: {exc}", file=sys.stderr)

    def _raw_url(self, path: str) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.ref}/{quote(path, safe='/')}"

    def fetch_definition(self, path: str) -> dict[str, Any] | None:
        path = str(path or "").strip().lstrip("/")
        if not path:
            return None
        if path in self._definition_cache:
            return self._definition_cache[path]
        cached = self._read_definition(path)
        if cached is not None:
            self._definition_cache[path] = cached
            return cached
        try:
            response = self.session.get(self._raw_url(path), timeout=self.timeout)
        except requests.RequestException as exc:
            stale = self._read_definition(path, fresh_only=False)
            if stale is not None:
                print(f"WARNING: DTL {path} refresh failed; using stale cache: {exc}", file=sys.stderr)
                self._definition_cache[path] = stale
                return stale
            raise DtlError(f"fetch DTL {path}: {exc}") from exc
        if response.status_code == 404:
            self._definition_cache[path] = None
            return None
        if response.status_code != 200:
            stale = self._read_definition(path, fresh_only=False)
            if stale is not None:
                print(f"WARNING: DTL {path} HTTP {response.status_code}; using stale cache", file=sys.stderr)
                self._definition_cache[path] = stale
                return stale
            raise DtlError(f"fetch DTL {path}: HTTP {response.status_code}")
        try:
            data = yaml.safe_load(response.text)
        except yaml.YAMLError as exc:
            raise DtlError(f"parse DTL {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise DtlError(f"parse DTL {path}: expected a mapping")
        self._definition_cache[path] = data
        self._write_definition(path, data)
        return data

    def _tree_path(self) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / "tree.json"

    def _read_tree(self, *, fresh_only: bool = True) -> list[str] | None:
        path = self._tree_path()
        if path is None or not path.is_file():
            return None
        if fresh_only and not self._fresh(path):
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, list):
            return None
        return [str(item) for item in payload if isinstance(item, str)]

    def tree(self) -> list[str]:
        if self._tree_cache is not None:
            return self._tree_cache
        cached = self._read_tree()
        if cached is not None:
            self._tree_cache = cached
            return cached
        url = f"https://api.github.com/repos/{self.repo}/git/trees/{quote(self.ref, safe='')}?recursive=1"
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            stale = self._read_tree(fresh_only=False)
            if stale is not None:
                print(f"WARNING: DTL tree refresh failed; using stale cache: {exc}", file=sys.stderr)
                self._tree_cache = stale
                return stale
            raise DtlError(f"fetch DTL tree: {exc}") from exc
        if response.status_code != 200:
            stale = self._read_tree(fresh_only=False)
            if stale is not None:
                print(
                    f"WARNING: DTL tree HTTP {response.status_code}; using stale cache",
                    file=sys.stderr,
                )
                self._tree_cache = stale
                return stale
            raise DtlError(f"fetch DTL tree: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DtlError(f"parse DTL tree: {exc}") from exc
        paths = [
            str(item.get("path"))
            for item in payload.get("tree", [])
            if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")
        ]
        path = self._tree_path()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(paths), encoding="utf-8")
            except OSError as exc:
                print(f"WARNING: DTL tree cache {path}: {exc}", file=sys.stderr)
        self._tree_cache = paths
        return paths

    def find_definition(
        self,
        item: dict[str, Any],
        *,
        kind: str,
        manufacturer_name: str = "",
    ) -> dict[str, Any] | None:
        reference = item.get("dtl")
        if isinstance(reference, str) and reference.strip():
            return self.fetch_definition(reference)
        if reference is False or not self.auto_lookup:
            return None
        if kind == "module-type" and not self.auto_module_types:
            return None
        model = str(item.get("model") or item.get("name") or "")
        slug = str(item.get("slug") or "")
        wanted = {_normalize(model), _normalize(slug)}
        wanted.discard("")
        prefix = "device-types" if kind == "device-type" else "module-types"
        candidates: list[str] = []
        try:
            paths = self.tree()
        except DtlError:
            return None
        for path in paths:
            if not path.startswith(prefix + "/") or not path.endswith((".yaml", ".yml")):
                continue
            parts = path.split("/")
            if len(parts) < 3:
                continue
            if manufacturer_name and _normalize(parts[1]) != _normalize(manufacturer_name):
                continue
            stem = parts[-1].rsplit(".", 1)[0]
            if _normalize(stem) in wanted:
                candidates.append(path)
        if len(candidates) != 1:
            return None
        return self.fetch_definition(candidates[0])

    def resolve(
        self,
        item: dict[str, Any],
        *,
        kind: str,
        manufacturer_name: str = "",
    ) -> dict[str, Any] | None:
        return self.find_definition(
            item,
            kind=kind,
            manufacturer_name=manufacturer_name,
        )

    def image_path(self, definition: dict[str, Any], face: str) -> Path | None:
        if face not in {"front", "rear"} or not definition.get(f"{face}_image"):
            return None
        manufacturer = str(definition.get("manufacturer") or "")
        slug = str(definition.get("slug") or "")
        if not manufacturer or not slug:
            return None
        for extension in ("png", "jpg", "jpeg"):
            path = f"elevation-images/{manufacturer}/{slug}.{face}.{extension}"
            cache_path = (
                self.cache_dir / "images" / f"{slug}.{face}.{extension}"
                if self.cache_dir
                else None
            )
            if cache_path is not None and self._fresh(cache_path):
                return cache_path
            try:
                response = self.session.get(self._raw_url(path), timeout=self.timeout)
            except requests.RequestException:
                if cache_path is not None and cache_path.is_file():
                    return cache_path
                continue
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                if cache_path is not None and cache_path.is_file():
                    return cache_path
                continue
            if cache_path is not None:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(response.content)
                except OSError as exc:
                    print(f"WARNING: DTL image cache {cache_path}: {exc}", file=sys.stderr)
                return cache_path
            # No cache configured: caller cannot use a path; keep the bytes out
            # of the inventory and skip the image.
            return None
        return None
