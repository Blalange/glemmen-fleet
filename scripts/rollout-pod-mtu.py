#!/usr/bin/env python3
"""Recreate pod-network workloads so Calico assigns the current 8980-byte MTU.

Calico only sets the veth MTU when a pod is created; existing pods keep the
old MTU forever. This script finds controllers that own pod-network pods and
rolls them. It is dry-run by default; pass --apply to actually restart
workloads. Use --verify after the rollout to inspect host-side veth MTUs via
the ecmp-route-agent pods.

This is intentionally conservative: hostNetwork pods (Calico node, MetalLB
speaker, kube-proxy, node-exporter, etc.) have no Calico veth and are skipped.
Unmanaged pod-network pods are reported but not deleted unless --delete-orphans
is passed.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KUBECTL = ROOT / "scripts" / "bin" / "kubectl"
DEFAULT_KUBECONFIG = ROOT / "talos" / "generated" / "kubeconfig"

# Storage/control-plane components last: a controller restart can trigger
# volume rebuilds or a brief API/ingress interruption.
PRIORITY = {
    "kube-system": 90,
    "calico-system": 95,
    "metallb-system": 96,
    "monitoring": 50,
    "yugabyte": 70,
    "netbox": 30,
    "media": 20,
}


def kubectl(kubectl_path: Path, kubeconfig: Path, args: list[str],
            check: bool = True) -> subprocess.CompletedProcess:
    command = [str(kubectl_path), "--kubeconfig", str(kubeconfig), *args]
    return subprocess.run(command, check=check, text=True,
                          capture_output=True)


def get_pods(kubectl_path: Path, kubeconfig: Path) -> list[dict]:
    completed = kubectl(kubectl_path, kubeconfig, ["get", "pods", "-A", "-o", "json"])
    return json.loads(completed.stdout).get("items", [])


def controllers(pods: list[dict]) -> tuple[list[tuple], list[dict]]:
    groups = {}
    orphans = []
    for pod in pods:
        if pod.get("spec", {}).get("hostNetwork"):
            continue
        if pod.get("status", {}).get("phase") not in ("Running", "Pending"):
            continue
        namespace = pod["metadata"]["namespace"]
        owners = pod["metadata"].get("ownerReferences") or []
        controller = next(
            (owner for owner in owners
             if owner.get("controller") and owner.get("kind") in
             ("Deployment", "StatefulSet", "DaemonSet")),
            None,
        )
        if not controller:
            orphans.append(pod)
            continue
        key = (namespace, controller["kind"], controller["name"])
        groups.setdefault(key, []).append(pod["metadata"]["name"])
    ordered = sorted(
        groups.items(),
        key=lambda item: (PRIORITY.get(item[0][0], 10), item[0][0], item[0][1],
                          item[0][2]),
    )
    return ordered, orphans


def rollout(kubectl_path: Path, kubeconfig: Path,
            namespace: str, kind: str, name: str, apply: bool) -> None:
    resource = "{}/{}".format(kind.lower(), name)
    print("  kubectl -n {} rollout restart {}".format(namespace, resource))
    if not apply:
        return
    kubectl(kubectl_path, kubeconfig,
            ["-n", namespace, "rollout", "restart", resource])
    kubectl(kubectl_path, kubeconfig,
            ["-n", namespace, "rollout", "status", resource,
             "--timeout=900s"], check=False)


def verify(kubectl_path: Path, kubeconfig: Path) -> int:
    completed = kubectl(kubectl_path, kubeconfig,
                        ["get", "pods", "-n", "ecmp-system", "-o", "json"])
    pods = json.loads(completed.stdout).get("items", [])
    bad = 0
    for pod in pods:
        name = pod["metadata"]["name"]
        node = pod["spec"].get("nodeName", "?")
        result = kubectl(
            kubectl_path, kubeconfig,
            ["exec", "-n", "ecmp-system", name, "--", "sh", "-c",
             "ip -d link show type veth 2>/dev/null "
             "| grep -o 'mtu [0-9]*' | sort | uniq -c"],
            check=False,
        )
        print("{} {}".format(node, result.stdout.strip() or "(no veths)"))
        if "mtu 1480" in result.stdout:
            bad += 1
    if bad:
        print("{} node(s) still have 1480-byte veths".format(bad))
        return 1
    print("no 1480-byte veths found on the inspected nodes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", type=Path, default=DEFAULT_KUBECTL)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--apply", action="store_true",
                        help="actually restart controllers")
    parser.add_argument("--delete-orphans", action="store_true",
                        help="delete pod-network pods without a controller")
    parser.add_argument("--verify", action="store_true",
                        help="only inspect veth MTUs through the agent pods")
    args = parser.parse_args()

    if args.verify:
        return verify(args.kubectl, args.kubeconfig)

    groups, orphans = controllers(get_pods(args.kubectl, args.kubeconfig))
    print("Controllers with pod-network pods ({}):".format(len(groups)))
    for (namespace, kind, name), pod_names in groups:
        print("{}/{} {} ({} pods)".format(namespace, kind, name, len(pod_names)))
        rollout(args.kubectl, args.kubeconfig, namespace, kind, name, args.apply)
        if args.apply:
            time.sleep(2)

    if orphans:
        print("\nUnmanaged pod-network pods ({}):".format(len(orphans)))
        for pod in orphans:
            namespace = pod["metadata"]["namespace"]
            name = pod["metadata"]["name"]
            print("  {}/{}".format(namespace, name))
            if args.apply and args.delete_orphans:
                kubectl(args.kubectl, args.kubeconfig,
                        ["-n", namespace, "delete", "pod", name, "--wait=false"])

    if not args.apply:
        print("\nDry run. Re-run with --apply to restart the controllers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
