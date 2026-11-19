#!/usr/bin/env python3
"""Build the Talos Factory schematic and print/download the boot assets.

Usage:
  ./scripts/talos/gen-iso.py              # create base + GPU schematics, print URLs
  ./scripts/talos/gen-iso.py --download   # ... and download the base metal-amd64.iso

The base and GPU schematic IDs are cached in talos/generated/schematic-id and
talos/generated/schematic-gpu-id. The base image is used by every node except
the nodes listed in TALOS_GPU_NODES; the GPU image is used for glemmen250.
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
GPU_SCHEMATIC_FILE = common.TALOS_DIR / "schematic-gpu.yaml"
GPU_SCHEMATIC_ID_FILE = common.GENERATED_DIR / "schematic-gpu-id"


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
    if not GPU_SCHEMATIC_FILE.is_file():
        common.die(f"{GPU_SCHEMATIC_FILE} not found")

    common.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    print(f"==> Creating base schematic from {SCHEMATIC_FILE}")
    schematic_id = common.create_schematic_from_file(SCHEMATIC_FILE)
    SCHEMATIC_ID_FILE.write_text(schematic_id + "\n", encoding="utf-8")
    print(f"    base schematic ID: {schematic_id}")

    print(f"==> Creating GPU schematic from {GPU_SCHEMATIC_FILE}")
    gpu_schematic_id = common.create_schematic_from_file(GPU_SCHEMATIC_FILE)
    GPU_SCHEMATIC_ID_FILE.write_text(gpu_schematic_id + "\n", encoding="utf-8")
    print(f"    GPU schematic ID: {gpu_schematic_id}")

    def urls(sid: str) -> tuple[str, str, str]:
        return (
            f"{common.FACTORY_URL}/image/{sid}/{talos_version}/metal-amd64.iso",
            f"{common.FACTORY_URL}/image/{sid}/{talos_version}/metal-amd64.raw.xz",
            f"factory.talos.dev/metal-installer/{sid}:{talos_version}",
        )

    iso_url, raw_url, installer_image = urls(schematic_id)
    gpu_iso_url, gpu_raw_url, gpu_installer_image = urls(gpu_schematic_id)

    print()
    print(f"Talos        : {talos_version}")
    print(f"Kubernetes   : {kubernetes_version}")
    print(f"Base ISO     : {iso_url}")
    print(f"Base disk    : {raw_url}")
    print(f"Base install : {installer_image}")
    print(f"GPU ISO      : {gpu_iso_url}")
    print(f"GPU disk     : {gpu_raw_url}")
    print(f"GPU install  : {gpu_installer_image}")
    print("Kernel args  : bond=... ip=... (see talos/README.md#maintenance-mode-network)")

    if args.download:
        destination = common.GENERATED_DIR / "iso" / "metal-amd64.iso"
        print()
        print(f"==> Downloading base ISO to {destination}")
        common.download_file(iso_url, destination)
        print("==> Done. Flash it to a USB stick or mount it as virtual media.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
