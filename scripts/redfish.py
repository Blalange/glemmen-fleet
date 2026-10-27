#!/usr/bin/env python3
"""Control one or more servers over the Redfish API.

Every command is sent to every selected BMC in parallel, so a single
invocation can power-cycle the whole fleet or set the minimum fan speed on
all iDRACs at once.

Commands:
  status                              Power state, health, model and fan floor
  power on|off|force-off|restart|reset|cycle
                                      Power control (ResetType)
  fan show                            Current fan settings
  fan min-speed <0-100|default>       "Minimum Fan Speed in PWM (% of Max)"
  fan profile <profile>               Thermal profile
  identify [on|off|blink|status]      Chassis locate LED

Examples:
  ./scripts/redfish.py --host 192.168.4.130 --host 192.168.4.150 \\
      --username root --password '...' status

  ./scripts/redfish.py --hosts 192.168.4.130,192.168.4.150 \\
      --user root --password '...' fan min-speed 30

  ./scripts/redfish.py --all --user root --password '...' fan min-speed default

  ./scripts/redfish.py --host glemmen130=192.168.4.130 \\
      --user root --password '...' power restart

Use --all to read every `redfish_ip` from `inventory/manual/devices/*.yaml`
(requires PyYAML). iDRACs normally use self-signed certificates, so TLS
verification is off by default; pass --verify or --ca-cert to enable it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests
    import urllib3
except ImportError:  # pragma: no cover - depends on the local environment
    sys.exit(
        "ERROR: this script requires the 'requests' package.\n"
        "       Install it with: python3 -m pip install requests\n"
        "       Or run with:    uv run --with requests python scripts/redfish.py ..."
    )

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_INVENTORY_DIR = REPO_ROOT / "inventory"

MFS_KEY = "ThermalSettings.1.MinimumFanSpeed"
MFS_MIN_KEY = "ThermalSettings.1.MFSMinimumLimit"
MFS_MAX_KEY = "ThermalSettings.1.MFSMaximumLimit"
PROFILE_KEY = "ThermalSettings.1.ThermalProfile"
OFFSET_KEY = "ThermalSettings.1.FanSpeedOffset"

MFS_DEFAULT = 255  # iDRAC's sentinel for "Default" instead of a PWM floor.

POWER_ACTIONS = {
    "on": "On",
    "off": "GracefulShutdown",
    "force-off": "ForceOff",
    "restart": "GracefulRestart",
    "reset": "ForceRestart",
    "cycle": "PowerCycle",
}

PROFILES = {
    "default": "Default",
    "maximum-performance": "Maximum Performance",
    "minimum-power": "Minimum Power",
    "sound-cap": "Sound Cap",
}


class RedfishError(Exception):
    """A failed Redfish request or an unexpected response."""


@dataclass(frozen=True)
class Target:
    label: str
    address: str


def _normalize_base(address: str) -> str:
    address = address.strip()
    if not address:
        raise ValueError("empty host")
    if "://" not in address:
        address = f"https://{address}"
    parsed = urllib.parse.urlsplit(address)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid Redfish address: {address!r}")
    return address.rstrip("/")


def _parse_target(value: str) -> Target:
    value = value.strip()
    if not value:
        raise ValueError("empty host")
    label = value
    address = value
    # NAME=ADDRESS is convenient for fleet output. Restrict the name to plain
    # words so a URL containing '=' is not mistaken for a label.
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)=(.+)", value)
    if match:
        label, address = match.group(1), match.group(2).strip()
    normalized = _normalize_base(address)
    if label == address:
        label = urllib.parse.urlsplit(normalized).netloc or address
    return Target(label=label, address=address)


def _inventory_targets(directory: Path) -> list[Target]:
    try:
        import yaml
    except ImportError:  # pragma: no cover - depends on the local environment
        raise RedfishError(
            "--all requires PyYAML; install it with: python3 -m pip install pyyaml"
        ) from None

    devices_dir = directory / "manual" / "devices"
    if not devices_dir.is_dir():
        raise RedfishError(f"inventory devices directory not found: {devices_dir}")

    targets: list[Target] = []
    seen: set[str] = set()
    for path in sorted(devices_dir.glob("*.yaml")):
        try:
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            raise RedfishError(f"cannot read {path}: {exc}") from exc
        for document in documents:
            if not isinstance(document, dict):
                continue
            for device in document.get("devices") or []:
                if not isinstance(device, dict):
                    continue
                address = device.get("redfish_ip")
                if not address:
                    continue
                address = str(address)
                try:
                    base = _normalize_base(address)
                except ValueError as exc:
                    raise RedfishError(f"{path}: {exc}") from exc
                if base in seen:
                    continue
                seen.add(base)
                targets.append(
                    Target(label=str(device.get("name") or address), address=address)
                )

    if not targets:
        raise RedfishError(f"no redfish_ip entries found under {devices_dir}")
    return targets


def _error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = " ".join(response.text.split())
        return f": {text[:300]}" if text else ""
    if not isinstance(data, dict):
        return ""

    error = data.get("error")
    if not isinstance(error, dict):
        error = data
    messages: list[str] = []
    for item in error.get("@Message.ExtendedInfo") or []:
        if isinstance(item, dict):
            message = item.get("Message") or item.get("MessageId")
            if message:
                messages.append(str(message))
    if not messages:
        message = error.get("message")
        if message:
            messages.append(str(message))
    return f": {'; '.join(messages)}" if messages else ""


class Server:
    """A single Redfish endpoint. One instance is used per server thread."""

    def __init__(
        self,
        target: Target,
        username: str,
        password: str,
        *,
        timeout: float,
        verify: bool,
        ca_cert: str | None,
        debug: bool,
    ) -> None:
        self.target = target
        self.timeout = timeout
        self.debug = debug
        self._system_uri: str | None = None
        self._manager_uri: str | None = None
        self._chassis_uri: str | None = None
        self._attributes_uri: str | None = None
        self._attributes: dict[str, Any] | None = None

        if ca_cert:
            verify_requests: bool | str = ca_cert
        else:
            verify_requests = verify
        if not verify_requests:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.base = _normalize_base(target.address)
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify_requests
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "glemmen-fleet-redfish/1.0",
            }
        )

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base}{path}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow: tuple[int, ...] = (),
    ) -> requests.Response:
        if self.debug:
            print(
                f"[{self.target.label}] {method} {path}",
                file=sys.stderr,
                flush=True,
            )
        try:
            response = self.session.request(
                method,
                self._url(path),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RedfishError(f"{method} {path} failed: {exc}") from exc

        if response.status_code in allow:
            return response
        if not 200 <= response.status_code < 300:
            raise RedfishError(
                f"{method} {path} returned HTTP {response.status_code}"
                f"{_error_detail(response)}"
            )
        return response

    def get_json(
        self, path: str, *, allow: tuple[int, ...] = ()
    ) -> dict[str, Any] | None:
        response = self._request("GET", path, allow=allow)
        if response.status_code in allow:
            return None
        try:
            data = response.json()
        except ValueError as exc:
            raise RedfishError(f"GET {path} did not return JSON") from exc
        if not isinstance(data, dict):
            raise RedfishError(f"GET {path} returned unexpected JSON")
        return data

    def try_get(self, path: str) -> dict[str, Any] | None:
        return self.get_json(path, allow=(404, 405))

    def _pick_member(self, collection: dict[str, Any], preferred: str) -> str:
        members = collection.get("Members") or []
        uris = [
            str(member.get("@odata.id"))
            for member in members
            if isinstance(member, dict) and member.get("@odata.id")
        ]
        if not uris:
            raise RedfishError("Redfish collection returned no members")
        for uri in uris:
            if preferred.lower() in uri.lower():
                return uri
        return uris[0]

    def system(self) -> dict[str, Any]:
        if self._system_uri is None:
            collection = self.get_json("/redfish/v1/Systems")
            if collection is None:
                raise RedfishError("cannot read /redfish/v1/Systems")
            self._system_uri = self._pick_member(collection, "System.Embedded.1")
        data = self.get_json(self._system_uri)
        if data is None:
            raise RedfishError(f"cannot read system {self._system_uri}")
        return data

    def manager(self) -> dict[str, Any] | None:
        if self._manager_uri is None:
            collection = self.try_get("/redfish/v1/Managers")
            if not collection:
                return None
            try:
                self._manager_uri = self._pick_member(collection, "iDRAC.Embedded.1")
            except RedfishError:
                return None
        return self.get_json(self._manager_uri, allow=(404,))

    def chassis(self) -> tuple[str, dict[str, Any]]:
        if self._chassis_uri is None:
            system = self.system()
            links = (system.get("Links") or {}).get("Chassis") or []
            if links and isinstance(links[0], dict) and links[0].get("@odata.id"):
                self._chassis_uri = str(links[0]["@odata.id"])
            else:
                collection = self.get_json("/redfish/v1/Chassis")
                if collection is None:
                    raise RedfishError("cannot read /redfish/v1/Chassis")
                self._chassis_uri = self._pick_member(collection, "System.Embedded.1")
        data = self.get_json(self._chassis_uri)
        if data is None:
            raise RedfishError(f"cannot read chassis {self._chassis_uri}")
        return self._chassis_uri, data

    def manager_attributes(self, required: tuple[str, ...] = ()) -> dict[str, Any]:
        """Return the iDRAC attribute document, trying the known URI variants."""
        if self._attributes is not None and all(
            key in self._attributes for key in required
        ):
            return self._attributes

        candidates: list[str] = []
        manager = self.manager()
        if manager:
            link = manager.get("Attributes")
            if isinstance(link, dict) and link.get("@odata.id"):
                candidates.append(str(link["@odata.id"]))
            if self._manager_uri:
                candidates.extend(
                    [
                        f"{self._manager_uri}/Attributes",
                        f"{self._manager_uri}/Oem/Dell/DellAttributes/System.Embedded.1",
                    ]
                )
        candidates.extend(
            [
                "/redfish/v1/Managers/System.Embedded.1/Attributes",
                "/redfish/v1/Managers/iDRAC.Embedded.1/Attributes",
                (
                    "/redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/"
                    "DellAttributes/System.Embedded.1"
                ),
            ]
        )

        seen: set[str] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            data = self.try_get(path)
            if not data:
                continue
            attributes = data.get("Attributes")
            if not isinstance(attributes, dict):
                continue
            if all(key in attributes for key in required):
                self._attributes_uri = path
                self._attributes = attributes
                return attributes

        missing = f" (missing {', '.join(required)})" if required else ""
        raise RedfishError(f"no Redfish manager attributes found{missing}")

    def min_fan(self) -> Any:
        return self.manager_attributes((MFS_KEY,)).get(MFS_KEY)

    @staticmethod
    def _format_percent(value: Any) -> str:
        if value is None:
            return "n/a"
        try:
            number = int(value)
        except (TypeError, ValueError):
            return str(value)
        if number == MFS_DEFAULT:
            return "default"
        return f"{number}%"

    def status(self) -> str:
        system = self.system()
        status = system.get("Status") or {}
        parts = [
            f"power={system.get('PowerState', '?')}",
            f"health={status.get('Health') or status.get('State') or '?'}",
            f"model={system.get('Model') or '?'}",
        ]
        if system.get("HostName"):
            parts.append(f"host={system['HostName']}")
        try:
            manager = self.manager()
        except RedfishError:
            manager = None
        if manager and manager.get("FirmwareVersion"):
            parts.append(f"idrac={manager['FirmwareVersion']}")
        try:
            parts.append(f"min-fan={self._format_percent(self.min_fan())}")
        except RedfishError:
            pass
        return " ".join(parts)

    def power(self, action: str) -> str:
        system = self.system()
        state = system.get("PowerState")
        if action == "on" and state == "On":
            return "already On"
        if action in {"off", "force-off"} and state == "Off":
            return "already Off"

        reset_type = POWER_ACTIONS[action]
        reset_action = (system.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        allowed = reset_action.get("ResetType@Redfish.AllowableValues") or []
        if allowed and reset_type not in allowed:
            raise RedfishError(
                f"ResetType {reset_type} is not supported "
                f"(allowed: {', '.join(str(item) for item in allowed)})"
            )

        self._request(
            "POST",
            f"{self._system_uri}/Actions/ComputerSystem.Reset",
            {"ResetType": reset_type},
        )
        return f"requested {reset_type} (was {state or 'unknown'})"

    def show_fan(self) -> str:
        attributes = self.manager_attributes((MFS_KEY,))
        minimum = attributes.get(MFS_MIN_KEY)
        maximum = attributes.get(MFS_MAX_KEY)
        limits = (
            f"{minimum}-{maximum}%"
            if minimum is not None and maximum is not None
            else "n/a"
        )
        return (
            f"min-fan={self._format_percent(attributes.get(MFS_KEY))} "
            f"limits={limits} "
            f"profile={attributes.get(PROFILE_KEY) or '?'} "
            f"offset={attributes.get(OFFSET_KEY) or '?'}"
        )

    def set_min_fan(self, value: int) -> str:
        attributes = self.manager_attributes((MFS_KEY,))
        path = self._attributes_uri
        if path is None:
            raise RedfishError("no Redfish manager attributes URI found")
        before = attributes.get(MFS_KEY)

        minimum = attributes.get(MFS_MIN_KEY)
        maximum = attributes.get(MFS_MAX_KEY)
        if value != MFS_DEFAULT and minimum is not None and maximum is not None:
            try:
                low, high = int(minimum), int(maximum)
            except (TypeError, ValueError):
                low = high = None
            if low is not None and high is not None and not low <= value <= high:
                raise RedfishError(
                    f"{value}% is outside this server's reported range "
                    f"{low}-{high}% (use 'default' to remove the floor)"
                )

        self._request("PATCH", path, {"Attributes": {MFS_KEY: value}})

        current: Any = value
        for attempt in range(10):
            data = self.get_json(path)
            if data and isinstance(data.get("Attributes"), dict):
                self._attributes = data["Attributes"]
                current = data["Attributes"].get(MFS_KEY, value)
            if str(current) == str(value):
                break
            if attempt < 9:
                time.sleep(1)

        if str(current) != str(value):
            raise RedfishError(
                "MinimumFanSpeed request was accepted but the iDRAC still reports "
                f"{self._format_percent(current)} (requested "
                f"{self._format_percent(value)})"
            )
        return (
            f"MinimumFanSpeed {self._format_percent(before)} -> "
            f"{self._format_percent(current)}"
        )

    def set_profile(self, profile: str) -> str:
        attributes = self.manager_attributes((PROFILE_KEY,))
        path = self._attributes_uri
        if path is None:
            raise RedfishError("no Redfish manager attributes URI found")
        before = attributes.get(PROFILE_KEY)
        value = PROFILES[profile]

        self._request("PATCH", path, {"Attributes": {PROFILE_KEY: value}})

        current: Any = value
        for attempt in range(10):
            data = self.get_json(path)
            if data and isinstance(data.get("Attributes"), dict):
                self._attributes = data["Attributes"]
                current = data["Attributes"].get(PROFILE_KEY, value)
            if current == value:
                break
            if attempt < 9:
                time.sleep(1)

        if current != value:
            raise RedfishError(
                "ThermalProfile request was accepted but the iDRAC still reports "
                f"{current} (requested {value})"
            )
        return f"ThermalProfile {before or '?'} -> {current}"

    def identify(self, state: str) -> str:
        uri, chassis = self.chassis()
        if "LocationIndicatorActive" in chassis:
            field = "LocationIndicatorActive"
            values: dict[str, Any] = {"on": True, "blink": True, "off": False}
        else:
            field = "IndicatorLED"
            values = {"on": "Lit", "blink": "Blinking", "off": "Off"}

        current = chassis.get(field)
        if state == "status":
            if isinstance(current, bool):
                current = "on" if current else "off"
            return f"{field}={current if current is not None else 'n/a'}"

        self._request("PATCH", uri, {field: values[state]})
        data = self.get_json(uri)
        current = (data or {}).get(field, values[state])
        return f"{state}: {field}={current}"


def _run_command(server: Server, args: argparse.Namespace) -> str:
    if args.command == "status":
        return server.status()
    if args.command == "power":
        if args.action == "status":
            return server.status()
        return server.power(args.action)
    if args.command == "fan":
        if args.fan_command == "show":
            return server.show_fan()
        if args.fan_command == "min-speed":
            return server.set_min_fan(args.value)
        if args.fan_command == "profile":
            return server.set_profile(args.profile)
        raise RedfishError(f"unknown fan command: {args.fan_command}")
    if args.command == "identify":
        return server.identify(args.state)
    raise RedfishError(f"unknown command: {args.command}")


def _parse_fan_value(raw: str) -> int:
    lowered = raw.strip().lower()
    if lowered in {"default", "auto", "off", "255"}:
        return MFS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected a percentage (0-100) or 'default'"
        ) from None
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("percentage must be between 0 and 100")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-u",
        "--username",
        "--user",
        default=os.environ.get("REDFISH_USER") or os.environ.get("IDRAC_USER"),
        help="BMC username (default: REDFISH_USER or IDRAC_USER)",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=os.environ.get("REDFISH_PASS") or os.environ.get("IDRAC_PASS"),
        help="BMC password (default: REDFISH_PASS or IDRAC_PASS)",
    )
    parser.add_argument(
        "-H",
        "--host",
        action="append",
        default=[],
        metavar="HOST",
        help="BMC address, optionally NAME=ADDRESS; repeat for each server",
    )
    parser.add_argument(
        "--hosts",
        action="append",
        default=[],
        metavar="HOST[,HOST...]",
        help="comma-separated BMC addresses",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="use every redfish_ip in inventory/manual/devices/*.yaml",
    )
    parser.add_argument(
        "--inventory-dir",
        default=str(DEFAULT_INVENTORY_DIR),
        help="inventory directory used by --all (default: %(default)s)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=8,
        metavar="N",
        help="servers to contact in parallel (default: 8)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-request timeout (default: 30)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify TLS certificates (default: off for iDRAC self-signed certs)",
    )
    parser.add_argument(
        "--ca-cert",
        metavar="PATH",
        help="CA bundle to verify TLS certificates with",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print every HTTP request",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="show power state, health, model and fan floor")

    power = commands.add_parser("power", help="power control")
    power.add_argument(
        "action",
        choices=[*POWER_ACTIONS, "status"],
        help="power action; 'off' is a graceful shutdown, 'force-off' is immediate",
    )

    fan = commands.add_parser("fan", help="fan and thermal settings")
    fan_commands = fan.add_subparsers(dest="fan_command", required=True)
    fan_commands.add_parser("show", help="show current fan settings")
    min_speed = fan_commands.add_parser(
        "min-speed",
        help='set "Minimum Fan Speed in PWM (%% of Max)"',
    )
    min_speed.add_argument(
        "value",
        type=_parse_fan_value,
        metavar="PERCENT|default",
        help="PWM floor 0-100, or 'default' to let the iDRAC decide",
    )
    profile = fan_commands.add_parser("profile", help="set the thermal profile")
    profile.add_argument("profile", choices=list(PROFILES))

    identify = commands.add_parser("identify", help="control the chassis locate LED")
    identify.add_argument(
        "state",
        nargs="?",
        default="status",
        choices=["on", "off", "blink", "status"],
    )
    return parser


def _collect_targets(args: argparse.Namespace) -> list[Target]:
    entries: list[str] = list(args.host)
    for value in args.hosts:
        entries.extend(part for part in value.split(",") if part.strip())

    targets: list[Target] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            target = _parse_target(entry)
        except ValueError as exc:
            raise RedfishError(str(exc)) from exc
        key = _normalize_base(target.address)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)

    if args.all:
        for target in _inventory_targets(Path(args.inventory_dir)):
            key = _normalize_base(target.address)
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)

    if not targets:
        raise RedfishError("no servers selected; use --host, --hosts or --all")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.username:
        parser.error("--username is required (or set REDFISH_USER/IDRAC_USER)")
    if not args.password:
        parser.error("--password is required (or set REDFISH_PASS/IDRAC_PASS)")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")

    try:
        targets = _collect_targets(args)
    except RedfishError as exc:
        parser.error(str(exc))

    lock = threading.Lock()

    def work(target: Target) -> tuple[Target, bool, str]:
        server = Server(
            target,
            args.username,
            args.password,
            timeout=args.timeout,
            verify=args.verify,
            ca_cert=args.ca_cert,
            debug=args.verbose,
        )
        try:
            return target, True, _run_command(server, args)
        except RedfishError as exc:
            return target, False, str(exc)
        except Exception as exc:  # noqa: BLE001 - isolate a broken server
            return target, False, f"unexpected error: {exc}"

    succeeded = 0
    with ThreadPoolExecutor(max_workers=min(args.jobs, len(targets))) as pool:
        futures = [pool.submit(work, target) for target in targets]
        for future in as_completed(futures):
            target, ok, message = future.result()
            label = target.label
            netloc = urllib.parse.urlsplit(_normalize_base(target.address)).netloc
            if label not in {target.address, netloc}:
                label = f"{label} ({target.address})"
            with lock:
                print(f"{'OK  ' if ok else 'FAIL'} {label}: {message}", flush=True)
            if ok:
                succeeded += 1

    if len(targets) > 1:
        print(f"\n{succeeded}/{len(targets)} servers succeeded", flush=True)
    return 0 if succeeded == len(targets) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
