# Scripts

All operational scripts live here. Configuration, machine patches and generated
state stay in `talos/` and `flux/`; downloaded CLI tools live in `scripts/bin/`
(gitignored).

## Tools

```sh
./scripts/download-tools.py                 # talosctl + kubectl + flux
./scripts/download-tools.py talosctl        # one tool
./scripts/download-tools.py flux --force    # re-download
```

Versions come from `talos/settings.env` (`TALOS_VERSION`,
`KUBERNETES_VERSION`) and the built-in Flux default. They can be overridden with
`TALOS_VERSION`, `KUBECTL_VERSION` and `FLUX_VERSION`. The other scripts never
download tools; if one is missing they print the command above.

## Talos

```sh
./scripts/talos/gen-iso.py [--download]      # Image Factory schematic + ISO URLs
./scripts/talos/setup-images.py [--print]    # per-node maintenance-mode ISOs
./scripts/talos/gen-config.py                # secrets + machine configs + talosconfig
./scripts/talos/manage.py update <node>      # apply regenerated config to a running node
./scripts/talos/manage.py <command>          # other day-2 operations
```

`gen-config.py`, `gen-iso.py` and `setup-images.py` need PyYAML
(`python3 -m pip install pyyaml`).

## Flux

```sh
export GITHUB_TOKEN=github_pat_...
./scripts/flux/setup.py                      # bootstrap Flux
./scripts/flux/manage.py status              # status / sync / suspend / resume / logs ...
```

Both use `talos/generated/kubeconfig` by default; override with `KUBECONFIG`.

## NetBox

The NetBox inventory data and usage notes live in
[`inventory/`](../inventory/README.md). The collector, sync, language server
and VS Code extension live in the separate
[netbox-inventory-toolkit](https://github.com/Blalange/netbox-inventory-toolkit)
repository and are pinned to `v0.1.2` here.

```sh
# collect live sources into inventory/generated
UNIFI_URL=https://unifi.glem.men \
uvx --from "netbox-inventory-toolkit[all] @ git+https://github.com/Blalange/netbox-inventory-toolkit@v0.1.2" \
  netbox-inventory-toolkit collect --inventory inventory --sources auto \
  --unifi-secret monitoring/unpoller-unifi \
  --kubectl ./scripts/bin/kubectl --kubeconfig talos/generated/kubeconfig \
  --talosctl ./scripts/bin/talosctl --talosconfig talos/generated/talosconfig

# validate the merged inventory
uvx --from "netbox-inventory-toolkit[all] @ git+https://github.com/Blalange/netbox-inventory-toolkit@v0.1.2" \
  netbox-inventory-toolkit sync --inventory inventory --check --no-dtl
```

The collector also polls SNMP v2c network devices (default community
`public`) for interfaces, VLANs and LLDP/FDB diagnostics. Use
`--snmp-community`, `--no-snmp` or `--snmp-report PATH` as needed. BMCs without
Redfish (iDRAC 6, IBM IMM) are polled with `ipmitool`; install it locally and
set `IPMI_USER` / `IPMI_PASS` if the factory `root` / `calvin` account is not
enabled.

The sync enriches device and module types from the Apache-2.0 NetBox Device
Type Library (`--dtl`, enabled by default). Definitions and elevation images
are fetched at runtime and cached outside Git; manual inventory values and
components always win. The CronJob enables this with `DTL_ENABLED=true` and
caches under the NetBox media volume.

## Redfish

`scripts/redfish.py` sends one command to many server BMCs in parallel. It
works with Dell iDRACs and other Redfish implementations.

```sh
# one server, several servers, or everything in inventory/manual/devices
./scripts/redfish.py --host 192.168.4.130 --username root --password '...' status
./scripts/redfish.py --hosts 192.168.4.130,192.168.4.150 \
  --username root --password '...' power restart
./scripts/redfish.py --all --username root --password '...' fan min-speed 30

# names are only for output; the address may also include a scheme/port
./scripts/redfish.py --host glemmen130=192.168.4.130 --username root \
  --password '...' fan show
```

Commands:

```text
status                              power state, health, model and fan floor
power on|off|force-off|restart|reset|cycle
fan show
fan min-speed <0-100|default>       "Minimum Fan Speed in PWM (% of Max)"
fan profile <profile>               default, maximum-performance, minimum-power, sound-cap
identify [on|off|blink|status]
```

`fan min-speed` writes the iDRAC attribute
`ThermalSettings.1.MinimumFanSpeed`; `default` writes Dell's `255` sentinel and
removes the PWM floor. The script reads the value back and reports a failure if
the iDRAC does not accept it. `off` is a graceful shutdown, `force-off` is
immediate, and `reset` is a hard reset; `restart` is a graceful restart.

Credentials can also come from `REDFISH_USER` / `REDFISH_PASS` (or
`IDRAC_USER` / `IDRAC_PASS`). TLS verification is off by default because iDRACs
use self-signed certificates; use `--verify` or `--ca-cert PATH` to enable it.
Use `--jobs N` to limit parallelism (`8` by default) and `--timeout SECONDS`
for slow BMCs.

The script needs `requests`, and `--all` additionally needs PyYAML:

```sh
uv run --with requests --with pyyaml python scripts/redfish.py --help
```

## Updates

```sh
./scripts/check-updates.py                  # Talos, Kubernetes, Flux, Helm, images
./scripts/check-updates.py --no-images      # skip registry lookups
./scripts/check-updates.py --json           # machine-readable output
./scripts/check-updates.py --fail-on-updates
```

The checker compares `TALOS_VERSION` and `KUBERNETES_VERSION` from
`talos/settings.env`, reads HelmRelease/OCIRepository versions from the Flux
manifests, and scans YAML for container image tags. `GITHUB_TOKEN` is optional
but avoids GitHub API rate limits. Tuppr's `TalosUpgrade` and
`KubernetesUpgrade` versions are generated from the same settings file via
Kustomize replacements.
