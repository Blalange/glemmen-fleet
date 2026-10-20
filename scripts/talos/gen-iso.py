#!/usr/bin/env python3
"""Build the Talos Factory schematic and print/download the boot assets.

Usage:
  ./scripts/talos/gen-iso.py              # create schematic, print ISO/installer URLs
  ./scripts/talos/gen-iso.py --download   # ... and download metal-amd64.iso

The schematic ID is cached in talos/generated/schematic-id.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import common  # noqa: E402

SCHEMATIC_FILE = common.TALOS_DIR / "schematic.yaml"
SCHEMATIC_ID_FILE = common.GENERATED_DIR / "schematic-id"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gen-iso.py",
        description="Create the Talos Factory schematic and boot-asset URLs.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="download metal-amd64.iso into talos/generated/iso/",
    )
    args = parser.parse_args(argv)

    settings = common.load_talos_settings()
    talos_version = settings.get("TALOS_VERSION", "")
    kubernetes_version = settings.get("KUBERNETES_VERSION", "")
    if not talos_version:
        common.die("TALOS_VERSION is not set in talos/settings.env")
    if not SCHEMATIC_FILE.is_file():
        common.die(f"{SCHEMATIC_FILE} not found")

    common.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    print(f"==> Creating schematic from {SCHEMATIC_FILE}")
    schematic_id = common.create_schematic_from_file(SCHEMATIC_FILE)

    SCHEMATIC_ID_FILE.write_text(schematic_id + "\n", encoding="utf-8")
    print(f"    schematic ID: {schematic_id}")

    iso_url = (
        f"{common.FACTORY_URL}/image/{schematic_id}/{talos_version}/metal-amd64.iso"
    )
    raw_url = (
        f"{common.FACTORY_URL}/image/{schematic_id}/{talos_version}"
        "/metal-amd64.raw.xz"
    )
    installer_image = (
        f"factory.talos.dev/metal-installer/{schematic_id}:{talos_version}"
    )

    print()
    print(f"Talos        : {talos_version}")
    print(f"Kubernetes   : {kubernetes_version}")
    print(f"ISO          : {iso_url}")
    print(f"Disk image   : {raw_url}")
    print(f"Installer    : {installer_image}")
    print("Kernel args  : bond=... ip=... (see talos/README.md#maintenance-mode-network)")

    if args.download:
        destination = common.GENERATED_DIR / "iso" / "metal-amd64.iso"
        print()
        print(f"==> Downloading ISO to {destination}")
        common.download_file(iso_url, destination)
        print("==> Done. Flash it to a USB stick or mount it as virtual media.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
