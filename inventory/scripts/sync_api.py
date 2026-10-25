"""NetBox connectivity and dry-run stand-ins."""

from __future__ import annotations

import sys
import time
from typing import Any

import pynetbox

from inventory_util import SyncError


class MockObject:
    """Stand-in for a newly created object during a dry run.

    Dry runs still need IDs to resolve references between objects that would be
    created in the same run. Negative IDs can never collide with NetBox IDs.
    """

    _next_id = -1

    def __init__(self, **data: Any) -> None:
        self.id = MockObject._next_id
        MockObject._next_id -= 1
        for key, value in data.items():
            setattr(self, key, value)
        if not hasattr(self, "tags"):
            self.tags = []

    def update(self, data: dict[str, Any] | None = None, **kwargs: Any) -> None:
        data = {**(data or {}), **kwargs}
        for key, value in data.items():
            setattr(self, key, value)

    def save(self) -> None:
        return None

    def delete(self) -> None:
        return None


def wait_for_netbox(nb: pynetbox.api, url: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            nb.status()
            # /api/status is unauthenticated. This request proves that the
            # bootstrap API token exists as well, so the first sync does not
            # race the chart's superuser/token creation.
            list(nb.users.users.filter(limit=1))
            return
        except Exception as exc:  # noqa: BLE001 - report the last connection error
            if time.monotonic() >= deadline:
                raise SyncError(f"NetBox is not ready at {url}: {exc}") from exc
            print(f"Waiting for NetBox (status and API token) at {url}...", file=sys.stderr)
            time.sleep(5)
