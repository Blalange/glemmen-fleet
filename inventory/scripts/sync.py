#!/usr/bin/env python3
"""Reconcile the NetBox YAML inventory into the NetBox REST API.

The inventory has two layers:

  * ``inventory/generated`` is written by ``inventory/scripts/collect.py`` from
    the live UniFi controller, Kubernetes/Talos nodes and iDRAC Redfish;
  * ``inventory/manual`` is edited by hand and owns models, rack layout,
    physical servers, accessories, manual cables, VIPs, ownership and contacts.

This process merges both layers (manual values win), expands the declarative
rack layout, and reconciles the result. Every taggable object it manages is
tagged with the ``gitops`` tag and the inventory's provenance source tags
(``manual``, ``unifi``, ``talos``, ``redfish``, ``ipmi``, ``snmp`` and
``dtl``). Pruning is opt-in and only ever deletes objects that carry the
``gitops`` tag, so objects created by hand in the NetBox UI are safe.
Owners and owner groups cannot be tagged and are therefore never pruned;
contacts and contact assignments are tagged and pruned normally.

Run locally (uv fetches the pinned dependencies):

    uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 --with requests==2.32.5 \
      python inventory/scripts/sync.py --inventory inventory --check

    NETBOX_URL=https://netbox.glem.men NETBOX_TOKEN=... \
    uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 --with requests==2.32.5 \
      python inventory/scripts/sync.py --inventory inventory --dry-run --prune

The CronJob runs the same entrypoint with DRY_RUN=false and PRUNE=true and
mounts the device-type images at /images.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pynetbox
from dtl_catalog import DtlCatalog
from inventory_problems import Problem
from inventory_util import SyncError, env_bool
from sync_api import wait_for_netbox
from sync_catalog import enrich_inventory
from sync_inventory import load_inventory
from sync_reconciler import Reconciler
from sync_validate import collect_inventory_problems, inventory_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        default=os.environ.get("NETBOX_INVENTORY_DIR", "/inventory"),
        help="directory containing inventory YAML files (default: /inventory)",
    )
    parser.add_argument("--url", default=os.environ.get("NETBOX_URL"), help="NetBox base URL")
    parser.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"), help="NetBox API token")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=env_bool("DRY_RUN", False),
        help="show the plan without writing to NetBox",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        default=env_bool("PRUNE", False),
        help="delete gitops-tagged objects that are no longer in the inventory",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the inventory and exit without contacting NetBox",
    )
    parser.add_argument(
        "--images",
        default=os.environ.get("NETBOX_IMAGES_DIR"),
        help="directory containing device-type images (default: <inventory>/images)",
    )
    parser.add_argument(
        "--force-images",
        action="store_true",
        default=env_bool("FORCE_IMAGES", False),
        help="re-upload device-type images even when the filename already matches",
    )
    parser.add_argument(
        "--dtl",
        dest="dtl",
        action="store_true",
        default=env_bool("DTL_ENABLED", True),
        help="enrich device and module types from the NetBox Device Type Library",
    )
    parser.add_argument(
        "--no-dtl",
        dest="dtl",
        action="store_false",
        help="disable Device Type Library enrichment",
    )
    parser.add_argument(
        "--dtl-cache",
        default=os.environ.get("DTL_CACHE_DIR"),
        help="optional DTL definition/image cache directory (default: DTL_CACHE_DIR)",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=int(os.environ.get("NETBOX_WAIT_SECONDS", "120")),
        help="seconds to wait for NetBox to become reachable (default: 120)",
    )
    args = parser.parse_args(argv)

    problems: list[Problem] = []
    inventory = load_inventory(Path(args.inventory), problems=problems)
    if args.dtl:
        catalog = DtlCatalog.from_env()
        if args.dtl_cache:
            catalog.cache_dir = Path(args.dtl_cache)
        manufacturers = {
            str(item.get("slug")): str(item.get("name"))
            for item in inventory.get("manufacturers", []) or []
            if isinstance(item, dict) and item.get("slug") and item.get("name")
        }
        report = enrich_inventory(
            inventory,
            catalog,
            manufacturers=manufacturers,
            fetch_images=not args.check,
        )
        print(f"DTL: {report.summary()}", file=sys.stderr)
    else:
        # Preserve previously applied catalog metadata/images when enrichment
        # is explicitly disabled or unavailable.
        for section in ("device_types", "module_types"):
            for item in inventory.get(section, []) or []:
                if isinstance(item, dict) and item.get("dtl"):
                    item["_dtl_failed"] = True
    problems.extend(
        collect_inventory_problems(inventory, report_duplicates=False)
    )
    if problems:
        for item in problems:
            print(
                f"{item.severity.upper():7} {item.code:24} {item.message}",
                file=sys.stderr,
            )
        if args.check:
            print(f"ERROR: {len(problems)} inventory problem(s)", file=sys.stderr)
            return 1
        raise SyncError(f"{len(problems)} inventory problem(s)")

    print(inventory_summary(inventory))
    if args.check:
        return 0

    if not args.url:
        raise SyncError("NETBOX_URL is not set (or pass --url)")
    if not args.token:
        raise SyncError("NETBOX_TOKEN is not set (or pass --token)")

    nb = pynetbox.api(args.url.rstrip("/"), token=args.token)
    wait_for_netbox(nb, args.url, args.wait)
    images_dir = Path(args.images) if args.images else Path(args.inventory) / "images"
    Reconciler(
        nb,
        inventory,
        dry_run=args.dry_run,
        prune=args.prune,
        url=args.url,
        token=args.token,
        images_dir=images_dir,
        force_images=args.force_images,
    ).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - keep CronJob logs readable
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
