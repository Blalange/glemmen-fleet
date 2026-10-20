"""Shared helpers for the glemmen-fleet management scripts.

Stdlib only for the helpers themselves; scripts that parse machine configs
import PyYAML through require_yaml().
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent  # repository/scripts
REPO_ROOT = SCRIPT_DIR.parent
TALOS_DIR = REPO_ROOT / "talos"
FLUX_DIR = REPO_ROOT / "flux"
GENERATED_DIR = TALOS_DIR / "generated"
SECRETS_DIR = TALOS_DIR / "secrets"
PATCHES_DIR = TALOS_DIR / "patches"
NODES_DIR = TALOS_DIR / "nodes"
TOOLS_DIR = SCRIPT_DIR / "bin"
TALOS_SETTINGS = TALOS_DIR / "settings.env"
TALOS_SCRIPTS_DIR = SCRIPT_DIR / "talos"
FLUX_SCRIPTS_DIR = SCRIPT_DIR / "flux"

FACTORY_URL = os.environ.get("TALOS_FACTORY_URL", "https://factory.talos.dev").rstrip("/")
DEFAULT_FLUX_VERSION = "v2.9.6"

# Behave like a normal Unix CLI when the consumer of our output goes away
# (e.g. `manage.py status | head`), instead of raising BrokenPipeError.
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


@dataclass(frozen=True)
class Node:
    name: str
    address: str


def die(message: str, code: int = 1) -> None:
    sys.stdout.flush()
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def warn(message: str) -> None:
    sys.stdout.flush()
    print(f"WARNING: {message}", file=sys.stderr)


def load_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE lines (shell-style quoting and comments)."""
    if not path.is_file():
        die(f"{path} not found")

    values: dict[str, str] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            warn(f"{path}:{line_no}: ignoring line without '='")
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            warn(f"{path}:{line_no}: ignoring invalid variable name {key!r}")
            continue

        try:
            tokens = shlex.split(value, comments=True, posix=True)
        except ValueError as exc:
            die(f"{path}:{line_no}: cannot parse value: {exc}")

        values[key] = " ".join(tokens)

    return values


def load_talos_settings() -> dict[str, str]:
    return load_env_file(TALOS_SETTINGS)


def parse_node_table(raw: str) -> list[Node]:
    try:
        entries = shlex.split(raw)
    except ValueError as exc:
        die(f"cannot parse TALOS_NODES: {exc}")

    nodes: list[Node] = []
    seen: set[str] = set()
    for entry in entries:
        name, separator, address = entry.partition("=")
        if not separator:
            address = name
        if not name or not address:
            die(f"invalid TALOS_NODES entry: {entry!r} (expected name=address)")
        if name in seen:
            die(f"duplicate node name in TALOS_NODES: {name}")
        seen.add(name)
        nodes.append(Node(name=name, address=address))

    if not nodes:
        die("TALOS_NODES is empty in talos/settings.env")

    return nodes


def worker_names(settings: dict[str, str]) -> set[str]:
    try:
        names = set(shlex.split(settings.get("TALOS_WORKERS", "")))
    except ValueError as exc:
        die(f"cannot parse TALOS_WORKERS: {exc}")

    if names:
        known = {node.name for node in parse_node_table(settings.get("TALOS_NODES", ""))}
        unknown = sorted(names - known)
        if unknown:
            die(f"TALOS_WORKERS contains unknown node(s): {', '.join(unknown)}")
    return names


def control_plane_nodes(nodes: list[Node], workers: set[str]) -> list[Node]:
    control_planes = [node for node in nodes if node.name not in workers]
    if not control_planes:
        die("no control-plane nodes found (TALOS_WORKERS covers every node)")
    return control_planes


def bootstrap_node(settings: dict[str, str], nodes: list[Node]) -> Node:
    raw = settings.get("BOOTSTRAP_NODE", "").strip()
    if not raw:
        return nodes[0]

    name, separator, address = raw.partition("=")
    if separator:
        target_name, target_address = name, address
    else:
        target_name, target_address = raw, ""

    for node in nodes:
        if target_name in (node.name, node.address):
            return Node(node.name, target_address or node.address)
    return Node(target_name, target_address or target_name)


def node_by_ref(nodes: list[Node], ref: str) -> Node | None:
    for node in nodes:
        if ref == node.name or ref == node.address:
            return node
    return None


def expand_refs(refs: list[str] | None) -> list[str]:
    expanded: list[str] = []
    for ref in refs or []:
        for part in ref.split(","):
            part = part.strip()
            if part:
                expanded.append(part)
    return expanded


