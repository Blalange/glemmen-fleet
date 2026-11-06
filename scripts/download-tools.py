#!/usr/bin/env python3
"""Download the pinned command-line tools into scripts/bin/.

Tools:
  talosctl    version from talos/settings.env (TALOS_VERSION)
  kubectl     version from talos/settings.env (KUBERNETES_VERSION)
  flux        version from FLUX_VERSION or the built-in default
  pv-migrate  version from PV_MIGRATE_VERSION or the built-in default

By default the script downloads the binary for the host operating system
and CPU architecture (linux, darwin, windows or freebsd on amd64, arm64,
arm, etc.) and adds a ``.exe`` suffix on Windows. A different target
platform can be requested with ``--platform``/``--os``/``--arch`` (or the
``DOWNLOAD_TOOLS_*`` / ``GOOS``/``GOARCH`` environment variables); those
binaries are stored under ``scripts/bin/<os>_<arch>/`` so they do not
overwrite the host binaries.

Existing binaries are compared against the pinned versions: the host
binary is executed (``talosctl version --client``, ``kubectl version
--client``, ``flux version --client`` or ``pv-migrate --version``) and
re-downloaded when its version differs. Versions downloaded for foreign
platforms, which cannot be executed locally, are recorded in
``scripts/bin/.tool-versions.json`` and compared against that record.
``--force`` re-downloads unconditionally; ``--check`` only reports.

Usage:
  ./scripts/download-tools.py                    # download/update all tools
  ./scripts/download-tools.py talosctl           # only talosctl
  ./scripts/download-tools.py pv-migrate         # only pv-migrate
  ./scripts/download-tools.py flux --force       # re-download flux
  ./scripts/download-tools.py --check            # report, exit 1 if stale
  ./scripts/download-tools.py --platform linux/amd64
  ./scripts/download-tools.py --os darwin --arch arm64
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

TOOLS = ("talosctl", "kubectl", "flux", "pv-migrate")
VERSION_MANIFEST = ".tool-versions.json"

_OS_ALIASES: dict[str, str] = {
    "linux": "linux",
    "darwin": "darwin",
    "macos": "darwin",
    "mac": "darwin",
    "osx": "darwin",
    "windows": "windows",
    "win32": "windows",
    "win": "windows",
    "freebsd": "freebsd",
}

_ARCH_ALIASES: dict[str, str] = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "x64": "amd64",
    "em64t": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "arm": "arm",
    "armv7": "arm",
    "armv7l": "arm",
    "armv8l": "arm",
    "armv6": "arm",
    "armv6l": "arm",
    "armhf": "arm",
    "armel": "arm",
    "386": "386",
    "x86": "386",
    "i386": "386",
    "i686": "386",
    "riscv64": "riscv64",
    "ppc64le": "ppc64le",
    "ppc64": "ppc64le",
    "s390x": "s390x",
}

_VERSION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r'GitVersion["\']?\s*[:=]\s*["\']?'
        r"(v?\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r'"flux"["\']?\s*[:=]\s*["\']?'
        r"(v?\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*flux\s*:\s*[\"']?"
        r"(v?\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?)",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"Client Version:\s*[\"']?"
        r"(v?\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*Tag\s*:\s*[\"']?"
        r"(v?\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?)",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"\bversion\s+(v\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?)",
        re.IGNORECASE,
    ),
]
_GENERIC_VERSION_RE = re.compile(r"v\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?")
_GENERIC_VERSION_RE_NO_PREFIX = re.compile(r"\d+(?:\.\d+){2,}(?:[-+][0-9A-Za-z.-]+)?")

_PROBE_COMMANDS: dict[str, list[list[str]]] = {
    "talosctl": [
        ["version", "--client"],
        ["version", "--client", "--short"],
    ],
    "kubectl": [
        ["version", "--client", "-o", "json"],
        ["version", "--client"],
    ],
    "flux": [
        ["version", "--client", "-o", "json"],
        ["version", "--client"],
        ["--version"],
    ],
    "pv-migrate": [
        ["--version"],
    ],
}


@dataclass(frozen=True)
class Platform:
    goos: str
    goarch: str

    @property
    def label(self) -> str:
        return f"{self.goos}/{self.goarch}"

    @property
    def is_windows(self) -> bool:
        return self.goos == "windows"


@dataclass(frozen=True)
class DownloadSpec:
    url: str
    kind: str  # binary | tar.gz | zip
    binary: str  # name of the executable inside the archive


@dataclass
class ToolState:
    tool: str
    destination: Path
    wanted: str
    installed: str | None
    state: str  # ok | missing | outdated | unknown
    detail: str = ""
    source_path: Path | None = None


def normalize_version(version: str) -> str:
    version = version.strip()
    return version if version.startswith("v") else f"v{version}"


def versions_equal(left: str, right: str) -> bool:
    return normalize_version(left) == normalize_version(right)


def versions() -> dict[str, str]:
    settings = common.load_env_file(common.TALOS_SETTINGS)
    talos_version = os.environ.get("TALOS_VERSION") or settings.get("TALOS_VERSION")
    k8s_version = os.environ.get("KUBECTL_VERSION") or settings.get(
        "KUBERNETES_VERSION"
    )
    flux_version = os.environ.get("FLUX_VERSION") or common.DEFAULT_FLUX_VERSION
    pv_migrate_version = (
        os.environ.get("PV_MIGRATE_VERSION") or common.DEFAULT_PV_MIGRATE_VERSION
    )

    if not talos_version:
        common.die(f"TALOS_VERSION is not set in {common.TALOS_SETTINGS}")
    if not k8s_version:
        common.die(f"KUBERNETES_VERSION is not set in {common.TALOS_SETTINGS}")

    return {
        "talosctl": normalize_version(talos_version),
        "kubectl": normalize_version(k8s_version),
        "flux": normalize_version(flux_version),
        "pv-migrate": normalize_version(pv_migrate_version),
    }


def normalize_os(raw: str) -> str:
    key = raw.strip().lower()
    if key not in _OS_ALIASES:
        common.die(
            f"unsupported operating system: {raw!r} "
            f"(supported: {sorted(set(_OS_ALIASES.values()))})"
        )
    return _OS_ALIASES[key]


def normalize_arch(raw: str) -> str:
    key = raw.strip().lower()
    if key not in _ARCH_ALIASES:
        common.die(
            f"unsupported architecture: {raw!r} "
            f"(supported: {sorted(set(_ARCH_ALIASES.values()))})"
        )
    return _ARCH_ALIASES[key]


def detect_host() -> Platform:
    system = platform.system().strip()
    machine = platform.machine().strip()
    if not system:
        common.die(f"cannot detect host operating system: {system!r}")
    if not machine:
        common.die(f"cannot detect host architecture: {machine!r}")
    return Platform(normalize_os(system), normalize_arch(machine))


def parse_platform_spec(spec: str) -> Platform:
    parts = re.split(r"[/_-]", spec.strip())
    if len(parts) != 2 or not all(parts):
        common.die(
            f"invalid platform {spec!r} (expected e.g. linux/amd64, "
            "darwin/arm64 or windows/amd64)"
        )
    return Platform(normalize_os(parts[0]), normalize_arch(parts[1]))


def resolve_platform(
    platform_flag: str | None,
    os_flag: str | None,
    arch_flag: str | None,
) -> Platform:
    platform_spec = (
        platform_flag
        or os.environ.get("DOWNLOAD_TOOLS_PLATFORM")
        or os.environ.get("DOWNLOAD_TOOLS_PLATFORM_SPEC")
    )
    os_value = (
        os_flag
        or os.environ.get("DOWNLOAD_TOOLS_OS")
        or os.environ.get("GOOS")
    )
    arch_value = (
        arch_flag
        or os.environ.get("DOWNLOAD_TOOLS_ARCH")
        or os.environ.get("GOARCH")
    )
    if platform_spec:
        if os_flag or arch_flag:
            common.die("use either --platform or --os/--arch, not both")
        return parse_platform_spec(platform_spec)
    host = detect_host()
    goos = normalize_os(os_value) if os_value else host.goos
    goarch = normalize_arch(arch_value) if arch_value else host.goarch
    return Platform(goos, goarch)


def binary_filename(tool: str, plat: Platform) -> str:
    return f"{tool}.exe" if plat.is_windows else tool


def destination_for(tool: str, plat: Platform, host: Platform) -> Path:
    name = binary_filename(tool, plat)
    if plat == host:
        return common.TOOLS_DIR / name
    return common.TOOLS_DIR / f"{plat.goos}_{plat.goarch}" / name


def alternate_destination(tool: str, destination: Path) -> Path:
    """The same tool name with the opposite .exe suffix, in the same dir."""
    if destination.name.endswith(".exe"):
        return destination.with_name(destination.name[:-4])
    return destination.with_name(f"{destination.name}.exe")


def download_spec(tool: str, version: str, plat: Platform) -> DownloadSpec:
    goos, goarch = plat.goos, plat.goarch
    extension = ".exe" if plat.is_windows else ""

    if tool == "talosctl":
        tokens = {"amd64": "amd64", "arm64": "arm64", "arm": "armv7", "riscv64": "riscv64"}
        supported = (
            (goos == "linux" and goarch in tokens)
            or (goos == "darwin" and goarch in ("amd64", "arm64"))
            or (goos == "windows" and goarch in ("amd64", "arm64"))
            or (goos == "freebsd" and goarch in ("amd64", "arm64"))
        )
        if not supported or goarch not in tokens:
            common.die(
                f"no talosctl build for {plat.label} (supported: linux/amd64, "
                "linux/arm64, linux/arm, linux/riscv64, darwin/amd64, "
                "darwin/arm64, windows/amd64, windows/arm64, freebsd/amd64, "
                "freebsd/arm64)"
            )
        url = (
            "https://github.com/siderolabs/talos/releases/download/"
            f"{version}/talosctl-{goos}-{tokens[goarch]}{extension}"
        )
        return DownloadSpec(url, "binary", f"talosctl{extension}")

    if tool == "kubectl":
        tokens = {
            "amd64": "amd64",
            "arm64": "arm64",
            "arm": "arm",
            "386": "386",
            "ppc64le": "ppc64le",
            "s390x": "s390x",
        }
        supported = (
            (goos == "linux" and goarch in tokens)
            or (goos == "darwin" and goarch in ("amd64", "arm64"))
            or (goos == "windows" and goarch in ("amd64", "arm64"))
        )
        if not supported:
            common.die(
                f"no kubectl build for {plat.label} (supported: linux/amd64, "
                "linux/arm64, linux/arm, linux/386, linux/ppc64le, "
                "linux/s390x, darwin/amd64, darwin/arm64, windows/amd64, "
                "windows/arm64)"
            )
        url = f"https://dl.k8s.io/release/{version}/bin/{goos}/{tokens[goarch]}/kubectl{extension}"
        return DownloadSpec(url, "binary", f"kubectl{extension}")

    if tool == "flux":
        bare = version.lstrip("v")
        if plat.is_windows:
            if goarch not in ("amd64", "arm64", "386"):
                common.die(f"no flux build for {plat.label}")
            url = (
                "https://github.com/fluxcd/flux2/releases/download/"
                f"{version}/flux_{bare}_windows_{goarch}.zip"
            )
            return DownloadSpec(url, "zip", "flux.exe")
        supported = (
            (goos == "linux" and goarch in ("amd64", "arm64", "arm"))
            or (goos == "darwin" and goarch in ("amd64", "arm64"))
        )
        if not supported:
            common.die(
                f"no flux build for {plat.label} (supported: linux/amd64, "
                "linux/arm64, linux/arm, darwin/amd64, darwin/arm64, "
                "windows/386, windows/amd64, windows/arm64)"
            )
        url = (
            "https://github.com/fluxcd/flux2/releases/download/"
            f"{version}/flux_{bare}_{goos}_{goarch}.tar.gz"
        )
        return DownloadSpec(url, "tar.gz", "flux")

    # pv-migrate
    tokens = {"amd64": "x86_64", "arm64": "arm64", "arm": "armv7"}
    if plat.is_windows:
        if goarch != "amd64":
            common.die(
                f"no pv-migrate build for {plat.label} (supported: "
                "windows/amd64, linux/amd64, linux/arm64, linux/arm, "
                "darwin/amd64, darwin/arm64)"
            )
        url = (
            "https://github.com/utkuozdemir/pv-migrate/releases/download/"
            f"{version}/pv-migrate_{version}_windows_x86_64.zip"
        )
        return DownloadSpec(url, "zip", "pv-migrate.exe")
    supported = (
        (goos == "linux" and goarch in tokens)
        or (goos == "darwin" and goarch in ("amd64", "arm64"))
    )
    if not supported:
        common.die(
            f"no pv-migrate build for {plat.label} (supported: "
            "windows/amd64, linux/amd64, linux/arm64, linux/arm, "
            "darwin/amd64, darwin/arm64)"
        )
    url = (
        "https://github.com/utkuozdemir/pv-migrate/releases/download/"
        f"{version}/pv-migrate_{version}_{goos}_{tokens[goarch]}.tar.gz"
    )
    return DownloadSpec(url, "tar.gz", "pv-migrate")


def manifest_key(tool: str, plat: Platform) -> str:
    return f"{tool}@{plat.label}"


def load_manifest() -> dict[str, dict[str, str]]:
    path = common.TOOLS_DIR / VERSION_MANIFEST
    if not path.is_file():
        return {}
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        common.warn(f"{path}: ignoring unreadable version manifest: {exc}")
        return {}
    if not isinstance(data, dict):
        common.warn(f"{path}: ignoring malformed version manifest")
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def save_manifest(manifest: dict[str, dict[str, str]]) -> None:
    path = common.TOOLS_DIR / VERSION_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{VERSION_MANIFEST}.", suffix=".part", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError as exc:
        common.die(f"failed to write {path}: {exc}")
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def manifest_version(
    manifest: dict[str, dict[str, str]], tool: str, plat: Platform
) -> str | None:
    entry = manifest.get(manifest_key(tool, plat))
    if not entry:
        return None
    recorded = entry.get("version")
    return normalize_version(recorded) if recorded else None


def record_manifest(
    manifest: dict[str, dict[str, str]],
    tool: str,
    plat: Platform,
    version: str,
    destination: Path,
) -> None:
    manifest[manifest_key(tool, plat)] = {
        "file": destination.name,
        "platform": plat.label,
        "version": normalize_version(version),
    }


def extract_version(output: str) -> str | None:
    for pattern in _VERSION_PATTERNS:
        match = pattern.search(output)
        if match:
            return normalize_version(match.group(1).strip().rstrip(",)\"'"))
    match = _GENERIC_VERSION_RE.search(output)
    if match:
        return normalize_version(match.group(0))
    # Last resort (e.g. a bare "2.9.6" line) - only if there is exactly
    # one dotted-numeric token that looks like a semver.
    candidates = _GENERIC_VERSION_RE_NO_PREFIX.findall(output)
    if len(set(candidates)) == 1:
        return normalize_version(candidates[0])
    return None


def probe_version(path: Path, tool: str) -> str | None:
    """Execute the local binary and parse its client version, if possible."""
    if not path.is_file():
        return None
    for argv in _PROBE_COMMANDS.get(tool, [["--version"]]):
        try:
            result = subprocess.run(
                [str(path), *argv],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        version = extract_version(output)
        if version:
            return version
    return None


def make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(0o755)
    except OSError:
        pass


def tool_state(
    tool: str,
    wanted: str,
    plat: Platform,
    host: Platform,
    manifest: dict[str, dict[str, str]],
) -> ToolState:
    destination = destination_for(tool, plat, host)
    source_path: Path | None = None
    if destination.is_file():
        source_path = destination
    else:
        alternate = alternate_destination(tool, destination)
        if alternate.is_file():
            installed = (
                probe_version(alternate, tool)
                if plat == host
                else manifest_version(manifest, tool, plat)
            )
            return ToolState(
                tool,
                destination,
                wanted,
                installed,
                "outdated",
                detail=(
                    f"unexpected file {alternate.name} "
                    f"(expected {destination.name})"
                ),
                source_path=alternate,
            )

    if source_path is None:
        return ToolState(tool, destination, wanted, None, "missing")

    if plat != host:
        recorded = manifest_version(manifest, tool, plat)
        if recorded is not None and versions_equal(recorded, wanted):
            return ToolState(
                tool,
                destination,
                wanted,
                recorded,
                "ok",
                detail="recorded in version manifest (foreign platform)",
                source_path=source_path,
            )
        if recorded is not None:
            return ToolState(
                tool,
                destination,
                wanted,
                recorded,
                "outdated",
                source_path=source_path,
            )
        return ToolState(
            tool,
            destination,
            wanted,
            None,
            "unknown",
            detail="no version manifest entry for this platform",
            source_path=source_path,
        )

    make_executable(source_path)
    installed = probe_version(source_path, tool)
    if installed is None:
        return ToolState(
            tool,
            destination,
            wanted,
            None,
            "unknown",
            detail="cannot determine version",
            source_path=source_path,
        )
    if versions_equal(installed, wanted):
        return ToolState(
            tool, destination, wanted, installed, "ok", source_path=source_path
        )
    return ToolState(
        tool, destination, wanted, installed, "outdated", source_path=source_path
    )


def download_binary(url: str, destination: Path) -> None:
    print(f"==> Downloading {destination.name} -> {destination}")
    common.download_file(url, destination)
    make_executable(destination)
    print(f"    {destination}")


def download_tar_binary(
    archive_url: str,
    binary: str,
    destination: Path,
    *,
    label: str | None = None,
) -> None:
    print(f"==> Downloading {label or destination.name} -> {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{destination.name}-download-"
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        archive = Path(temporary) / "download.tar.gz"
        common.download_file(archive_url, archive)
        with tarfile.open(archive, "r:gz") as tar:
            member = next(
                (
                    item
                    for item in tar.getmembers()
                    if item.isfile() and Path(item.name).name == binary
                ),
                None,
            )
            if member is None:
                common.die(f"{archive_url} does not contain a {binary} binary")
            source = tar.extractfile(member)
            if source is None:
                common.die(f"cannot read {binary} binary from {archive_url}")
            temporary_binary = destination.with_name(f".{destination.name}.part")
            with temporary_binary.open("wb") as handle:
                handle.write(source.read())
            make_executable(temporary_binary)
            os.replace(temporary_binary, destination)
    print(f"    {destination}")


def download_zip_binary(
    archive_url: str,
    binary: str,
    destination: Path,
    *,
    label: str | None = None,
) -> None:
    print(f"==> Downloading {label or destination.name} -> {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{destination.name}-download-"
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        archive = Path(temporary) / "download.zip"
        common.download_file(archive_url, archive)
        with zipfile.ZipFile(archive) as archive_file:
            info = next(
                (
                    item
                    for item in archive_file.infolist()
                    if not item.is_dir() and Path(item.filename).name == binary
                ),
                None,
            )
            if info is None:
                common.die(f"{archive_url} does not contain a {binary} binary")
            temporary_binary = destination.with_name(f".{destination.name}.part")
            with archive_file.open(info) as source, temporary_binary.open("wb") as handle:
                handle.write(source.read())
            make_executable(temporary_binary)
            os.replace(temporary_binary, destination)
    print(f"    {destination}")


def download_tool(
    tool: str,
    version: str,
    plat: Platform,
    destination: Path,
) -> None:
    spec = download_spec(tool, version, plat)
    label = f"{tool} {version} ({spec.url})" if spec.kind != "binary" else None
    if spec.kind == "binary":
        # The URL itself is printed by download_binary via the destination.
        download_binary(spec.url, destination)
    elif spec.kind == "zip":
        download_zip_binary(spec.url, spec.binary, destination, label=label)
    else:
        download_tar_binary(spec.url, spec.binary, destination, label=label)


def print_state(state: ToolState, *, verb: str | None = None) -> None:
    destination = state.destination
    if state.state == "ok":
        prefix = verb or "already present"
        print(
            f"==> {state.tool}: {prefix} ({state.installed}) "
            f"({destination})"
        )
    elif state.state == "missing":
        print(
            f"==> {state.tool}: missing, want {state.wanted} "
            f"({destination})"
        )
    elif state.state == "outdated":
        installed = state.installed or "unknown"
        suffix = f" - {state.detail}" if state.detail else ""
        print(
            f"==> {state.tool}: {installed} -> {state.wanted} "
            f"({destination}){suffix}"
        )
    else:
        suffix = f" - {state.detail}" if state.detail else ""
        print(
            f"==> {state.tool}: unknown version, want {state.wanted} "
            f"({destination}){suffix}"
        )


def state_as_dict(state: ToolState, spec: DownloadSpec | None = None) -> dict[str, str]:
    data: dict[str, str] = {
        "tool": state.tool,
        "wanted": state.wanted,
        "installed": state.installed or "",
        "state": state.state,
        "path": state.destination.as_posix(),
    }
    if state.detail:
        data["detail"] = state.detail
    if spec is not None:
        data["url"] = spec.url
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="download-tools.py",
        description=(
            "Download pinned talosctl, kubectl, flux and pv-migrate binaries "
            "into scripts/bin/, updating tools whose installed version "
            "differs from the pinned version."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Versions come from talos/settings.env and can be overridden with\n"
            "TALOS_VERSION, KUBECTL_VERSION, FLUX_VERSION and PV_MIGRATE_VERSION.\n"
            "Platform defaults to the host (linux/darwin/windows/freebsd on\n"
            "amd64/arm64/arm/...); override with --platform e.g. linux/amd64,\n"
            "darwin/arm64, windows/amd64, or --os/--arch, or the env vars\n"
            "DOWNLOAD_TOOLS_PLATFORM, DOWNLOAD_TOOLS_OS, DOWNLOAD_TOOLS_ARCH,\n"
            "GOOS and GOARCH. Foreign-platform binaries go to\n"
            "scripts/bin/<os>_<arch>/ and are compared via .tool-versions.json.\n"
        ),
    )
    parser.add_argument(
        "tools", nargs="*", choices=TOOLS, metavar="tool",
        help=f"tools to download (default: all of {' '.join(TOOLS)})",
    )
    parser.add_argument(
        "--platform",
        metavar="OS/ARCH",
        help=(
            "target platform, e.g. linux/amd64, darwin/arm64, windows/amd64 "
            "(default: host, or DOWNLOAD_TOOLS_PLATFORM)"
        ),
    )
    parser.add_argument(
        "--os",
        metavar="OS",
        help=(
            "target OS: linux, darwin, windows or freebsd "
            "(default: host, or DOWNLOAD_TOOLS_OS/GOOS)"
        ),
    )
    parser.add_argument(
        "--arch",
        metavar="ARCH",
        help=(
            "target arch: amd64, arm64, arm, 386, riscv64, ... "
            "(default: host, or DOWNLOAD_TOOLS_ARCH/GOARCH)"
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="download even if the version matches"
    )
    parser.add_argument(
        "--check",
        "--dry-run",
        action="store_true",
        help="only report whether tools are current; exit 1 if any needs an update",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable status summary"
    )
    args = parser.parse_args(argv)

    selected = args.tools or list(TOOLS)
    tool_versions = versions()
    plat = resolve_platform(args.platform, args.os, args.arch)
    host = detect_host()
    common.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()

    # Validate platforms before any state handling so unsupported targets
    # fail fast with a clear message in both modes.
    specs: dict[str, DownloadSpec] = {}
    for tool in selected:
        specs[tool] = download_spec(tool, tool_versions[tool], plat)

    states: list[ToolState] = [
        tool_state(tool, tool_versions[tool], plat, host, manifest)
        for tool in selected
    ]

    if args.check:
        if not args.json:
            print(f"==> Platform: {plat.label}" + (f" (host: {host.label})"))
            for state in states:
                print_state(state)
        needs_update = [state for state in states if state.state != "ok"]
        if args.json:
            print(
                json.dumps(
                    {
                        "platform": plat.label,
                        "host": host.label,
                        "cross": plat != host,
                        "tools": [
                            state_as_dict(state, specs[state.tool]) for state in states
                        ],
                    },
                    indent=2,
                )
            )
        if needs_update:
            if not args.json:
                print(f"\nSummary: {len(needs_update)} tool(s) need an update")
            return 1
        if not args.json:
            print("\nSummary: all tools are up to date")
        return 0

    for state in states:
        if not args.force and state.state == "ok":
            print_state(state, verb="already present")
            continue
        if args.force and state.state == "ok":
            print_state(state, verb="re-downloading")
        elif state.state == "missing":
            print_state(state)
        elif state.state == "outdated":
            print_state(state)
        else:
            print_state(state)
        download_tool(state.tool, state.wanted, plat, state.destination)
        record_manifest(manifest, state.tool, plat, state.wanted, state.destination)
        save_manifest(manifest)

    print(f"\nTools are in {common.TOOLS_DIR}")
    if args.json:
        # Re-read state so host binaries are verified by execution after
        # the download and foreign binaries are verified via the manifest.
        final_states = [
            tool_state(tool, tool_versions[tool], plat, host, manifest)
            for tool in selected
        ]
        print(
            json.dumps(
                {
                    "platform": plat.label,
                    "host": host.label,
                    "cross": plat != host,
                    "tools": [
                        state_as_dict(state, specs[state.tool]) for state in final_states
                    ],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
