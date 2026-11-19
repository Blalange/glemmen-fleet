#!/usr/bin/env python3
"""Generate the Talos machine configs for every node.

Steps:
  1. render the Netbird extension config from talos/secrets/netbird.env
  2. create the cluster secrets bundle (talos/generated/secrets.yaml) if missing
  3. generate the base control-plane config (talos/generated/base/) with the common patch
  4. merge each talos/nodes/<host>.yaml on top and validate the result
  5. for TALOS_GPU_NODES, merge the GPU installer image while preserving the
     node-specific UnattendedInstallConfig

Everything lands in talos/generated/ (gitignored). The secrets bundle is the
cluster PKI: back it up, losing it means losing talosctl access.

This script never downloads tools. Run ./scripts/download-tools.py first if
talosctl is missing.

Usage: ./scripts/talos/gen-config.py
"""

from __future__ import annotations

import argparse
import shlex
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
PROFILES_DIR = common.PROFILES_DIR


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


def render_install_patch(
    installer_image: str, disk_selector: str, output_name: str = "install.yaml"
) -> None:
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
    output = GENERATED / "patches" / output_name
    output.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    print(f"==> Rendered {output}")


def gpu_node_names(settings: dict[str, str]) -> set[str]:
    raw = settings.get("TALOS_GPU_NODES", "").strip()
    if not raw:
        return set()
    try:
        names = set(shlex.split(raw))
    except ValueError as exc:
        common.die(f"cannot parse TALOS_GPU_NODES: {exc}")
    known = {node.name for node in common.parse_node_table(settings["TALOS_NODES"])}
    unknown = sorted(names - known)
    if unknown:
        common.die(
            "TALOS_GPU_NODES contains unknown node(s): " + ", ".join(unknown)
        )
    return names


def render_gpu_install_patch(
    node_file: Path, gpu_installer_image: str
) -> Path:
    """Render a per-node UnattendedInstallConfig with the GPU image.

    A partial patch that only sets installer.image would replace the
    UnattendedInstallConfig document and lose the node-specific disk selector
    (for example glemmen250's NVMe WWID). Start from the node's existing
    document and only replace the installer image.
    """
    yaml = common.require_yaml()
    try:
        documents = [
            document
            for document in yaml.safe_load_all(
                node_file.read_text(encoding="utf-8")
            )
            if document
        ]
    except (OSError, ValueError) as exc:
        common.die(f"failed to read {node_file}: {exc}")

    install = next(
        (
            document
            for document in documents
            if document.get("kind") == "UnattendedInstallConfig"
        ),
        None,
    )
    if install is None:
        common.die(
            f"{node_file} has no UnattendedInstallConfig; cannot select the GPU image"
        )

    install["installer"] = {"image": gpu_installer_image}
    output = GENERATED / "patches" / f"install-gpu-{node_file.stem}.yaml"
    output.write_text(
        yaml.safe_dump(install, sort_keys=False), encoding="utf-8"
    )
    print(f"==> Rendered {output}")
    return output


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
    talosctl: str,
    base_dir: Path,
    workers: set[str],
    gpu_nodes: set[str],
    gpu_installer_image: str,
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

        patches = [f"@{node_file}"]
        if node in gpu_nodes:
            gpu_patch = render_gpu_install_patch(
                node_file, gpu_installer_image
            )
            patches.append(f"@{gpu_patch}")

        command: list[str | Path] = [
            talosctl,
            "machineconfig",
            "patch",
            base,
        ]
        for patch in patches:
            command.extend(["--patch", patch])
        output = GENERATED / f"{node}.yaml"
        command.extend(["-o", output])
        common.run(command)

        validation = common.run(
            [talosctl, "validate", "--config", output, "--mode", "metal"],
            capture=True,
        )
        # talosctl prints warnings (for example KubeNodeConfig role-label
        # warnings) on stderr even when validation succeeds.
        if validation.stderr.strip():
            print(validation.stderr.rstrip(), file=sys.stderr)
        print(f"    {node}.yaml OK ({role})")


