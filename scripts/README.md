# Scripts

All operational scripts live here. Configuration, machine patches and generated
state stay in `talos/` and `flux/`; downloaded CLI tools live in `scripts/bin/`
(gitignored).

## Tools

```sh
./scripts/download-tools.py                 # talosctl + kubectl + flux + pv-migrate
./scripts/download-tools.py talosctl        # one tool
./scripts/download-tools.py pv-migrate      # PVC migration helper
./scripts/download-tools.py flux --force    # re-download
```

Versions come from `talos/settings.env` (`TALOS_VERSION`,
`KUBERNETES_VERSION`) and the built-in Flux and pv-migrate defaults. They can be
overridden with `TALOS_VERSION`, `KUBECTL_VERSION`, `FLUX_VERSION` and
`PV_MIGRATE_VERSION`. The other scripts never download tools; if one is
missing they print the command above.

## PersistentVolumeClaims

`pv-migrate` copies the contents of one PVC into another. It is useful for
moving a volume to a different StorageClass or, as with the Prometheus volume,
for shrinking a volume: Kubernetes cannot reduce a bound PVC in place, so the
destination PVC is created at the smaller size and `--ignore-sizes` allows the
copy when the old claim is larger.

Stop the workload using the PVC first and create the destination PVC (same
namespace, smaller size). Then run:

```sh
./scripts/bin/pv-migrate \
  --source-namespace monitoring --source old-pvc \
  --dest-namespace monitoring --dest new-pvc \
  --ignore-sizes
```

For a long copy, use `--detach`; the job continues in the cluster:

```sh
./scripts/bin/pv-migrate ... --detach --id my-migration
./scripts/bin/pv-migrate status my-migration --follow
./scripts/bin/pv-migrate cleanup my-migration
```

The source and destination PVCs must not be mounted by other pods. Copying a
subdirectory is possible with `--source-path` and `--dest-path`.

Keep the old claim until the workload has started successfully on the new one.
The `monitoring` StorageClass uses `reclaimPolicy: Retain`, so deleting the old
PVC and PV does not delete the backing Longhorn volume; remove the leftover
`Volume` resource in `longhorn-system` as well when it is no longer needed for
rollback. Re-add the Longhorn volume labels (notably
`recurring-job-group.longhorn.io/default=enabled`, `backup-target` and
`backup-volume`) on the new volume so recurring backups keep running.

## Talos

```sh
./scripts/talos/gen-iso.py [--download]      # Image Factory schematic + ISO URLs
./scripts/talos/setup-images.py [--print]    # per-node maintenance-mode ISOs
./scripts/talos/gen-config.py                # secrets + machine configs + talosconfig
./scripts/talos/gen-pxe.py                   # render the PXE iPXE boot script
./scripts/talos/manage.py update <node>      # apply regenerated config to a running node
./scripts/talos/manage.py <command>          # other day-2 operations
```

`gen-config.py`, `gen-iso.py`, `gen-pxe.py` and `setup-images.py` need PyYAML
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
repository and are pinned to `0.3.2` here.

```sh
# validate collector settings and inventory/profile coverage
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.3.2" \
  netbox-inventory-toolkit config check --inventory inventory

# collect live sources into inventory/generated
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.3.2" \
  netbox-inventory-toolkit collect --inventory inventory

# suggest manual entries from the inventory and live endpoints
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.3.2" \
  netbox-inventory-toolkit discover --inventory inventory --scan-inventory

# validate the merged inventory
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.3.2" \
  netbox-inventory-toolkit sync --inventory inventory --check --no-dtl
```

Source credentials, SNMP communities, Redfish/IPMI accounts, UniFi controllers
and Talos clusters live in `inventory/settings.yml`. The collector polls SNMP
v2c network devices for interfaces, VLANs and LLDP/FDB diagnostics, and BMCs
without Redfish (iDRAC 6, IBM IMM) with `ipmitool`; install it locally if needed.

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
