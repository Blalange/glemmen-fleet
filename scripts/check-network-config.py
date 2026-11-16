#!/usr/bin/env python3
"""Validate the cluster-specific networking invariants.

Checks that every live node has a Calico IP pool matching its pod CIDR and a
route-agent entry, that the Talos node configs use MTU 9000 on the server/VLAN7
links, that MetalLB never includes the API VIP, and that BGP is disabled.

This is a repository/live-state check, not a mutating script.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KUBECTL = ROOT / "scripts" / "bin" / "kubectl"
DEFAULT_KUBECONFIG = ROOT / "talos" / "generated" / "kubeconfig"
OFFLINE_NODES = {"glemmen50"}

failures: list[str] = []
warnings: list[str] = []


def fail(message: str) -> None:
    failures.append(message)


def warn(message: str) -> None:
    warnings.append(message)


def kubectl_json(kubectl: Path, kubeconfig: Path, *args: str):
    command = [str(kubectl), "--kubeconfig", str(kubeconfig), *args, "-o", "json"]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "kubectl {} failed: {}".format(
                " ".join(args), completed.stderr.strip()))
    return json.loads(completed.stdout)


def check_live_nodes(kubectl: Path, kubeconfig: Path) -> None:
    nodes = kubectl_json(kubectl, kubeconfig, "get", "nodes").get("items", [])
    pools = kubectl_json(kubectl, kubeconfig, "get", "ippools").get("items", [])
    live = {node["metadata"]["name"]: node for node in nodes}

    for name, node in sorted(live.items()):
        pod_cidr = node.get("spec", {}).get("podCIDR", "")
        if not pod_cidr:
            fail("{} has no spec.podCIDR".format(name))
            continue
        matching = []
        for pool in pools:
            spec = pool.get("spec", {})
            selector = spec.get("nodeSelector", "")
            if spec.get("cidr") == pod_cidr and name in selector:
                matching.append(pool["metadata"]["name"])
        if not matching:
            fail("{} ({}) has no Calico IPPool with a matching nodeSelector".format(
                name, pod_cidr))

    configured = {
        path.stem for path in (ROOT / "talos" / "nodes").glob("*.yaml")
    }
    for name in sorted(configured - set(live)):
        if name in OFFLINE_NODES:
            print("INFO: {} is offline; checking its repository config only".format(
                name))
        else:
            fail("{} is configured in talos/nodes but is not a live node".format(
                name))


def check_pools_and_agent() -> None:
    installation_path = (ROOT / "flux" / "infrastructure" / "calico" /
                         "installation.yaml")
    installation = installation_path.read_text()
    if "bgp: Disabled" not in installation:
        fail("Calico BGP is not explicitly disabled in installation.yaml")

    documents = list(yaml.safe_load_all(installation))
    glemmen50_pool = False
    for document in documents:
        if not document or document.get("kind") != "Installation":
            continue
        for pool in document.get("spec", {}).get("calicoNetwork", {}).get(
                "ipPools", []):
            if pool.get("cidr") == "10.42.16.0/24" and \
                    "glemmen50" in pool.get("nodeSelector", ""):
                glemmen50_pool = True
    if not glemmen50_pool:
        fail("Calico has no 10.42.16.0/24 pool for glemmen50")

    agent = (ROOT / "flux" / "infrastructure" / "ecmp-route-agent" /
             "agent.sh").read_text()
    for path in sorted((ROOT / "talos" / "nodes").glob("*.yaml")):
        name = path.stem
        if "mtu: 9000" not in path.read_text():
            fail("{} has no mtu: 9000 LinkConfig".format(path.relative_to(ROOT)))
        if not re.search(r"\b{}\b".format(re.escape(name)), agent):
            fail("{} is missing from the ECMP route-agent map".format(name))


def check_metallb() -> None:
    path = (ROOT / "flux" / "infrastructure" / "metallb" / "resources" /
            "pool.yaml")
    documents = list(yaml.safe_load_all(path.read_text()))
    addresses = []
    advertisements = []
    for document in documents:
        if not document:
            continue
        if document.get("kind") == "IPAddressPool":
            addresses.extend(document.get("spec", {}).get("addresses", []))
        elif document.get("kind") == "L2Advertisement":
            advertisements.append(document)
    if any("192.168.6.98" in address for address in addresses):
        fail("MetalLB pool still includes the Talos API VIP 192.168.6.98")
    if not advertisements:
        fail("MetalLB has no L2Advertisement")
    for advertisement in advertisements:
        spec = advertisement.get("spec", {})
        if not spec.get("interfaces"):
            fail("MetalLB L2Advertisement {} does not restrict interfaces".format(
                advertisement.get("metadata", {}).get("name")))
        if not spec.get("nodeSelectors"):
            fail("MetalLB L2Advertisement {} does not restrict nodes".format(
                advertisement.get("metadata", {}).get("name")))


def check_kube_proxy() -> None:
    common = (ROOT / "talos" / "patches" / "common.yaml").read_text()
    if "kind: KubeProxyConfig" not in common or "mode: iptables" not in common:
        fail("kube-proxy is not pinned to iptables in talos/patches/common.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", type=Path, default=DEFAULT_KUBECTL)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    args = parser.parse_args()

    check_pools_and_agent()
    check_metallb()
    check_kube_proxy()
    try:
        check_live_nodes(args.kubectl, args.kubeconfig)
    except Exception as exc:  # noqa: BLE001 - show a useful failure
        fail("live cluster check failed: {}".format(exc))

    for message in warnings:
        print("WARN: {}".format(message))
    for message in failures:
        print("FAIL: {}".format(message))
    if failures:
        print("\n{} check(s) failed".format(len(failures)))
        return 1
    print("network configuration checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