def generate_profile_configs(talosctl: str, base_dir: Path) -> None:
    """Render reusable, non-host-specific worker profiles.

    Profiles live in talos/profiles/ and are always merged on top of the worker
    base config. Unlike nodes/<host>.yaml they are not listed in TALOS_NODES;
    the resulting generated/<profile>.yaml can be applied to any machine.
    """
    profile_files = sorted(PROFILES_DIR.glob("*.yaml"))
    if not profile_files:
        return

    node_names = {path.stem for path in NODES_DIR.glob("*.yaml")}

    print("==> Merging profile configs")

    for profile_file in profile_files:
        profile = profile_file.stem
        if profile in node_names:
            collision = NODES_DIR / (profile + ".yaml")
            common.die(
                f"profile {profile_file} has the same name as a node config "
                f"({collision})"
            )

        output = GENERATED / f"{profile}.yaml"
        common.run(
            [
                talosctl,
                "machineconfig",
                "patch",
                base_dir / "worker.yaml",
                "--patch",
                f"@{profile_file}",
                "-o",
                output,
            ]
        )
        validation = common.run(
            [talosctl, "validate", "--config", output, "--mode", "metal"],
            capture=True,
        )
        if validation.stderr.strip():
            print(validation.stderr.rstrip(), file=sys.stderr)
        print(f"    {profile}.yaml OK (worker profile)")


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
    gpu_nodes = gpu_node_names(settings)
    bootstrap = common.bootstrap_node(settings, nodes)
    talosctl = common.require_tool("talosctl", "TALOSCTL")
    version = talos_version_string(talosctl)
    print(f"==> Using talosctl: {talosctl}" + (f" ({version})" if version else ""))
    if gpu_nodes:
        print(f"==> GPU schematic nodes: {', '.join(sorted(gpu_nodes))}")

    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "patches").mkdir(parents=True, exist_ok=True)

    schematic_id_file = GENERATED / "schematic-id"
    gpu_schematic_id_file = GENERATED / "schematic-gpu-id"
    if not schematic_id_file.is_file() or not gpu_schematic_id_file.is_file():
        print("==> Missing schematic ID(s), running gen-iso.py")
        common.run([sys.executable, str(GEN_ISO)], capture=True)

    schematic_id = schematic_id_file.read_text(encoding="utf-8").strip()
    gpu_schematic_id = gpu_schematic_id_file.read_text(encoding="utf-8").strip()
    if not schematic_id:
        common.die(f"{schematic_id_file} is empty (run ./scripts/talos/gen-iso.py)")
    if not gpu_schematic_id:
        common.die(
            f"{gpu_schematic_id_file} is empty (run ./scripts/talos/gen-iso.py)"
        )

    installer_image = (
        f"factory.talos.dev/metal-installer/{schematic_id}:"
        f"{settings['TALOS_VERSION']}"
    )
    gpu_installer_image = (
        f"factory.talos.dev/metal-installer/{gpu_schematic_id}:"
        f"{settings['TALOS_VERSION']}"
    )
    print(f"==> Base schematic: {schematic_id}")
    print(f"==> GPU schematic : {gpu_schematic_id}")

    render_netbird_patch()
    render_install_patch(installer_image, settings["INSTALL_DISK_SELECTOR"])

    secrets_file = GENERATED / "secrets.yaml"
    if not secrets_file.is_file():
        print("==> Creating new cluster secrets bundle (PKI)")
        common.run([talosctl, "gen", "secrets", "-o", secrets_file])
    else:
        print(f"==> Reusing existing secrets bundle {secrets_file}")

    base_dir = generate_base_config(talosctl, settings, installer_image)
    generate_node_configs(
        talosctl,
        base_dir,
        workers,
        gpu_nodes,
        gpu_installer_image,
    )
    generate_profile_configs(talosctl, base_dir)
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
