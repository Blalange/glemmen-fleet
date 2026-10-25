#!/usr/bin/env python3
"""Collect live UniFi, Talos and iDRAC facts into the generated inventory layer.

The implementation lives in the ``collector`` package next to this wrapper. It
writes ``inventory/generated`` and never edits the manual layer.
"""

from __future__ import annotations

import sys

from collector.cli import main
from inventory_util import InventoryError


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - keep CLI output readable
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