def resolve_node(nodes: list[Node], ref: str, address: str | None = None) -> Node:
    node = node_by_ref(nodes, ref)
    if node is not None:
        return Node(node.name, address or node.address)
    return Node(ref, address or ref)


def resolve_nodes(
    nodes: list[Node], refs: list[str] | None, default: list[Node] | None = None
) -> list[Node]:
    expanded = expand_refs(refs)
    if not expanded:
        return list(default or [])
    return [resolve_node(nodes, ref) for ref in expanded]


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    if not env.get("TALOSCONFIG"):
        env["TALOSCONFIG"] = str(GENERATED_DIR / "talosconfig")
    if not env.get("KUBECONFIG"):
        env["KUBECONFIG"] = str(GENERATED_DIR / "kubeconfig")
    return env


def run(
    cmd: list[str | Path],
    *,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in cmd]
    full_env = subprocess_env()
    if env:
        full_env.update(env)

    sys.stdout.flush()
    sys.stderr.flush()
    try:
        result = subprocess.run(
            argv,
            env=full_env,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )
    except FileNotFoundError:
        die(f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        message = f"command timed out after {timeout}s: {' '.join(argv)}"
        if check:
            die(message)
        return subprocess.CompletedProcess(argv, 124, "", message)

    if result.returncode != 0 and check:
        if result.returncode in (-signal.SIGPIPE, 128 + signal.SIGPIPE):
            raise SystemExit(0)
        sys.stdout.flush()
        if capture and result.stderr and not quiet:
            print(result.stderr.rstrip(), file=sys.stderr)
        if not quiet:
            print(
                f"ERROR: command failed with exit code {result.returncode}: "
                f"{' '.join(argv)}",
                file=sys.stderr,
            )
        raise SystemExit(result.returncode)

    return result


def find_tool(name: str, env_var: str | None = None) -> str | None:
    if env_var:
        override = os.environ.get(env_var)
        if override:
            candidate = Path(override)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            found = shutil.which(override)
            if found:
                return found
            die(f"{env_var}={override} is not an executable")

    local = TOOLS_DIR / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)

    return shutil.which(name)


def require_tool(name: str, env_var: str | None = None) -> str:
    path = find_tool(name, env_var)
    if path:
        return path
    hint = f" or set {env_var}" if env_var else ""
    die(f"{name} not found; run ./scripts/download-tools.py {name}{hint}")


def require_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(
            "ERROR: PyYAML is required (install with: python3 -m pip install pyyaml)"
        ) from exc
    return yaml


def http_get_json(
    url: str, *, headers: dict[str, str] | None = None, timeout: float = 30
) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "glemmen-fleet-scripts", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def create_schematic(payload: dict) -> str:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{FACTORY_URL}/schematics",
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "glemmen-fleet-scripts",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        die(f"failed to create schematic at {FACTORY_URL}: {exc}")

    schematic_id = result.get("id")
    if not schematic_id:
        die(f"unexpected schematic response: {result!r}")
    return str(schematic_id)


def create_schematic_from_file(path: Path) -> str:
    yaml = require_yaml()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"failed to read {path}: {exc}")
    if not isinstance(payload, dict):
        die(f"{path} does not contain a YAML mapping")
    return create_schematic(payload)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def download_file(url: str, destination: Path, *, timeout: float = 300) -> Path:
    """Download url to destination, replacing an existing file."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(
        url, headers={"User-Agent": "glemmen-fleet-scripts"}
    )
    temporary: Path | None = None
    completed = False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
            )
            temporary = Path(temporary_name)
            downloaded = 0
            last_report = 0

            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if downloaded - last_report >= 8 * 1024 * 1024:
                        _print_progress(downloaded, total)
                        last_report = downloaded
                _print_progress(downloaded, total, final=True)

        os.replace(temporary, destination)
        completed = True
        return destination
    except (urllib.error.URLError, OSError) as exc:
        die(f"failed to download {url}: {exc}")
    finally:
        if not completed and temporary is not None:
            temporary.unlink(missing_ok=True)


def _print_progress(downloaded: int, total: int, *, final: bool = False) -> None:
    if total:
        percent = min(100, downloaded * 100 // total)
        text = f"{percent:3d}% ({human_size(downloaded)} / {human_size(total)})"
    else:
        text = human_size(downloaded)
    print(f"\r    {text}", end="\n" if final else "", flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
