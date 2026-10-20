#!/usr/bin/env python3
"""Manage the Flux installation and GitOps reconciliation.

Usage: ./scripts/flux/manage.py <command> [args]

  status                 Show all Flux resources
  sync                   Fetch the latest revision from Git and reconcile
  reconcile [name...]    Reconcile Kustomizations (default: everything, with source)
  suspend <name...>      Suspend Kustomizations
  resume <name...>       Resume Kustomizations
  freeze                 Suspend everything (all Kustomizations)
  unfreeze               Resume everything
  apps-off               Suspend just the application Kustomizations
  apps-on                Resume the application Kustomizations
  logs [controller]      Flux controller logs (default: kustomize-controller)
  tree <name>            Show the resource tree of a Kustomization
  helm [name...]         Show HelmReleases / a specific one
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import common  # noqa: E402

APP_KUSTOMIZATIONS = (
    "colibri",
    "energisjappa",
    "frigate",
    "homepage",
    "homepage-assets",
    "panel",
    "snipe-it",
    "unifi",
    "volunteer",
    "warrior",
)


def flux_command(flux: str, args: list[str], **kwargs):
    return common.run([flux, *args], **kwargs)


def cmd_status(flux: str, _args: argparse.Namespace) -> int:
    flux_command(flux, ["get", "sources", "all"])
    print()
    flux_command(flux, ["get", "kustomizations"])
    print()
    flux_command(flux, ["get", "helmreleases", "-A"])
    return 0


def cmd_sync(flux: str, _args: argparse.Namespace) -> int:
    flux_command(
        flux, ["reconcile", "source", "git", "flux-system", "-n", "flux-system"]
    )
    flux_command(
        flux,
        [
            "reconcile",
            "kustomization",
            "flux-system",
            "-n",
            "flux-system",
            "--with-source",
        ],
    )
    return 0


def cmd_reconcile(flux: str, args: argparse.Namespace) -> int:
    if not args.names:
        flux_command(
            flux,
            [
                "reconcile",
                "kustomization",
                "--all",
                "-n",
                "flux-system",
                "--with-source",
            ],
        )
        return 0

    for name in args.names:
        flux_command(
            flux,
            [
                "reconcile",
                "kustomization",
                name,
                "-n",
                "flux-system",
                "--with-source",
            ],
        )
    return 0


def cmd_suspend(flux: str, args: argparse.Namespace) -> int:
    for name in args.names:
        flux_command(flux, ["suspend", "kustomization", name, "-n", "flux-system"])
    return 0


def cmd_resume(flux: str, args: argparse.Namespace) -> int:
    for name in args.names:
        flux_command(flux, ["resume", "kustomization", name, "-n", "flux-system"])
    return 0


def cmd_freeze(flux: str, _args: argparse.Namespace) -> int:
    flux_command(flux, ["suspend", "kustomization", "--all", "-n", "flux-system"])
    return 0


def cmd_unfreeze(flux: str, _args: argparse.Namespace) -> int:
    flux_command(flux, ["resume", "kustomization", "--all", "-n", "flux-system"])
    return 0


def _best_effort(flux: str, action: str, name: str) -> None:
    result = flux_command(
        flux,
        [action, "kustomization", name, "-n", "flux-system"],
        check=False,
        capture=True,
        quiet=True,
    )
    if result.returncode == 0 and result.stdout:
        print(result.stdout, end="")


def cmd_apps_off(flux: str, _args: argparse.Namespace) -> int:
    for name in APP_KUSTOMIZATIONS:
        _best_effort(flux, "suspend", name)
    return 0


def cmd_apps_on(flux: str, _args: argparse.Namespace) -> int:
    for name in APP_KUSTOMIZATIONS:
        _best_effort(flux, "resume", name)
    return 0


def cmd_logs(_flux: str, args: argparse.Namespace) -> int:
    kubectl = common.require_tool("kubectl", "KUBECTL")
    common.run(
        [
            kubectl,
            "-n",
            "flux-system",
            "logs",
            f"deploy/{args.controller}",
            "--tail=200",
            "-f",
        ]
    )
    return 0


def cmd_tree(flux: str, args: argparse.Namespace) -> int:
    flux_command(
        flux, ["tree", "kustomization", args.name, "-n", "flux-system"]
    )
    return 0


def cmd_helm(flux: str, args: argparse.Namespace) -> int:
    if not args.names:
        flux_command(flux, ["get", "helmreleases", "-A"])
    else:
        flux_command(flux, ["get", "helmrelease", *args.names, "-A"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Manage Flux and GitOps reconciliation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ./scripts/flux/manage.py status\n"
            "  ./scripts/flux/manage.py reconcile homepage\n"
            "  ./scripts/flux/manage.py freeze\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    commands = {
        "status": (cmd_status, "Show all Flux resources"),
        "sync": (cmd_sync, "Fetch the latest revision and reconcile"),
        "freeze": (cmd_freeze, "Suspend all Kustomizations"),
        "unfreeze": (cmd_unfreeze, "Resume all Kustomizations"),
        "apps-off": (cmd_apps_off, "Suspend application Kustomizations"),
        "apps-on": (cmd_apps_on, "Resume application Kustomizations"),
    }
    for name, (function, help_text) in commands.items():
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(func=function)

    command = sub.add_parser(
        "reconcile", help="Reconcile Kustomizations", description="Reconcile Kustomizations."
    )
    command.add_argument("names", nargs="*", metavar="name")
    command.set_defaults(func=cmd_reconcile)

    for name, function, help_text in (
        ("suspend", cmd_suspend, "Suspend Kustomizations"),
        ("resume", cmd_resume, "Resume Kustomizations"),
    ):
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.add_argument("names", nargs="+", metavar="name")
        command.set_defaults(func=function)

    command = sub.add_parser("logs", help="Show Flux controller logs")
    command.add_argument("controller", nargs="?", default="kustomize-controller")
    command.set_defaults(func=cmd_logs)

    command = sub.add_parser("tree", help="Show a Kustomization resource tree")
    command.add_argument("name")
    command.set_defaults(func=cmd_tree)

    command = sub.add_parser("helm", help="Show HelmReleases")
    command.add_argument("names", nargs="*", metavar="name")
    command.set_defaults(func=cmd_helm)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        args.command = "status"
        args.func = cmd_status

    flux = common.require_tool("flux", "FLUX")
    return int(args.func(flux, args) or 0)


if __name__ == "__main__":
    sys.exit(main())
