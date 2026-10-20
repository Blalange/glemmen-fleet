#!/usr/bin/env python3
"""Generate the Talos machine configs for every node.

Steps:
  1. render the Netbird extension config from talos/secrets/netbird.env
  2. create the cluster secrets bundle (talos/generated/secrets.yaml) if missing
  3. generate the base control-plane config (talos/generated/base/) with the common patch
  4. merge each talos/nodes/<host>.yaml on top and validate the result

Everything lands in talos/generated/ (gitignored). The secrets bundle is the
cluster PKI: back it up, losing it means losing talosctl access.

This script never downloads tools. Run ./scripts/download-tools.py first if
talosctl is missing.

Usage: ./scripts/talos/gen-config.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import common  # noqa: E402

GEN_ISO = SCRIPT_DIR / "gen-iso.py"
GENERATED = common.GENERATED_DIR
PATCHES = common.PATCHES_DIR
SECRETS = common.SECRETS_DIR
NODES_DIR = common.NODES_DIR


def require_setting(settings: dict[str, str], key: str) -> str:
    value = settings.get(key, "").strip()
    if not value:
        common.die(f"{key} is not set in {common.TALOS_SETTINGS}")
    return value


def talos_version_string(talosctl: str) -> str:
    result = common.run(
        [talosctl, "version", "--client", "--short"],
        check=False,
        capture=True,
        quiet=True,
    )
    return " ".join((result.stdout or "").split())


def render_netbird_patch() -> None:
    env_file = SECRETS / "netbird.env"
    if not env_file.is_file():
        common.die(f"{env_file} is missing (see talos/secrets/README.md)")

    netbird_env = common.load_env_file(env_file)
    setup_key = netbird_env.get("NB_SETUP_KEY", "")
    if not setup_key:
        common.die(f"NB_SETUP_KEY is empty in {env_file}")
    if any(character in setup_key for character in "\r\n"):
        common.die("NB_SETUP_KEY contains a newline")

    template = PATCHES / "netbird.yaml.tpl"
    if not template.is_file():
        common.die(f"{template} not found")
    template_text = template.read_text(encoding="utf-8")
    placeholder = "NB_SETUP_KEY=@NB_SETUP_KEY@"
    if placeholder not in template_text:
        common.die(f"{template} does not contain the {placeholder} placeholder")

    output = GENERATED / "patches" / "netbird.yaml"
    output.write_text(
        template_text.replace(placeholder, f"NB_SETUP_KEY={setup_key}"),
        encoding="utf-8",
    )
    print(f"==> Rendered {output}")


def render_install_patch(installer_image: str, disk_selector: str) -> None:
    yaml = common.require_yaml()
    payload = {
        "apiVersion": "v1alpha1",
        "kind": "UnattendedInstallConfig",
        "reboot": True,
        "installer": {"image": installer_image},
        "provisioning": {
            "diskSelector": {"match": disk_selector},
            "wipe": True,
        },
    }
    output = GENERATED / "patches" / "install.yaml"
    output.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    print(f"==> Rendered {output}")


def generate_base_config(
    talosctl: str,
    settings: dict[str, str],
    installer_image: str,
) -> Path:
    base_dir = GENERATED / "base"
    shutil.rmtree(base_dir, ignore_errors=True)
    print("==> Generating base config")
    common.run(
        [
            talosctl,
            "gen",
            "config",
            settings["CLUSTER_NAME"],
            settings["CLUSTER_ENDPOINT"],
            "--with-secrets",
            GENERATED / "secrets.yaml",
            "--kubernetes-version",
            settings["KUBERNETES_VERSION"],
            "--install-image",
            installer_image,
            "--install-disk",
            "/dev/sda",
            "--output-dir",
            base_dir,
            "--config-patch",
            f"@{PATCHES / 'common.yaml'}",
            "--config-patch-control-plane",
            f"@{PATCHES / 'control-plane.yaml'}",
            "--config-patch",
            f"@{GENERATED / 'patches' / 'install.yaml'}",
            "--config-patch",
            f"@{GENERATED / 'patches' / 'netbird.yaml'}",
            "--with-docs=false",
            "--with-examples=false",
        ]
    )
    return base_dir


def generate_node_configs(
    talosctl: str, base_dir: Path, workers: set[str]
) -> None:
    print("==> Merging node configs")
    node_files = sorted(NODES_DIR.glob("*.yaml"))
    if not node_files:
        common.die(f"no node configs found in {NODES_DIR}")

    for node_file in node_files:
        node = node_file.stem
        if node in workers:
            base = base_dir / "worker.yaml"
            role = "worker"
        else:
            base = base_dir / "controlplane.yaml"
            role = "control-plane"

        output = GENERATED / f"{node}.yaml"
        common.run(
            [
                talosctl,
                "machineconfig",
                "patch",
                base,
                "--patch",
                f"@{node_file}",
                "-o",
                output,
            ]
        )
        validation = common.run(
            [talosctl, "validate", "--config", output, "--mode", "metal"],
            capture=True,
        )
        # talosctl prints warnings (for example KubeNodeConfig role-label
        # warnings) on stderr even when validation succeeds.
        if validation.stderr.strip():
            print(validation.stderr.rstrip(), file=sys.stderr)
        print(f"    {node}.yaml OK ({role})")


def write_talosconfig(
    talosctl: str,
    base_dir: Path,
    nodes: list[common.Node],
    workers: set[str],
    bootstrap: common.Node,
) -> None:
    talosconfig = GENERATED / "talosconfig"
    shutil.copyfile(base_dir / "talosconfig", talosconfig)

    # Workers cannot forward Talos API requests. Listing them as endpoints makes
    # talosctl fail intermittently with "PermissionDenied: no request forwarding".
    control_planes = common.control_plane_nodes(nodes, workers)
    print("==> Configuring talosconfig")
    common.run(
        [
            talosctl,
            "--talosconfig",
            talosconfig,
            "config",
            "endpoints",
            *[node.address for node in control_planes],
        ],
        capture=True,
    )
    common.run(
        [
            talosctl,
            "--talosconfig",
            talosconfig,
            "config",
            "nodes",
            bootstrap.address,
        ],
        capture=True,
    )
    print(f"    endpoints: {', '.join(node.address for node in control_planes)}")
    print(f"    default node: {bootstrap.name} ({bootstrap.address})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gen-config.py",
        description="Generate and validate Talos machine configs.",
    )
    parser.parse_args(argv)

    settings = common.load_talos_settings()
    for key in (
        "TALOS_VERSION",
        "KUBERNETES_VERSION",
        "CLUSTER_NAME",
        "CLUSTER_ENDPOINT",
        "INSTALL_DISK_SELECTOR",
        "TALOS_NODES",
        "BOOTSTRAP_NODE",
    ):
        require_setting(settings, key)

    nodes = common.parse_node_table(settings["TALOS_NODES"])
    workers = common.worker_names(settings)
    bootstrap = common.bootstrap_node(settings, nodes)
    talosctl = common.require_tool("talosctl", "TALOSCTL")
    version = talos_version_string(talosctl)
    print(f"==> Using talosctl: {talosctl}" + (f" ({version})" if version else ""))

    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "patches").mkdir(parents=True, exist_ok=True)

    schematic_id_file = GENERATED / "schematic-id"
    if not schematic_id_file.is_file():
        print("==> No schematic ID yet, running gen-iso.py")
        common.run([sys.executable, str(GEN_ISO)], capture=True)
    schematic_id = schematic_id_file.read_text(encoding="utf-8").strip()
    if not schematic_id:
        common.die(f"{schematic_id_file} is empty (run ./scripts/talos/gen-iso.py)")
    installer_image = (
        f"factory.talos.dev/metal-installer/{schematic_id}:"
        f"{settings['TALOS_VERSION']}"
    )
    print(f"==> Schematic: {schematic_id}")

    render_netbird_patch()
    render_install_patch(installer_image, settings["INSTALL_DISK_SELECTOR"])

    secrets_file = GENERATED / "secrets.yaml"
    if not secrets_file.is_file():
        print("==> Creating new cluster secrets bundle (PKI)")
        common.run([talosctl, "gen", "secrets", "-o", secrets_file])
    else:
        print(f"==> Reusing existing secrets bundle {secrets_file}")

    base_dir = generate_base_config(talosctl, settings, installer_image)
    generate_node_configs(talosctl, base_dir, workers)
    write_talosconfig(talosctl, base_dir, nodes, workers, bootstrap)

    print()
    print(f"Done. Configs in {GENERATED}:")
    for path in sorted(GENERATED.glob("*.yaml")):
        print(f"    {path.name}")
    print()
    print("Next: boot the ISO and run ./scripts/talos/manage.py apply <node>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
