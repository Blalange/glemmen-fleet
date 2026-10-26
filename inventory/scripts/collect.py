#!/usr/bin/env python3
"""Collect live UniFi, Talos, Redfish and IPMI facts into the generated inventory layer.

The implementation lives in the ``collector`` package next to this wrapper. It
writes ``inventory/generated`` and never edits the manual layer.
"""

from __future__ import annotations

from collector.cli import main
from collector.log import get_logger
from inventory_util import InventoryError

log = get_logger("main")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InventoryError as exc:
        log.error("Collector failed: %s", exc)
        raise SystemExit(1)
    except Exception:
        log.exception("Collector failed unexpectedly")
        raise SystemExit(1)
