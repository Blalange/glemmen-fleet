#!/usr/bin/env python3
"""Sync a node's Longhorn disk annotation with the live Longhorn Node CR.

Talos writes the desired disk layout to the Kubernetes node annotation
`node.longhorn.io/default-disks-config`. Longhorn only reads that annotation
when it first creates the default disks, so later changes must be applied to
`nodes.longhorn.io/<node>.spec.disks` as well.

Usage:
  ./scripts/talos/sync-longhorn-disks.py <node>... --from repo
  ./scripts/talos/sync-longhorn-disks.py <node>... --from annotation
  ./scripts/talos/sync-longhorn-disks.py <node>... --from longhorn
  ./scripts/talos/sync-longhorn-disks.py <node>... --check
  ./scripts/talos/sync-longhorn-disks.py --all --dry-run

Directions:
  --from repo (default)   annotation in talos/nodes/<node>.yaml -> Longhorn
  --from annotation       live Kubernetes node annotation -> Longhorn
  --from longhorn         live Longhorn Node CR -> live node annotation

The repo source is the desired state you edit; the live node annotation is
what Talos has applied so far and can lag behind until the config is applied.

Options:
  --check          print the diff; exit 1 if anything is out of sync
  --dry-run        print the diff and planned changes; apply nothing
  --all            use every node that has a Longhorn Node CR
  --allow-empty    allow an empty disk list (removes every disk)
  --repair-system-uuid
                   reset the Longhorn system disk status UUID from the
                   on-disk longhorn-disk.cfg (needed after replacing the
                   system disk; uses talosctl)

The annotation is owned by Talos's KubeNodeConfig. A write from
--from longhorn is reverted the next time the machine config is applied,
unless talos/nodes/<node>.yaml is updated as well.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import common  # noqa: E402

ANNOTATION = "node.longhorn.io/default-disks-config"
LONGHORN_NS = "longhorn-system"
FIELDS = (
    "path",
    "allowScheduling",
    "evictionRequested",
    "storageReserved",
    "tags",
    "diskType",
    "diskDriver",
)
DEFAULTS: dict[str, Any] = {
    "allowScheduling": True,
    "evictionRequested": False,
    "storageReserved": 0,
    "tags": [],
    "diskType": "filesystem",
    "diskDriver": "",
}


def kubectl_json(args: list[str]) -> Any:
    result = common.run(
        [common.require_tool("kubectl", "KUBECTL"), *args],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        common.die(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        common.die(f"kubectl {' '.join(args)} returned invalid JSON: {exc}")


def kubectl_patch(args: list[str], patch: dict[str, Any]) -> tuple[bool, str]:
    result = common.run(
        [common.require_tool("kubectl", "KUBECTL"), *args, "--type=merge",
         "-p", json.dumps(patch, separators=(",", ":"))],
        capture=True,
        check=False,
    )
    return result.returncode == 0, result.stderr.strip()


def normalize_disk(raw: Any, source: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        common.die(f"{source}: disk entry is not an object: {raw!r}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        common.die(f"{source}: disk entry has no name: {raw!r}")
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        common.die(f"{source}: disk {name!r} has no path")

    try:
        reserved = int(raw.get("storageReserved", DEFAULTS["storageReserved"]))
    except (TypeError, ValueError):
        common.die(f"{source}: disk {name!r} has an invalid storageReserved")

    tags = raw.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        common.die(f"{source}: disk {name!r} has invalid tags")

    return name, {
        "path": path,
        "allowScheduling": bool(raw.get("allowScheduling", DEFAULTS["allowScheduling"])),
        "evictionRequested": bool(
            raw.get("evictionRequested", DEFAULTS["evictionRequested"])
        ),
        "storageReserved": reserved,
        "tags": list(tags),
        "diskType": str(raw.get("diskType") or DEFAULTS["diskType"]),
        "diskDriver": str(raw.get("diskDriver") or DEFAULTS["diskDriver"]),
    }


def parse_annotation(raw: str, source: str) -> dict[str, dict[str, Any]]:
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        common.die(f"{source} is not valid JSON: {exc}")
    if not isinstance(entries, list):
        common.die(f"{source} is not a JSON array")

    disks: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name, disk = normalize_disk(entry, source)
        if name in disks:
            common.die(f"{source} has duplicate disk {name!r}")
        disks[name] = disk
    return disks


def read_live_annotation(node: str) -> dict[str, dict[str, Any]]:
    obj = kubectl_json(["get", "node", node, "-o", "json"])
    raw = (obj.get("metadata", {}).get("annotations") or {}).get(ANNOTATION)
    if raw is None:
        common.die(f"node/{node} has no {ANNOTATION} annotation")
    return parse_annotation(raw, f"node/{node} annotation")


def read_repo_annotation(node: str) -> dict[str, dict[str, Any]]:
    yaml = common.require_yaml()
    path = common.NODES_DIR / f"{node}.yaml"
    if not path.is_file():
        common.die(f"{path} not found")
    try:
        documents = [
            document
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if document
        ]
    except (OSError, ValueError) as exc:
        common.die(f"failed to read {path}: {exc}")

    for document in documents:
        if document.get("kind") != "KubeNodeConfig":
            continue
        raw = (document.get("annotations") or {}).get(ANNOTATION)
        if raw is None:
            common.die(f"{path} has no {ANNOTATION} annotation")
        return parse_annotation(raw, f"{path} annotation")
    common.die(f"{path} has no KubeNodeConfig document")


def read_longhorn(node: str) -> dict[str, dict[str, Any]]:
    obj = kubectl_json(
        ["-n", LONGHORN_NS, "get", "nodes.longhorn.io", node, "-o", "json"]
    )
    raw = (obj.get("spec") or {}).get("disks") or {}
    disks: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        key, disk = normalize_disk({**entry, "name": name}, f"nodes.longhorn.io/{node}")
        disks[key] = disk
    return disks


def talos_address(node: str) -> str:
    settings = common.load_talos_settings()
    for entry in common.parse_node_table(settings.get("TALOS_NODES", "")):
        if entry.name == node:
            return entry.address
    return node


def talos_read(address: str, path: str) -> str:
    result = common.run(
        [common.require_tool("talosctl", "TALOSCTL"), "-n", address, "read", path],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        common.die(
            f"talosctl read {path} on {address} failed: {result.stderr.strip()}"
        )
    return result.stdout


def longhorn_status_uuid(node: str, disk: str) -> str | None:
    obj = kubectl_json(
        ["-n", LONGHORN_NS, "get", "nodes.longhorn.io", node, "-o", "json"]
    )
    status = ((obj.get("status") or {}).get("diskStatus") or {}).get(disk) or {}
    value = status.get("diskUUID")
    return value if isinstance(value, str) and value else None


def patch_status_uuid(node: str, disk: str, uuid: str) -> bool:
    result = common.run(
        [
            common.require_tool("kubectl", "KUBECTL"),
            "-n", LONGHORN_NS, "patch", "nodes.longhorn.io", node,
            "--subresource=status", "--type=merge",
            "-p", json.dumps(
                {"status": {"diskStatus": {disk: {"diskUUID": uuid}}}},
                separators=(",", ":"),
            ),
        ],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"  ERROR: failed to reset {disk} UUID: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def repair_system_uuid(node: str, *, check: bool, dry_run: bool) -> bool:
    disks = read_longhorn(node)
    system = disks.get("system")
    if system is None:
        print(f"{node}: no system disk in the Longhorn spec", file=sys.stderr)
        return False

    path = f"{system['path'].rstrip('/')}/longhorn-disk.cfg"
    address = talos_address(node)
    try:
        raw = talos_read(address, path)
    except SystemExit:
        return False

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"{node}: {path} is not valid JSON: {exc}", file=sys.stderr)
        return False
    new_uuid = data.get("diskUUID")
    if not isinstance(new_uuid, str) or not new_uuid:
        print(f"{node}: {path} has no diskUUID", file=sys.stderr)
        return False

    old_uuid = longhorn_status_uuid(node, "system")
    if old_uuid == new_uuid:
        print(f"{node}: system UUID OK ({new_uuid})")
        return True
    if check:
        print(
            f"{node}: system UUID {old_uuid or '<none>'} -> {new_uuid} "
            "(would reset)"
        )
        return False
    if dry_run:
        print(
            f"{node}: would reset system UUID {old_uuid or '<none>'} -> {new_uuid}"
        )
        return True

    if patch_status_uuid(node, "system", new_uuid):
        print(f"{node}: system UUID reset: {old_uuid or '<none>'} -> {new_uuid}")
        return True
    return False


def disk_order(name: str) -> tuple[int, str]:
    return (0 if name == "system" else 1, name)


def diff_disks(
    current: dict[str, dict[str, Any]], desired: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str], list[tuple[str, dict[str, tuple[Any, Any]]]]]:
    added = sorted(set(desired) - set(current), key=disk_order)
    removed = sorted(set(current) - set(desired), key=disk_order)
    changed: list[tuple[str, dict[str, tuple[Any, Any]]]] = []
    for name in sorted(set(current) & set(desired), key=disk_order):
        fields: dict[str, tuple[Any, Any]] = {}
        for field in FIELDS:
            old, new = current[name][field], desired[name][field]
            if field == "tags":
                if set(old) != set(new):
                    fields[field] = (old, new)
            elif old != new:
                fields[field] = (old, new)
        if fields:
            changed.append((name, fields))
    return added, removed, changed


def describe(disk: dict[str, Any]) -> str:
    parts = [f"path={disk['path']}"]
    if not disk["allowScheduling"]:
        parts.append("allowScheduling=false")
    if disk["evictionRequested"]:
        parts.append("evictionRequested=true")
    if disk["storageReserved"]:
        parts.append(f"storageReserved={disk['storageReserved']}")
    if disk["tags"]:
        parts.append(f"tags={json.dumps(disk['tags'])}")
    if disk["diskType"] != DEFAULTS["diskType"]:
        parts.append(f"diskType={disk['diskType']}")
    if disk["diskDriver"]:
        parts.append(f"diskDriver={disk['diskDriver']}")
    return " ".join(parts)


def print_diff(node: str, current, desired) -> None:
    added, removed, changed = diff_disks(current, desired)
    if not (added or removed or changed):
        print(f"{node}: in sync")
        return

    print(f"{node}:")
    for name in added:
        print(f"  + {name}: {describe(desired[name])}")
    for name in removed:
        print(f"  - {name}: {describe(current[name])}")
    for name, fields in changed:
        detail = "; ".join(
            f"{field} {json.dumps(old) if field == 'tags' else old}"
            f" -> {json.dumps(new) if field == 'tags' else new}"
            for field, (old, new) in fields.items()
        )
        print(f"  ~ {name}: {detail}")


def annotation_entries(disks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for name in sorted(disks, key=disk_order):
        disk = disks[name]
        entry: dict[str, Any] = {
            "name": name,
            "path": disk["path"],
            "allowScheduling": disk["allowScheduling"],
            "storageReserved": disk["storageReserved"],
        }
        if disk["tags"]:
            entry["tags"] = disk["tags"]
        if disk["evictionRequested"]:
            entry["evictionRequested"] = True
        if disk["diskType"] != DEFAULTS["diskType"]:
            entry["diskType"] = disk["diskType"]
        if disk["diskDriver"]:
            entry["diskDriver"] = disk["diskDriver"]
        entries.append(entry)
    return entries


def apply_to_longhorn(
    node: str, current: dict[str, dict[str, Any]], desired: dict[str, dict[str, Any]]
) -> bool:
    added, removed, changed = diff_disks(current, desired)
    ok = True

    update_names = [*added, *(name for name, _ in changed)]
    updates = {name: desired[name] for name in update_names}
    if updates:
        success, error = kubectl_patch(
            ["-n", LONGHORN_NS, "patch", "nodes.longhorn.io", node],
            {"spec": {"disks": updates}},
        )
        if not success:
            print(f"  ERROR: failed to update disks: {error}", file=sys.stderr)
            ok = False

    for name in removed:
        if current[name]["allowScheduling"]:
            success, error = kubectl_patch(
                ["-n", LONGHORN_NS, "patch", "nodes.longhorn.io", node],
                {"spec": {"disks": {name: {"allowScheduling": False}}}},
            )
            if not success:
                print(f"  ERROR: failed to disable disk {name!r}: {error}",
                      file=sys.stderr)
                ok = False
                continue

        success, error = kubectl_patch(
            ["-n", LONGHORN_NS, "patch", "nodes.longhorn.io", node],
            {"spec": {"disks": {name: None}}},
        )
        if not success:
            print(
                f"  ERROR: could not remove disk {name!r} (left disabled): {error}",
                file=sys.stderr,
            )
            ok = False

    return ok


def apply_to_annotation(node: str, desired: dict[str, dict[str, Any]]) -> bool:
    value = json.dumps(annotation_entries(desired), separators=(",", ":"))
    success, error = kubectl_patch(
        ["patch", "node", node],
        {"metadata": {"annotations": {ANNOTATION: value}}},
    )
    if not success:
        print(f"  ERROR: failed to update node annotation: {error}", file=sys.stderr)
        return False
    common.warn(
        f"{node}: annotation updated; Talos will revert it on the next config "
        f"apply unless talos/nodes/{node}.yaml is updated too"
    )
    return True


def longhorn_nodes() -> list[str]:
    obj = kubectl_json(["-n", LONGHORN_NS, "get", "nodes.longhorn.io", "-o", "json"])
    return sorted(item["metadata"]["name"] for item in obj.get("items", []))


def sync_node(
    node: str,
    source: str,
    *,
    check: bool,
    dry_run: bool,
    allow_empty: bool,
    repair_uuid: bool,
) -> bool:
    try:
        longhorn = read_longhorn(node)
        if source == "repo":
            current, desired = longhorn, read_repo_annotation(node)
        elif source == "annotation":
            current, desired = longhorn, read_live_annotation(node)
        else:
            current, desired = read_live_annotation(node), longhorn
    except Exception as exc:  # pragma: no cover - defensive
        print(f"{node}: ERROR: {exc}", file=sys.stderr)
        return False

    if not desired and not allow_empty:
        print(
            f"{node}: refusing to sync an empty disk list "
            "(use --allow-empty if this is intentional)",
            file=sys.stderr,
        )
        return False

    print_diff(node, current, desired)
    added, removed, changed = diff_disks(current, desired)
    has_diff = bool(added or removed or changed)

    spec_ok = True
    if has_diff:
        if check:
            spec_ok = False
        elif dry_run:
            print(f"{node}: would sync ({source} is the source)")
        else:
            if source == "longhorn":
                spec_ok = apply_to_annotation(node, longhorn)
            else:
                spec_ok = apply_to_longhorn(node, longhorn, desired)
            if spec_ok:
                print(f"{node}: synced")

    repair_ok = True
    if repair_uuid:
        repair_ok = repair_system_uuid(node, check=check, dry_run=dry_run)
    return spec_ok and repair_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync-longhorn-disks.py",
        description="Sync the Longhorn disk annotation with nodes.longhorn.io.",
    )
    parser.add_argument("nodes", nargs="*", metavar="NODE")
    parser.add_argument(
        "--from", dest="source", choices=("repo", "annotation", "longhorn"),
        default="repo",
        help="source of truth: repo node config (default), live node annotation, "
             "or live Longhorn",
    )
    parser.add_argument("--check", action="store_true",
                        help="report drift and exit 1; apply nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the diff and planned changes; apply nothing")
    parser.add_argument("--all", action="store_true",
                        help="every node with a Longhorn Node CR")
    parser.add_argument("--allow-empty", action="store_true",
                        help="allow an empty disk list (removes every disk)")
    parser.add_argument("--repair-system-uuid", action="store_true",
                        help="reset the system disk status UUID from the on-disk "
                             "longhorn-disk.cfg (uses talosctl)")
    args = parser.parse_args(argv)

    if args.all and args.nodes:
        parser.error("use either NODE arguments or --all, not both")
    if not args.all and not args.nodes:
        parser.error("give at least one NODE, or use --all")

    nodes = args.nodes or longhorn_nodes()
    if args.all and not nodes:
        common.die("no Longhorn nodes found")

    ok = True
    for node in nodes:
        print(f"==> {node}")
        if not sync_node(
            node,
            args.source,
            check=args.check,
            dry_run=args.dry_run,
            allow_empty=args.allow_empty,
            repair_uuid=args.repair_system_uuid,
        ):
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
