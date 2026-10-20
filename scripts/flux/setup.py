#!/usr/bin/env python3
"""Bootstrap Flux on the Talos cluster and connect it to this repository.

Prerequisites:
  - the cluster is up and KUBECONFIG points at it
    (default: talos/generated/kubeconfig, created with
    ./scripts/talos/manage.py kubeconfig)
  - GITHUB_TOKEN is exported: a fine-grained PAT for Blalange/glemmen-fleet with
      Contents: Read and write, Metadata: Read-only
    (Administration: Read-only is additionally required when bootstrapping in
     an organization). The token is stored in the cluster secret
     flux-system/flux-system for the controllers. Prefer a repo-scoped token
     and rotate it after the migration; a read/write deploy key works too.
  - the repository already contains flux/clusters/glemmen (this tree).

This script never downloads tools. Run ./scripts/download-tools.py flux if the
flux CLI is missing.

Usage:
  export KUBECONFIG=talos/generated/kubeconfig
  export GITHUB_TOKEN=github_pat_...
  ./scripts/flux/setup.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import common  # noqa: E402


def github_account_type(owner: str, token: str) -> str:
    try:
        account = common.http_get_json(
            f"https://api.github.com/users/{owner}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        return str(account.get("type", "Organization"))
    except Exception:
        return "Organization"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="Bootstrap Flux with the GitHub bootstrap provider.",
    )
    parser.add_argument(
        "--owner", default=os.environ.get("OWNER", "Blalange"),
        help="GitHub owner (default: OWNER or Blalange)",
    )
    parser.add_argument(
        "--repository", default=os.environ.get("REPOSITORY", "glemmen-fleet"),
        help="GitHub repository (default: REPOSITORY or glemmen-fleet)",
    )
    parser.add_argument(
        "--branch", default=os.environ.get("BRANCH", "main"),
        help="Git branch (default: BRANCH or main)",
    )
    parser.add_argument(
        "--path", default=os.environ.get("BOOTSTRAP_PATH", "flux/clusters/glemmen"),
        help="cluster path inside the repository",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        common.die(
            f"GITHUB_TOKEN is not set "
            f"(fine-grained PAT for {args.owner}/{args.repository})"
        )

    flux = common.require_tool("flux", "FLUX")
    kubectl = common.require_tool("kubectl", "KUBECTL")

    print("==> Checking cluster access")
    common.run([kubectl, "get", "nodes"], capture=True)

    account_type = github_account_type(args.owner, token)
    print(f"==> GitHub account {args.owner} is a {account_type}")

    command = [
        flux,
        "bootstrap",
        "github",
        f"--owner={args.owner}",
        f"--repository={args.repository}",
        f"--branch={args.branch}",
        f"--path={args.path}",
        "--token-auth",
    ]
    if account_type == "User":
        command.append("--personal")

    print(f"==> Bootstrapping Flux (path {args.path})")
    common.run(command)

    print("==> Waiting for the root Kustomization")
    common.run(
        [
            flux,
            "-n",
            "flux-system",
            "reconcile",
            "kustomization",
            "flux-system",
            "--with-source",
            "--timeout=5m",
        ],
        check=False,
    )

    print()
    print(f"Flux is installed. The cluster tree under {args.path} is now applied,")
    print("starting with flux/infrastructure.")
    print("Use ./scripts/flux/manage.py status to watch.")
    print()
    print("TIP: before restoring the Longhorn volumes/secrets, freeze the workloads:")
    print("  ./scripts/flux/manage.py freeze        # suspend all Kustomizations")
    print("  ./scripts/flux/manage.py status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
