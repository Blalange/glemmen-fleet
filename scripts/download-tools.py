#!/usr/bin/env python3
"""Download the pinned command-line tools into scripts/bin/.

Tools:
  talosctl   version from talos/settings.env (TALOS_VERSION)
  kubectl    version from talos/settings.env (KUBERNETES_VERSION)
  flux       version from FLUX_VERSION or the built-in default

Usage:
  ./scripts/download-tools.py                 # download all missing tools
  ./scripts/download-tools.py talosctl        # only talosctl
  ./scripts/download-tools.py flux --force    # re-download flux
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

TOOLS = ("talosctl", "kubectl", "flux")


def normalize_version(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def versions() -> dict[str, str]:
    settings = common.load_env_file(common.TALOS_SETTINGS)
    talos_version = os.environ.get("TALOS_VERSION") or settings.get("TALOS_VERSION")
    k8s_version = os.environ.get("KUBECTL_VERSION") or settings.get(
        "KUBERNETES_VERSION"
    )
    flux_version = os.environ.get("FLUX_VERSION") or common.DEFAULT_FLUX_VERSION

    if not talos_version:
        common.die(f"TALOS_VERSION is not set in {common.TALOS_SETTINGS}")
    if not k8s_version:
        common.die(f"KUBERNETES_VERSION is not set in {common.TALOS_SETTINGS}")

    return {
        "talosctl": talos_version,
        "kubectl": normalize_version(k8s_version),
        "flux": normalize_version(flux_version),
    }


def already_present(destination: Path, force: bool) -> bool:
    if destination.is_file() and not force:
        destination.chmod(0o755)
        print(f"==> {destination.name}: already present ({destination})")
        return True
    return False


def download_binary(url: str, destination: Path, force: bool) -> None:
    if already_present(destination, force):
        return
    print(f"==> Downloading {destination.name} -> {destination}")
    common.download_file(url, destination)
    destination.chmod(0o755)
    print(f"    {destination}")


def download_flux(version: str, destination: Path, force: bool) -> None:
    if already_present(destination, force):
        return

    archive_url = (
        "https://github.com/fluxcd/flux2/releases/download/"
        f"{version}/flux_{version.lstrip('v')}_linux_amd64.tar.gz"
    )
    print(f"==> Downloading flux {version} -> {destination}")
    with tempfile.TemporaryDirectory(prefix="flux-download-") as temporary:
        archive = Path(temporary) / "flux.tar.gz"
        common.download_file(archive_url, archive)
        with tarfile.open(archive, "r:gz") as tar:
            member = next(
                (
                    item
                    for item in tar.getmembers()
                    if item.name == "flux" or item.name.endswith("/flux")
                ),
                None,
            )
            if member is None:
                common.die(f"{archive_url} does not contain a flux binary")
            source = tar.extractfile(member)
            if source is None:
                common.die(f"cannot read flux binary from {archive_url}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_binary = destination.with_name(f".{destination.name}.part")
            with temporary_binary.open("wb") as handle:
                handle.write(source.read())
            temporary_binary.chmod(0o755)
            os.replace(temporary_binary, destination)
    print(f"    {destination}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="download-tools.py",
        description="Download pinned talosctl, kubectl and flux binaries into scripts/bin/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Versions come from talos/settings.env and can be overridden with\n"
            "TALOS_VERSION, KUBECTL_VERSION and FLUX_VERSION.\n"
        ),
    )
    parser.add_argument(
        "tools", nargs="*", choices=TOOLS, metavar="tool",
        help=f"tools to download (default: all of {' '.join(TOOLS)})",
    )
    parser.add_argument(
        "--force", action="store_true", help="download even if the tool already exists"
    )
    args = parser.parse_args(argv)

    selected = args.tools or list(TOOLS)
    tool_versions = versions()
    common.TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    for tool in selected:
        version = tool_versions[tool]
        destination = common.TOOLS_DIR / tool
        if tool == "talosctl":
            download_binary(
                "https://github.com/siderolabs/talos/releases/download/"
                f"{version}/talosctl-linux-amd64",
                destination,
                args.force,
            )
        elif tool == "kubectl":
            download_binary(
                f"https://dl.k8s.io/release/{version}/bin/linux/amd64/kubectl",
                destination,
                args.force,
            )
        else:
            download_flux(version, destination, args.force)

    print(f"\nTools are in {common.TOOLS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
