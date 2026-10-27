"""Shared rack layout and occupancy rules.

The sync loader, sync validator and language server all use these functions so
they agree about unit accounting, positions, bounds and overlaps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from inventory_problems import Problem, ProblemLocation, problem


@dataclass
class RackPlan:
    positions: dict[str, tuple[int, int, str]] = field(default_factory=dict)
    occupancy: dict[int, str] = field(default_factory=dict)
    used: int = 0
    problems: list[Problem] = field(default_factory=list)


def plan_rack_layout(
    rack: dict[str, Any],
    devices: Mapping[str, dict[str, Any]],
    device_types: Mapping[str, dict[str, Any]],
) -> RackPlan:
    """Compute positions for one rack layout.

    The function never mutates its inputs. Callers apply ``positions`` to the
    device objects and collect ``problems`` for reporting.
    """

    plan = RackPlan()
    rack_name = str(rack.get("name") or "")
    site = rack.get("site")
    key = (site, rack_name)

    try:
        u_height = int(rack.get("u_height", 42))
    except (TypeError, ValueError):
        u_height = 42
        plan.problems.append(
            problem(
                "rack-invalid-height",
                f"Rack {rack_name!r} has an invalid u_height.",
                kind="rack",
                key=key,
                field_path=("u_height",),
            )
        )

    layout = rack.get("layout")
    if layout is None:
        return plan
    if not isinstance(layout, list):
        plan.problems.append(
            problem(
                "rack-layout-invalid",
                f"Rack {rack_name!r} layout must be a list.",
                kind="rack",
                key=key,
                field_path=("layout",),
            )
        )
        return plan
    if not layout:
        return plan

    cursor = u_height
    seen: set[str] = set()
    for index, entry in enumerate(layout):
        entry_path = ("layout", index)
        if not isinstance(entry, dict):
            plan.problems.append(
                problem(
                    "rack-layout-entry",
                    f"Rack {rack_name!r} layout entry must be a mapping.",
                    kind="rack",
                    key=key,
                    field_path=entry_path,
                )
            )
            continue
        gap = entry.get("gap", entry.get("empty"))
        if gap is not None:
            try:
                gap_units = int(gap)
            except (TypeError, ValueError):
                plan.problems.append(
                    problem(
                        "rack-layout-entry",
                        f"Rack {rack_name!r} layout gap must be an integer.",
                        kind="rack",
                        key=key,
                        field_path=entry_path,
                    )
                )
                continue
            if gap_units < 1:
                plan.problems.append(
                    problem(
                        "rack-layout-entry",
                        f"Rack {rack_name!r} layout gap must be at least 1U.",
                        kind="rack",
                        key=key,
                        field_path=entry_path,
                    )
                )
                continue
            cursor -= gap_units
            if cursor < 0:
                plan.problems.append(
                    problem(
                        "rack-layout-overflow",
                        f"Rack {rack_name!r} layout exceeds its {u_height}U height.",
                        kind="rack",
                        key=key,
                        field_path=entry_path,
                    )
                )
            continue

        name = entry.get("device")
        if not name:
            plan.problems.append(
                problem(
                    "rack-layout-entry",
                    f"Rack {rack_name!r} layout entry needs a device or gap.",
                    kind="rack",
                    key=key,
                    field_path=entry_path,
                )
            )
            continue
        name = str(name)
        device_path = entry_path + ("device",)
        if name in seen:
            plan.problems.append(
                problem(
                    "rack-layout-duplicate-device",
                    f"Device {name!r} appears more than once in rack {rack_name!r}.",
                    kind="rack",
                    key=key,
                    field_path=device_path,
                )
            )
            continue
        seen.add(name)
        device = devices.get(name)
        if device is None:
            plan.problems.append(
                problem(
                    "rack-layout-unknown-device",
                    f"Device {name!r} is not defined.",
                    kind="rack",
                    key=key,
                    field_path=device_path,
                )
            )
            continue
        device_type_slug = device.get("device_type")
        device_type = device_types.get(str(device_type_slug)) if device_type_slug else None
        if device_type is None:
            plan.problems.append(
                problem(
                    "rack-layout-unknown-device-type",
                    f"Device {name!r} has no defined device type.",
                    kind="rack",
                    key=key,
                    field_path=device_path,
                    related=[ProblemLocation("device", name, ("device_type",))],
                )
            )
            continue
        try:
            height = int(device_type.get("u_height", 1))
        except (TypeError, ValueError):
            height = 1
        if height < 1:
            plan.problems.append(
                problem(
                    "rack-layout-zero-height",
                    f"Device {name!r} has u_height {height} and cannot be placed in a rack layout.",
                    kind="rack",
                    key=key,
                    field_path=device_path,
                )
            )
            continue
        position = cursor - height + 1
        if position < 1:
            plan.problems.append(
                problem(
                    "rack-layout-does-not-fit",
                    f"Device {name!r} ({height}U) does not fit in rack {rack_name!r}.",
                    kind="rack",
                    key=key,
                    field_path=entry_path,
                )
            )
            continue
        face = str(entry.get("face") or device.get("face") or "front")
        end = position + height - 1
        plan.positions[name] = (position, end, face)
        plan.used += height
        for unit in range(position, end + 1):
            plan.occupancy[unit] = name
        cursor = position - 1

    if cursor != 0:
        used = u_height - cursor
        if used < u_height:
            message = (
                f"Rack {rack_name!r} layout uses {used} of {u_height} units; "
                f"add {u_height - used} empty unit(s)."
            )
        else:
            message = (
                f"Rack {rack_name!r} layout uses {used} of {u_height} units; "
                f"it overflows by {used - u_height} unit(s)."
            )
        plan.problems.append(
            problem(
                "rack-layout-incomplete",
                message,
                kind="rack",
                key=key,
                field_path=("layout",),
            )
        )
    return plan


def rack_occupancy_problems(
    devices: list[dict[str, Any]],
    device_types: Mapping[str, dict[str, Any]],
    racks: Mapping[tuple[Any, str], dict[str, Any]],
) -> list[Problem]:
    """Check explicit rack/position fields and overlap between racked devices."""

    problems: list[Problem] = []
    occupancy: dict[tuple[Any, str], dict[int, str]] = {}

    for device in devices:
        name = str(device.get("name") or "")
        rack_name = device.get("rack")
        position = device.get("position")
        face = device.get("face")
        if not rack_name:
            if position is not None or face:
                problems.append(
                    problem(
                        "rack-position-without-rack",
                        f"Device {name!r} has position/face without a rack.",
                        kind="device",
                        key=name,
                        field_path=("position" if position is not None else "face",),
                    )
                )
            continue
        if position is None:
            continue
        site = device.get("site")
        key = (site, str(rack_name))
        rack = racks.get(key)
        if rack is None:
            # The reference validator reports the unknown rack itself.
            continue
        if not face:
            problems.append(
                problem(
                    "rack-position-without-face",
                    f"Device {name!r} has a rack position without a face.",
                    kind="device",
                    key=name,
                    field_path=("position",),
                )
            )
        try:
            start = int(position)
        except (TypeError, ValueError):
            problems.append(
                problem(
                    "rack-position-invalid",
                    f"Device {name!r} has an invalid rack position.",
                    kind="device",
                    key=name,
                    field_path=("position",),
                )
            )
            continue
        device_type_slug = device.get("device_type")
        device_type = device_types.get(str(device_type_slug)) if device_type_slug else None
        if device_type is None:
            continue
        try:
            height = int(device_type.get("u_height", 1))
        except (TypeError, ValueError):
            height = 1
        if height < 1:
            problems.append(
                problem(
                    "racked-device-zero-height",
                    f"Device {name!r}: a racked device type must be at least 1U.",
                    kind="device",
                    key=name,
                    field_path=("device_type",),
                )
            )
            continue
        end = start + height - 1
        try:
            rack_height = int(rack.get("u_height", 42))
        except (TypeError, ValueError):
            rack_height = 42
        if start < 1 or end > rack_height:
            problems.append(
                problem(
                    "rack-position-out-of-bounds",
                    f"Device {name!r}: rack position {start}-{end} is outside "
                    f"rack {rack_name!r} (1-{rack_height})",
                    kind="device",
                    key=name,
                    field_path=("position",),
                )
            )
            continue
        used = occupancy.setdefault(key, {})
        conflict = next(
            ((unit, used[unit]) for unit in range(start, end + 1) if unit in used),
            None,
        )
        if conflict is not None:
            unit, other = conflict
            problems.append(
                problem(
                    "rack-position-overlap",
                    f"Rack {rack_name!r} unit {unit} is used by both "
                    f"{other!r} and {name!r}",
                    kind="device",
                    key=name,
                    field_path=("position",),
                    related=[ProblemLocation("device", other, ("position",))],
                )
            )
        for unit in range(start, end + 1):
            used.setdefault(unit, name)
    return problems
