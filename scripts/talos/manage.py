#!/usr/bin/env python3
"""Manage the Talos cluster.

Usage: ./scripts/talos/manage.py <command> [options]

Cluster lifecycle:
  status                     Version, etcd members/status and machine status
  version [node...]          Client and per-node Talos versions
  apply <node> [address]     Apply the generated config to a node booted from the
                             ISO (maintenance mode, --insecure). <node> is a node
                             name or IP; [address] overrides the configured IP
                             (e.g. the DHCP address in maintenance mode).
  apply-all [node...]        Apply configs to all (or the given) nodes
  update [node...]           Apply regenerated configs to running nodes
                             (--workers-first, --mode, --dry-run, --no-wait)
  bootstrap                  Bootstrap etcd on the first control-plane node
  kubeconfig [path]          Write the admin kubeconfig (default generated/kubeconfig)
  health                     Run talosctl health
  wait [node...]             Wait until the Talos API answers on every node

Day-2 operations:
  services <node> [svc] [action]   List services or control one service
  logs <node> <service>            Show service logs (--follow to stream)
  dmesg <node>                     Show the kernel log
  links <node>                     List network links (names, MACs)
  disks <node>                     List disks (for disk selectors)
  mounts <node>                    Show user-volume mount status (Longhorn)
  roles                            Label nodes: control planes get all roles,
                                   workers get the worker role only
  cordon <node...>                 Mark Kubernetes nodes unschedulable
  uncordon <node...>               Mark Kubernetes nodes schedulable again
  drain <node> [args...]           Cordon and evict workloads from a node
                                   (defaults: --ignore-daemonsets
                                   --delete-emptydir-data --timeout=300s)
  reboot <node...>                 Drain and reboot Talos nodes, then wait
  shutdown <node...>               Gracefully shut down Talos nodes
                                   (Talos cordons/drains first)
  patch <node> <file>              Apply a machine config patch to a running node
  netbird [node...]                Re-render and apply the Netbird config
  vip on|off [node...]             Add/remove the Layer2 VIP on the control planes

Maintenance and upgrades:
  etcd [members|status|snapshot] [path]   etcd membership, health and snapshots
  wipe <node> <disk>               Wipe a disk (e.g. /dev/sdb), removing old partitions
  reset <node>                     Reset a node (graceful shutdown + wipe)
  upgrade-k8s <version>            Upgrade Kubernetes (e.g. 1.37.1)
  upgrade-talos [version]          Upgrade Talos (default: TALOS_VERSION)

Commands that access a node in maintenance mode accept --insecure (links, disks,
mounts, services, logs, dmesg, wipe). Talos settings are read from
talos/settings.env; TALOSCTL, KUBECTL, TALOSCONFIG, KUBECONFIG and
TALOS_ENDPOINTS can be overridden in the environment. TALOS_ENDPOINTS is a
comma-separated list and defaults to the control-plane addresses (workers cannot
forward Talos API requests).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent  # repository/scripts/talos
REPO_ROOT = SCRIPT_DIR.parents[1]
TALOS_DIR = REPO_ROOT / "talos"
GENERATED = TALOS_DIR / "generated"
SECRETS = TALOS_DIR / "secrets"
PATCHES = TALOS_DIR / "patches"
SETTINGS_FILE = TALOS_DIR / "settings.env"
TOOLS_DIR = REPO_ROOT / "scripts" / "bin"

# Behave like a normal Unix CLI when the consumer of our output goes away
# (e.g. `manage.py status | head`), instead of raising BrokenPipeError.
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

SETTINGS: dict[str, str] = {}
NODES: list["Node"] = []
WORKERS: set[str] = set()
CONTROL_PLANES: list["Node"] = []
BOOTSTRAP: "Node | None" = None
_TALOSCTL: str | None = None
_KUBECTL: str | None = None


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
        die("TALOS_NODES is empty in settings.env")

    return nodes


def node_by_ref(ref: str) -> Node | None:
    for node in NODES:
        if ref == node.name or ref == node.address:
            return node
    return None


def resolve_node(ref: str, address: str | None = None) -> Node:
    """Resolve a node name/IP, falling back to treating ref as an address."""
    node = node_by_ref(ref)
    if node is not None:
        return Node(node.name, address or node.address)
    return Node(ref, address or ref)


def expand_refs(refs: list[str] | None) -> list[str]:
    """Expand comma-separated node references, keeping the order."""
    expanded: list[str] = []
    for ref in refs or []:
        for part in ref.split(","):
            part = part.strip()
            if part:
                expanded.append(part)
    return expanded


def resolve_nodes(
    refs: list[str] | None, default: list[Node] | None = None
) -> list[Node]:
    expanded = expand_refs(refs)
    if not expanded:
        return list(default or [])
    return [resolve_node(ref) for ref in expanded]


def resolve_names(refs: list[str] | None) -> list[str]:
    return [resolve_node(ref).name for ref in expand_refs(refs)]


def load_config() -> None:
    global SETTINGS, NODES, WORKERS, CONTROL_PLANES, BOOTSTRAP

    SETTINGS = load_env_file(SETTINGS_FILE)
    NODES = parse_node_table(SETTINGS.get("TALOS_NODES", ""))

    try:
        worker_names = shlex.split(SETTINGS.get("TALOS_WORKERS", ""))
    except ValueError as exc:
        die(f"cannot parse TALOS_WORKERS: {exc}")

    known_names = {node.name for node in NODES}
    unknown_workers = sorted(set(worker_names) - known_names)
    if unknown_workers:
        die(f"TALOS_WORKERS contains unknown node(s): {', '.join(unknown_workers)}")

    WORKERS = set(worker_names)
    CONTROL_PLANES = [node for node in NODES if node.name not in WORKERS]
    if not CONTROL_PLANES:
        die("no control-plane nodes found (TALOS_WORKERS covers every node)")

    raw_bootstrap = SETTINGS.get("BOOTSTRAP_NODE", "").strip()
    if not raw_bootstrap:
        BOOTSTRAP = CONTROL_PLANES[0]
    else:
        name, separator, address = raw_bootstrap.partition("=")
        found = node_by_ref(name if separator else raw_bootstrap)
        if found is not None:
            BOOTSTRAP = Node(found.name, address or found.address)
        else:
            BOOTSTRAP = Node(name if separator else raw_bootstrap,
                             address or (name if separator else raw_bootstrap))


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    if not env.get("TALOSCONFIG"):
        env["TALOSCONFIG"] = str(GENERATED / "talosconfig")
    if not env.get("KUBECONFIG"):
        env["KUBECONFIG"] = str(GENERATED / "kubeconfig")
    return env


def run(
    cmd: list[str | Path],
    *,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in cmd]
    # Keep our own progress messages in order with the child's output.
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        result = subprocess.run(
            argv,
            env=subprocess_env(),
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
        # The consumer of our output went away (e.g. `manage.py disks ... | head`).
        # The child gets SIGPIPE; this is not an error worth reporting.
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


def talosctl() -> str:
    global _TALOSCTL
    if _TALOSCTL is not None:
        return _TALOSCTL

    override = os.environ.get("TALOSCTL")
    if override:
        _TALOSCTL = override
        return _TALOSCTL

    # Prefer the pinned binary from scripts/bin: it matches TALOS_VERSION and
    # the running nodes, avoiding client/server version skew from a talosctl in
    # PATH.
    local = TOOLS_DIR / "talosctl"
    if local.is_file() and os.access(local, os.X_OK):
        _TALOSCTL = str(local)
        return _TALOSCTL

    found = shutil.which("talosctl")
    if found:
        _TALOSCTL = found
        return _TALOSCTL

    die(
        "talosctl not found "
        "(run ./scripts/download-tools.py talosctl or set TALOSCTL)"
    )


def kubectl() -> str:
    global _KUBECTL
    if _KUBECTL is not None:
        return _KUBECTL

    override = os.environ.get("KUBECTL")
    if override:
        _KUBECTL = override
        return _KUBECTL

    local = TOOLS_DIR / "kubectl"
    if local.is_file() and os.access(local, os.X_OK):
        _KUBECTL = str(local)
        return _KUBECTL

    found = shutil.which("kubectl")
    if found:
        _KUBECTL = found
        return _KUBECTL

    die(
        "kubectl not found "
        "(run ./scripts/download-tools.py kubectl or set KUBECTL)"
    )


def node_addresses(nodes: Node | list[Node] | None) -> list[str]:
    if nodes is None:
        return []
    if isinstance(nodes, Node):
        return [nodes.address]
    return [node.address for node in nodes]


def talos_endpoints() -> list[str]:
    """Talos API endpoints; workers cannot forward requests to other nodes."""
    override = os.environ.get("TALOS_ENDPOINTS")
    if override is not None:
        return [endpoint.strip() for endpoint in override.split(",") if endpoint.strip()]
    return [node.address for node in CONTROL_PLANES]


def run_talos(
    args: list[str | Path],
    *,
    nodes: Node | list[Node] | None = None,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    cmd: list[str | Path] = [talosctl()]
    addresses = node_addresses(nodes)
    if addresses:
        cmd.extend(["-n", ",".join(addresses)])

    # Maintenance-mode commands target the node directly and must not use the
    # cluster endpoints. For authenticated commands, explicitly use the control
    # planes so a stale talosconfig with worker endpoints cannot cause
    # "PermissionDenied: no request forwarding".
    if "--insecure" not in args:
        endpoints = talos_endpoints()
        if endpoints:
            cmd.extend(["-e", ",".join(endpoints)])

    cmd.extend(args)
    return run(cmd, **kwargs)


def run_kubectl(
    args: list[str | Path], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    return run([kubectl(), *args], **kwargs)


@contextlib.contextmanager
def temporary_patch(text: str):
    fd, path = tempfile.mkstemp(prefix="talos-patch-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o600)
        yield Path(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def wait_for_nodes(
    nodes: list[Node], timeout: float, interval: float = 5.0
) -> None:
    """Wait until talosctl can talk to every node."""
    deadline = time.monotonic() + timeout
    pending = list(nodes)

    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            names = ", ".join(f"{node.name} ({node.address})" for node in pending)
            die(f"timed out after {timeout:.0f}s waiting for: {names}")

        still_pending: list[Node] = []
        for node in pending:
            remaining = max(0.1, deadline - time.monotonic())
            probe = run_talos(
                ["version", "--short"],
                nodes=node,
                check=False,
                capture=True,
                quiet=True,
                timeout=min(10.0, remaining),
            )
            if probe.returncode == 0:
                print(f"==> {node.name} ({node.address}) is up")
            else:
                still_pending.append(node)

        pending = still_pending
        if pending:
            time.sleep(min(interval, max(0.1, deadline - time.monotonic())))


def normalize_k8s_version(raw: str) -> str:
    version = raw.strip()
    if version.startswith("v"):
        version = version[1:]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        die(f"invalid Kubernetes version {raw!r} (expected e.g. 1.37.1)")
    return version


def normalize_talos_version(raw: str) -> str:
    version = raw.strip()
    if not version.startswith("v"):
        version = f"v{version}"
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        die(f"invalid Talos version {raw!r} (expected e.g. v1.14.3)")
    return version


def config_path_for(name: str) -> Path:
    return GENERATED / f"{name}.yaml"


def apply_node(node: Node, config: Path, mode: str | None = None) -> None:
    if not config.is_file():
        die(f"{config} not found (run ./scripts/talos/gen-config.py)")

    print(f"==> Applying {config} to {node.name} ({node.address}) "
          "(maintenance mode)")
    cmd: list[str | Path] = [
        "apply-config",
        "--insecure",
        "--nodes",
        node.address,
        "--file",
        config,
    ]
    if mode:
        cmd.extend(["--mode", mode])
    run_talos(cmd)


def cmd_status(_args: argparse.Namespace) -> int:
    return_code = 0

    print("==> Talos version")
    client = run_talos(
        ["version", "--client", "--short"],
        check=False,
        capture=True,
        quiet=True,
    )
    if client.returncode == 0 and client.stdout.strip():
        print(client.stdout.rstrip())
    else:
        return_code |= client.returncode
    result = run_talos(["get", "version"], nodes=NODES, check=False)
    return_code |= result.returncode

    print("\n==> Etcd members")
    result = run_talos(["etcd", "members"], nodes=BOOTSTRAP, check=False)
    return_code |= result.returncode

    print("\n==> Etcd status")
    result = run_talos(["etcd", "status"], nodes=CONTROL_PLANES, check=False)
    return_code |= result.returncode

    print("\n==> Machine status")
    result = run_talos(["get", "machinestatus"], nodes=NODES, check=False)
    return_code |= result.returncode

    return 1 if return_code else 0


def cmd_version(args: argparse.Namespace) -> int:
    refs = expand_refs(args.nodes)
    if not refs:
        return run_talos(["version"]).returncode

    return_code = 0
    for ref in refs:
        node = resolve_node(ref)
        print(f"==> {node.name} ({node.address})")
        result = run_talos(["version"], nodes=node, check=False)
        return_code |= result.returncode
        print()
    return 1 if return_code else 0


def cmd_apply(args: argparse.Namespace) -> int:
    node = node_by_ref(args.node)
    if node is not None:
        target = Node(node.name, args.address or node.address)
    else:
        target = Node(args.node, args.address or args.node)

    if args.config:
        config = Path(args.config).expanduser()
    else:
        config = config_path_for(target.name)

    if not config.is_file():
        known = ", ".join(node.name for node in NODES)
        die(
            f"{config} not found (run ./scripts/talos/gen-config.py; "
            f"known nodes: {known}). "
            "For an unknown address, use: apply <config-name> <address>"
        )

    apply_node(target, config, args.mode)
    return 0


def cmd_apply_all(args: argparse.Namespace) -> int:
    nodes = resolve_nodes(args.nodes, NODES)
    missing = [
        str(config_path_for(node.name))
        for node in nodes
        if not config_path_for(node.name).is_file()
    ]
    if missing:
        die("missing generated config(s): " + ", ".join(missing))

    for node in nodes:
        apply_node(node, config_path_for(node.name))
    return 0


def update_node(
    node: Node,
    config: Path,
    *,
    mode: str | None,
    dry_run: bool,
) -> None:
    if not config.is_file():
        die(f"{config} not found (run ./scripts/talos/gen-config.py)")

    print(f"==> Updating {node.name} ({node.address}) from {config}")
    cmd: list[str | Path] = [
        "apply-config",
        "--nodes",
        node.address,
        "--file",
        config,
    ]
    if mode:
        cmd.extend(["--mode", mode])
    if dry_run:
        cmd.append("--dry-run")
    run_talos(cmd)


def cmd_update(args: argparse.Namespace) -> int:
    nodes = resolve_nodes(args.nodes, NODES)
    if args.workers_first:
        order = {node.name: index for index, node in enumerate(NODES)}
        nodes.sort(
            key=lambda node: (
                node.name not in WORKERS,
                order.get(node.name, 10**9),
            )
        )

    missing = [
        str(config_path_for(node.name))
        for node in nodes
        if not config_path_for(node.name).is_file()
    ]
    if missing:
        die("missing generated config(s): " + ", ".join(missing))

    for node in nodes:
        update_node(
            node,
            config_path_for(node.name),
            mode=args.mode,
            dry_run=args.dry_run,
        )
        if not args.no_wait and not args.dry_run:
            print(f"==> Waiting for {node.name} to come back")
            wait_for_nodes([node], args.timeout)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    if not args.force:
        candidates = [BOOTSTRAP] + [
            node for node in CONTROL_PLANES if node.name != BOOTSTRAP.name
        ]
        for candidate in candidates:
            probe = run_talos(
                ["etcd", "members"],
                nodes=candidate,
                check=False,
                capture=True,
                quiet=True,
                timeout=10,
            )
            if probe.returncode == 0:
                print(
                    "==> etcd is already bootstrapped "
                    f"(answered by {candidate.name}); use --force to bootstrap anyway"
                )
                return 0

    cmd: list[str | Path] = ["bootstrap"]
    if args.recover_from:
        snapshot = Path(args.recover_from).expanduser()
        if not snapshot.is_file():
            die(f"snapshot not found: {snapshot}")
        cmd.extend(["--recover-from", snapshot])
        if args.recover_skip_hash_check:
            cmd.append("--recover-skip-hash-check")

    print(f"==> Bootstrapping etcd on {BOOTSTRAP.name} ({BOOTSTRAP.address})")
    run_talos(cmd, nodes=BOOTSTRAP)
    print(
        "==> Bootstrapped. Next: ./scripts/talos/manage.py kubeconfig "
        "&& ./scripts/talos/manage.py health"
    )
    return 0


def cmd_kubeconfig(args: argparse.Namespace) -> int:
    output = Path(args.path).expanduser() if args.path else GENERATED / "kubeconfig"
    if str(output) != "-":
        output.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str | Path] = ["kubeconfig", output, "--force"]
    cmd.append("--merge=true" if args.merge else "--merge=false")
    run_talos(cmd)

    if str(output) != "-":
        print(f"==> Wrote {output}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    cmd = ["health"]
    if not args.server:
        cmd.append("--server=false")
    if args.wait_timeout:
        cmd.append(f"--wait-timeout={args.wait_timeout}")
    run_talos(cmd)
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    nodes = resolve_nodes(args.nodes, NODES)
    print(f"==> Waiting up to {args.timeout:.0f}s for {len(nodes)} node(s)")
    wait_for_nodes(nodes, args.timeout, args.interval)
    return 0


def cmd_services(args: argparse.Namespace) -> int:
    node = resolve_node(args.node)
    cmd = ["service"]
    if args.service:
        cmd.append(args.service)
    if args.action:
        if not args.service:
            die("an action requires a service name")
        cmd.append(args.action)
    if args.insecure:
        cmd.append("--insecure")
    run_talos(cmd, nodes=node)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    node = resolve_node(args.node)
    cmd = ["logs", args.service]
    if args.follow:
        cmd.append("--follow")
    if args.tail is not None:
        cmd.append(f"--tail={args.tail}")
    if args.namespace:
        cmd.append(f"--namespace={args.namespace}")
    if args.insecure:
        cmd.append("--insecure")
    run_talos(cmd, nodes=node)
    return 0


def cmd_dmesg(args: argparse.Namespace) -> int:
    node = resolve_node(args.node)
    cmd = ["dmesg"]
    if args.follow:
        cmd.append("--follow")
    if args.tail:
        cmd.append("--tail")
    if args.insecure:
        cmd.append("--insecure")
    run_talos(cmd, nodes=node)
    return 0


def _get_resource(args: argparse.Namespace, resource: str) -> int:
    node = resolve_node(args.node)
    cmd = ["get", resource]
    if args.insecure:
        cmd.append("--insecure")
    run_talos(cmd, nodes=node)
    return 0


def cmd_links(args: argparse.Namespace) -> int:
    return _get_resource(args, "links")


def cmd_disks(args: argparse.Namespace) -> int:
    return _get_resource(args, "disks")


def cmd_mounts(args: argparse.Namespace) -> int:
    return _get_resource(args, "mountstatus")


def cmd_roles(_args: argparse.Namespace) -> int:
    result = run_kubectl(["get", "nodes", "-o", "name"], capture=True)
    existing = {
        line.strip().removeprefix("node/")
        for line in result.stdout.splitlines()
        if line.strip()
    }

    control_planes = [node for node in CONTROL_PLANES if node.name in existing]
    workers = [node for node in NODES if node.name in WORKERS and node.name in existing]

    for node in CONTROL_PLANES:
        if node.name not in existing:
            print(f"==> {node.name}: not in the cluster yet, skipping")
    for node in NODES:
        if node.name in WORKERS and node.name not in existing:
            print(f"==> {node.name}: not in the cluster yet, skipping")

    print("==> control-plane nodes (control-plane + etcd + worker)")
    if control_planes:
        run_kubectl(
            [
                "label",
                "node",
                *[node.name for node in control_planes],
                "node-role.kubernetes.io/control-plane=",
                "node-role.kubernetes.io/etcd=",
                "node-role.kubernetes.io/worker=",
                "--overwrite",
            ]
        )

    print("==> worker nodes (worker role only)")
    for node in workers:
        run_kubectl(
            [
                "label",
                "node",
                node.name,
                "node-role.kubernetes.io/worker=",
                "--overwrite",
            ]
        )
        # Remove stray control-plane/etcd labels one by one. A label may not
        # exist, which makes kubectl exit non-zero; that is expected here.
        for label in (
            "node-role.kubernetes.io/etcd-",
            "node-role.kubernetes.io/control-plane-",
        ):
            run_kubectl(
                ["label", "node", node.name, label],
                check=False,
                capture=True,
                quiet=True,
            )
    return 0


def cmd_cordon(args: argparse.Namespace) -> int:
    run_kubectl(["cordon", *resolve_names(args.nodes)])
    return 0


def cmd_uncordon(args: argparse.Namespace) -> int:
    run_kubectl(["uncordon", *resolve_names(args.nodes)])
    return 0


def cmd_drain(args: argparse.Namespace) -> int:
    node = resolve_node(args.node).name
    extra = list(args.kubectl_args)

    defaults: list[str] = []
    if not any(arg == "--ignore-daemonsets" for arg in extra):
        defaults.append("--ignore-daemonsets")
    if not any(arg == "--delete-emptydir-data" for arg in extra):
        defaults.append("--delete-emptydir-data")
    if not any(arg.startswith("--timeout") for arg in extra):
        defaults.append("--timeout=300s")

    print(f"==> Draining {node} (cordon + evict)")
    run_kubectl(["drain", node, *defaults, *extra])
    return 0


def cmd_reboot(args: argparse.Namespace) -> int:
    for node in resolve_nodes(args.nodes, NODES):
        print(f"==> Rebooting {node.name} ({node.address})")
        cmd = ["reboot"]
        if not args.no_drain:
            cmd.append("--drain")
        if args.mode:
            cmd.append(f"--mode={args.mode}")
        run_talos(cmd, nodes=node)
        if not args.no_wait:
            wait_for_nodes([node], args.timeout)
    return 0


def cmd_shutdown(args: argparse.Namespace) -> int:
    for node in resolve_nodes(args.nodes, NODES):
        print(f"==> Shutting down {node.name} ({node.address})")
        cmd = ["shutdown"]
        if args.force:
            cmd.append("--force")
        run_talos(cmd, nodes=node)
    return 0


def cmd_patch(args: argparse.Namespace) -> int:
    node = resolve_node(args.node)
    patch_file = Path(args.file).expanduser()
    if not patch_file.is_file():
        die(f"patch file not found: {patch_file}")

    cmd: list[str | Path] = [
        "patch",
        "machineconfig",
        "--patch",
        f"@{patch_file}",
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.mode:
        cmd.extend(["--mode", args.mode])
    if args.timeout:
        cmd.extend(["--timeout", args.timeout])

    print(f"==> Patching {node.name} ({node.address}) with {patch_file}")
    run_talos(cmd, nodes=node)
    return 0


def cmd_netbird(args: argparse.Namespace) -> int:
    netbird_env = load_env_file(SECRETS / "netbird.env")
    setup_key = netbird_env.get("NB_SETUP_KEY", "")
    if not setup_key:
        die(f"NB_SETUP_KEY is empty in {SECRETS / 'netbird.env'}")

    template = PATCHES / "netbird.yaml.tpl"
    if not template.is_file():
        die(f"template not found: {template}")
    if any(character in setup_key for character in "\r\n"):
        die("NB_SETUP_KEY contains a newline")

    template_text = template.read_text(encoding="utf-8")
    placeholder = "NB_SETUP_KEY=@NB_SETUP_KEY@"
    if placeholder not in template_text:
        die(f"{template} does not contain the {placeholder} placeholder")

    rendered = template_text.replace(placeholder, f"NB_SETUP_KEY={setup_key}")
    nodes = resolve_nodes(args.nodes, NODES)

    with temporary_patch(rendered) as patch_file:
        for node in nodes:
            print(f"==> Netbird config -> {node.name} ({node.address})")
            run_talos(
                ["patch", "machineconfig", "--patch", f"@{patch_file}"],
                nodes=node,
            )
    return 0


def cmd_vip(args: argparse.Namespace) -> int:
    vip = SETTINGS.get("CLUSTER_VIP", "").strip()
    if not vip:
        die("CLUSTER_VIP is not set in settings.env")

    nodes = resolve_nodes(args.nodes, CONTROL_PLANES)
    if args.action == "on":
        patch_text = (
            "apiVersion: v1alpha1\n"
            "kind: Layer2VIPConfig\n"
            f"name: {vip}\n"
            f"link: {args.link}\n"
        )
    else:
        patch_text = (
            "apiVersion: v1alpha1\n"
            "kind: Layer2VIPConfig\n"
            f"name: {vip}\n"
            "$patch: delete\n"
        )

    with temporary_patch(patch_text) as patch_file:
        for node in nodes:
            print(f"==> VIP {args.action}: {node.name} ({node.address})")
            cmd: list[str | Path] = [
                "patch",
                "machineconfig",
                "--patch",
                f"@{patch_file}",
            ]
            if args.dry_run:
                cmd.append("--dry-run")
            run_talos(cmd, nodes=node)
    return 0


def cmd_wipe(args: argparse.Namespace) -> int:
    node = resolve_node(args.node)
    device = args.disk
    # talosctl wipe disk expects a device name (sdb), not a /dev path.
    if device.startswith("/dev/") and "/" not in device[len("/dev/") :]:
        device = device[len("/dev/") :]

    cmd: list[str | Path] = ["wipe", "disk", device, "--method", args.method]
    if not args.no_drop_partition:
        cmd.append("--drop-partition")
    if args.insecure:
        cmd.append("--insecure")

    print(f"==> Wiping {device} on {node.name} ({node.address})")
    run_talos(cmd, nodes=node)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    node = resolve_node(args.node)
    cmd = ["reset", "--graceful=false" if args.no_graceful else "--graceful"]
    if args.reboot:
        cmd.append("--reboot")
    if args.wipe_mode:
        cmd.append(f"--wipe-mode={args.wipe_mode}")
    for label in args.system_labels_to_wipe or []:
        cmd.extend(["--system-labels-to-wipe", label])
    for disk in args.user_disks_to_wipe or []:
        cmd.extend(["--user-disks-to-wipe", disk])

    print(f"==> Resetting {node.name} ({node.address})")
    run_talos(cmd, nodes=node)
    return 0


def cmd_etcd(args: argparse.Namespace) -> int:
    if args.action == "members":
        if args.path:
            die("'etcd members' does not take a path")
        nodes = resolve_nodes(args.nodes, [BOOTSTRAP])
        run_talos(["etcd", "members"], nodes=nodes)
        return 0

    if args.action == "status":
        if args.path:
            die("'etcd status' does not take a path")
        nodes = resolve_nodes(args.nodes, CONTROL_PLANES)
        run_talos(["etcd", "status"], nodes=nodes)
        return 0

    # snapshot
    if args.nodes:
        die("'etcd snapshot' takes --node, not --nodes")
    node = resolve_node(args.node) if args.node else BOOTSTRAP
    snapshot = (
        Path(args.path).expanduser()
        if args.path
        else GENERATED / f"etcd-snapshot-{datetime.now():%Y%m%d-%H%M%S}.db"
    )
    if str(snapshot) != "-":
        snapshot.parent.mkdir(parents=True, exist_ok=True)

    print(f"==> Saving etcd snapshot from {node.name} to {snapshot}")
    run_talos(["etcd", "snapshot", snapshot], nodes=node)
    return 0


def cmd_upgrade_k8s(args: argparse.Namespace) -> int:
    version = normalize_k8s_version(args.version)
    cmd = [
        "upgrade-k8s",
        "--to",
        version,
        "--with-docs=false",
        "--with-examples=false",
    ]
    if args.dry_run:
        cmd.append("--dry-run")

    print(f"==> Upgrading Kubernetes to {version}")
    run_talos(cmd)
    return 0


def cmd_upgrade_talos(args: argparse.Namespace) -> int:
    version = normalize_talos_version(args.version or SETTINGS.get("TALOS_VERSION", ""))
    schematic_file = GENERATED / "schematic-id"
    if not schematic_file.is_file():
        die(f"{schematic_file} not found (run ./scripts/talos/gen-iso.py first)")
    schematic = schematic_file.read_text(encoding="utf-8").strip()
    if not schematic:
        die(f"{schematic_file} is empty (run ./scripts/talos/gen-iso.py)")

    image = f"factory.talos.dev/metal-installer/{schematic}:{version}"
    nodes = resolve_nodes(args.nodes, NODES)
    if args.workers_first:
        order = {node.name: index for index, node in enumerate(NODES)}
        nodes.sort(key=lambda node: (node.name not in WORKERS, order.get(node.name, 10**9)))

    for node in nodes:
        print(f"==> Upgrading {node.name} to {version}")
        run_talos(["upgrade", "--image", image], nodes=node)
        print(f"==> Waiting for {node.name} to come back")
        wait_for_nodes([node], args.node_timeout)
        if not args.no_health:
            run_talos(
                ["health", "--server=false", f"--wait-timeout={args.health_timeout}"],
                nodes=node,
            )
    return 0


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Manage the Talos cluster.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ./scripts/talos/manage.py apply glemmen60 192.168.6.61\n"
            "  ./scripts/talos/manage.py update --workers-first --mode auto\n"
            "  ./scripts/talos/manage.py logs glemmen130 etcd --follow\n"
            "  ./scripts/talos/manage.py etcd snapshot\n"
            "  ./scripts/talos/manage.py vip off glemmen170 glemmen180\n"
            "  ./scripts/talos/manage.py upgrade-talos v1.14.3\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(
        name: str, func, help_text: str, **kwargs: object
    ) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text, **kwargs)
        command.set_defaults(func=func)
        return command

    add("status", cmd_status, "Version, etcd members/status and machine status")

    p = add("version", cmd_version, "Client and per-node Talos versions")
    p.add_argument("nodes", nargs="*", metavar="node")

    p = add("apply", cmd_apply,
            "Apply a generated config to a node in maintenance mode")
    p.add_argument("node", help="node name or IP")
    p.add_argument("address", nargs="?",
                   help="override the configured address (e.g. DHCP address)")
    p.add_argument("--config", help="explicit machine config file")
    p.add_argument("--mode", choices=("auto", "no-reboot", "staged", "try"))

    p = add("apply-all", cmd_apply_all,
            "Apply generated configs to all (or the given) nodes")
    p.add_argument("nodes", nargs="*", metavar="node")

    p = add("update", cmd_update,
            "Apply regenerated configs to running nodes")
    p.add_argument("nodes", nargs="*", metavar="node")
    p.add_argument("--workers-first", action="store_true",
                   help="update worker nodes before control planes")
    p.add_argument("--mode", choices=("auto", "no-reboot", "staged", "try"),
                   help="talosctl apply-config mode (default: auto)")
    p.add_argument("--dry-run", action="store_true",
                   help="show the config diff without applying it")
    p.add_argument("--no-wait", action="store_true",
                   help="do not wait for each node to come back")
    p.add_argument("--timeout", type=positive_float, default=600.0,
                   help="seconds to wait for each node (default: 600)")

    p = add("bootstrap", cmd_bootstrap, "Bootstrap etcd on the first node")
    p.add_argument("--force", action="store_true",
                   help="bootstrap even if etcd already answers")
    p.add_argument("--recover-from", help="recover etcd from a snapshot file")
    p.add_argument("--recover-skip-hash-check", action="store_true",
                   help="skip the snapshot integrity check")

    p = add("kubeconfig", cmd_kubeconfig, "Write the admin kubeconfig")
    p.add_argument("path", nargs="?",
                   help="output path (default: generated/kubeconfig)")
    p.add_argument("--merge", action="store_true",
                   help="merge into the existing kubeconfig instead of replacing")

    p = add("health", cmd_health, "Run talosctl health")
    p.add_argument("--server", action="store_true",
                   help="run the server-side health check (default: client-side)")
    p.add_argument("--wait-timeout", metavar="DURATION",
                   help="how long to wait (e.g. 10m)")

    p = add("wait", cmd_wait, "Wait until the Talos API answers on every node")
    p.add_argument("nodes", nargs="*", metavar="node")
    p.add_argument("--timeout", type=positive_float, default=600.0,
                   help="seconds to wait (default: 600)")
    p.add_argument("--interval", type=positive_float, default=5.0,
                   help="seconds between probes (default: 5)")

    p = add("services", cmd_services, "List or control Talos services on a node")
    p.add_argument("node")
    p.add_argument("service", nargs="?")
    p.add_argument("action", nargs="?",
                   choices=("start", "stop", "restart", "status"))
    p.add_argument("--insecure", action="store_true",
                   help="talk to a node in maintenance mode")

    p = add("logs", cmd_logs, "Show service logs from a node")
    p.add_argument("node")
    p.add_argument("service")
    p.add_argument("-f", "--follow", action="store_true", help="stream the logs")
    p.add_argument("--tail", type=int, metavar="LINES")
    p.add_argument("--namespace", choices=("system", "cri", "taloscontainers"))
    p.add_argument("--insecure", action="store_true",
                   help="talk to a node in maintenance mode")

    p = add("dmesg", cmd_dmesg, "Show the kernel log of a node")
    p.add_argument("node")
    p.add_argument("-f", "--follow", action="store_true", help="stream the log")
    p.add_argument("--tail", action="store_true",
                   help="only new messages (with --follow)")
    p.add_argument("--insecure", action="store_true",
                   help="talk to a node in maintenance mode")

    for name, func, resource in (
        ("links", cmd_links, "network links (names, MACs)"),
        ("disks", cmd_disks, "disks (for disk selectors)"),
        ("mounts", cmd_mounts, "user-volume mount status"),
    ):
        p = add(name, func, f"List {resource} on a node")
        p.add_argument("node")
        p.add_argument("--insecure", action="store_true",
                       help="talk to a node in maintenance mode")

    add("roles", cmd_roles, "Label control-plane and worker nodes")

    p = add("cordon", cmd_cordon, "Mark Kubernetes nodes unschedulable")
    p.add_argument("nodes", nargs="+", metavar="node")

    p = add("uncordon", cmd_uncordon, "Mark Kubernetes nodes schedulable again")
    p.add_argument("nodes", nargs="+", metavar="node")

    p = add("drain", cmd_drain, "Cordon and evict workloads from a node")
    p.add_argument("node")
    p.add_argument("kubectl_args", nargs=argparse.REMAINDER,
                   help="extra flags passed to kubectl drain")

    p = add("reboot", cmd_reboot, "Drain and reboot Talos nodes")
    p.add_argument("nodes", nargs="+", metavar="node")
    p.add_argument("--no-drain", action="store_true",
                   help="do not cordon/drain before rebooting")
    p.add_argument("--no-wait", action="store_true",
                   help="do not wait for the node to come back")
    p.add_argument("--timeout", type=positive_float, default=600.0,
                   help="seconds to wait for each node (default: 600)")
    p.add_argument("--mode", choices=("default", "force", "powercycle"))

    p = add("shutdown", cmd_shutdown, "Gracefully shut down Talos nodes")
    p.add_argument("nodes", nargs="+", metavar="node")
    p.add_argument("--force", action="store_true",
                   help="shut down without cordoning/draining")

    p = add("patch", cmd_patch, "Apply a machine config patch to a running node")
    p.add_argument("node")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true", help="show the patch only")
    p.add_argument("--mode", choices=("auto", "no-reboot", "staged", "try"))
    p.add_argument("--timeout", help="try-mode rollback timeout (e.g. 1m)")

    p = add("netbird", cmd_netbird,
            "Re-render and apply the Netbird config to all nodes")
    p.add_argument("nodes", nargs="*", metavar="node")

    p = add("vip", cmd_vip, "Add or remove the Layer2 VIP")
    p.add_argument("action", choices=("on", "off"))
    p.add_argument("nodes", nargs="*", metavar="node")
    p.add_argument("--link", default="bond0",
                   help="interface to advertise the VIP on (default: bond0)")
    p.add_argument("--dry-run", action="store_true", help="show the patch only")

    p = add("wipe", cmd_wipe, "Wipe a disk, removing old partitions")
    p.add_argument("node")
    p.add_argument("disk", help="disk, e.g. /dev/sdb or sdb")
    p.add_argument("--method", type=str.upper, choices=("FAST", "ZEROES"),
                   default="FAST")
    p.add_argument("--no-drop-partition", action="store_true",
                   help="keep the partition table")
    p.add_argument("--insecure", action="store_true",
                   help="talk to a node in maintenance mode")

    p = add("reset", cmd_reset, "Reset a node (graceful shutdown + wipe)")
    p.add_argument("node")
    p.add_argument("--reboot", action="store_true",
                   help="reboot after resetting instead of shutting down")
    p.add_argument("--no-graceful", action="store_true",
                   help="do not cordon/drain or leave etcd first")
    p.add_argument("--wipe-mode", choices=("all", "system-disk", "user-disks"))
    p.add_argument("--system-labels-to-wipe", nargs="+", metavar="LABEL")
    p.add_argument("--user-disks-to-wipe", nargs="+", metavar="DISK")

    p = add("etcd", cmd_etcd, "etcd membership, health and snapshots")
    p.add_argument("action", nargs="?", default="members",
                   choices=("members", "status", "snapshot"))
    p.add_argument("path", nargs="?",
                   help="snapshot path (for 'etcd snapshot')")
    p.add_argument("--nodes", nargs="+", metavar="node")
    p.add_argument("--node", help="single node (for 'etcd snapshot')")

    p = add("upgrade-k8s", cmd_upgrade_k8s, "Upgrade Kubernetes")
    p.add_argument("version", help="target version, e.g. 1.37.1")
    p.add_argument("--dry-run", action="store_true", help="show the upgrade plan")

    p = add("upgrade-talos", cmd_upgrade_talos, "Upgrade Talos")
    p.add_argument("version", nargs="?",
                   help="target version (default: TALOS_VERSION)")
    p.add_argument("--nodes", nargs="+", metavar="node",
                   help="upgrade only the given nodes")
    p.add_argument("--workers-first", action="store_true",
                   help="upgrade worker nodes before control planes")
    p.add_argument("--node-timeout", type=positive_float, default=900.0,
                   help="seconds to wait for each node (default: 900)")
    p.add_argument("--health-timeout", default="10m", metavar="DURATION",
                   help="health check timeout after each node (default: 10m)")
    p.add_argument("--no-health", action="store_true",
                   help="skip the cluster health check between nodes")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    load_config()

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
