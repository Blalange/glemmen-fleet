# NetBox inventory

[NetBox](https://netbox.dev) is the cluster's network documentation, DCIM and
IPAM visualization. It is served at <https://netbox.glem.men>.

This directory contains the **Glemmen-specific inventory data** only. The
collector, NetBox sync, language server and VS Code extension live in the
separate [netbox-inventory-toolkit](https://github.com/Blalange/netbox-inventory-toolkit)
repository and are consumed at the pinned `0.2.1` release.

## Layout

```text
inventory/
├── kustomization.yaml   # NetBox inventory/image ConfigMaps
├── generated/           # written by the collector; do not edit
│   ├── network.yaml     # UniFi + SNMP devices, VLANs, prefixes, cables
│   ├── compute.yaml     # servers/nodes and discovered modules
│   └── module-types.yaml
├── manual/              # hand-maintained inventory; wins over generated
│   ├── defaults.yaml
│   ├── site.yaml        # site, roles, collector settings, UniFi model map
│   ├── device-types.yaml
│   ├── module-types.yaml
│   ├── racks.yaml
│   ├── cables.yaml
│   ├── ipam.yaml
│   ├── owners.yaml
│   ├── contacts.yaml
│   └── devices/         # Talos, servers, network, storage and power devices
└── images/              # device-type front/rear/other images
```

The generic schema, natural keys, rack layout, ownership and source adapter
documentation moved to the toolkit repository:

- <https://github.com/Blalange/netbox-inventory-toolkit/blob/main/docs/inventory-schema.md>
- <https://github.com/Blalange/netbox-inventory-toolkit/blob/main/docs/collector.md>
- <https://github.com/Blalange/netbox-inventory-toolkit/blob/main/docs/sync.md>

## Glemmen collector settings

`manual/site.yaml` configures the generic toolkit for this site:

```yaml
settings:
  site: glemmen
  default_tag: production
  rack: Glemmen rack
  talos_platform: talos-linux
  roles:
    control_plane: control-plane
    worker: worker
  compute_roles:
    - server
  snmp:
    port_names: port-number
    auto_unifi: true
  unifi_models: { ... }
  unifi_vlan_names: { ... }
```

`snmp.port_names: port-number` preserves the existing `Port N` names for the
Zyxel switch and UniFi devices. Other sites can use `auto` or `ifname`.

## Sources used here

| Source | Glemmen setup |
|---|---|
| UniFi | `UNIFI_URL=https://unifi.glem.men`, credentials from `UNIFI_USER`/`UNIFI_PASS` or `--unifi-secret monitoring/unpoller-unifi` |
| SNMP | manual `snmp_ip` on the UDM Pro, USW Pro 24 PoE and Zyxel GS1920-24; Zyxel has `snmp_interfaces: true` and `snmp_cables: true` |
| Talos | `--kubeconfig talos/generated/kubeconfig`, `--talosctl ./scripts/bin/talosctl`, `--talosconfig talos/generated/talosconfig` |
| Redfish | R250/R630 iDRACs with `redfish_ip`; read-only `readonly`/`readonly` account |
| IPMI | iDRAC 6 on `glemmen60` and the IBM IMM on `glemmen50` via `ipmi_ip` |

The collector merges sources into the generated layer. Redfish is preferred
over Talos for compute components; Talos is the fallback. IPMI supplies fan/PSU
data for BMCs without Redfish. SNMP only fills missing network fields unless a
device opts in with `snmp_interfaces` / `snmp_cables`.

## Discovering devices

The toolkit can suggest manual entries from the existing inventory and probe
Redfish/SNMP/IPMI endpoints:

```sh
# inventory-only suggestions (no network traffic)
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit discover --inventory inventory

# probe the addresses already configured in the inventory
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit discover --inventory inventory --scan-inventory

# scan the OOB network for Redfish and SNMP
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit discover --inventory inventory \
  --network 192.168.4.0/24 --protocols redfish,snmp

# write complete new entries to a review file
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit discover --inventory inventory --scan-inventory \
  --write-new inventory/manual/discovered.yaml
```

Discovery never edits existing manual files. Review `discovered.yaml` before
committing it and add it to `inventory/kustomization.yaml` if used in-cluster.
See the toolkit's `docs/discovery.md` for all options.

## Running the collector

Use the pinned toolkit revision:

```sh
UNIFI_URL=https://unifi.glem.men \
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit collect \
  --inventory inventory \
  --sources auto \
  --unifi-secret monitoring/unpoller-unifi \
  --kubeconfig talos/generated/kubeconfig \
  --talosctl ./scripts/bin/talosctl \
  --talosconfig talos/generated/talosconfig
```

UniFi credentials come from the environment or from a Secret:

```sh
UNIFI_URL=https://unifi.glem.men \
UNIFI_USER=... UNIFI_PASS=... \
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit collect --inventory inventory --sources auto
```

Or read the existing `monitoring/unpoller-unifi` Secret:

```sh
UNIFI_URL=https://unifi.glem.men \
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit collect \
  --inventory inventory \
  --unifi-secret monitoring/unpoller-unifi \
  --kubeconfig talos/generated/kubeconfig \
  --talosctl ./scripts/bin/talosctl \
  --talosconfig talos/generated/talosconfig
```

The collector never edits `inventory/manual/`.

## Running the sync

The CronJob is the normal path. To trigger a run now:

```sh
kubectl -n netbox create job --from=cronjob/netbox-sync netbox-sync-manual
kubectl -n netbox logs -f job/netbox-sync-manual
```

Validate or preview locally with the same pinned toolkit:

```sh
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit sync --inventory inventory --check --no-dtl

NETBOX_URL=https://netbox.glem.men \
NETBOX_TOKEN="$(kubectl -n netbox get secret netbox-secrets \
  -o jsonpath='{.data.api_token_full}' | base64 -d)" \
uvx --python 3.13 --from "netbox-inventory-toolkit[all]==0.2.1" \
  netbox-inventory-toolkit sync --inventory inventory --dry-run --prune
```

`--check` validates references, unique keys, rack bounds and overlaps without
contacting NetBox. `--dry-run` prints the exact create/update/delete plan.
`--prune` is required for deletions. The `netbox-sync` CronJob runs with
`PRUNE=true` every 10 minutes.

## Deployment

The Flux Kustomization `flux/clusters/glemmen/apps/netbox-inventory.yaml`
builds this directory into three ConfigMaps:

- `netbox-inventory-data` — the YAML inventory
- `netbox-device-type-images` — device-type images
- the app Kustomization depends on it so the CronJob starts only after the
  inventory is present

The `netbox-sync` CronJob in `flux/apps/netbox/` uses the published image
`ghcr.io/blalange/netbox-inventory-toolkit:v0.2.1`; it no longer installs
Python packages at runtime or mounts a code ConfigMap.

The GHCR package must be public for the cluster to pull it anonymously. If it
is still private, make it public in the GitHub package settings or add a
`ghcr-pull` image pull secret and reference it from the CronJob.

## Device types and images

Device types use real manufacturer/model names. `manual/device-types.yaml`
may reference the community Device Type Library and image files. The sync
fetches DTL definitions/elevation images at runtime and caches them on the
`netbox-media` volume. Manual values win; DTL fills gaps.

Images are shipped in `netbox-device-type-images`, which must stay below the
Kubernetes ConfigMap limit. Keep the raw image directory below roughly 2 MiB
and resize/compress large photos.

## Secrets

Nothing secret is stored in Git. NetBox secrets live in the `netbox-secrets`
Secret in the `netbox` namespace; see the "Secrets" section of the toolkit's
sync documentation for the key list. UniFi credentials are read from
`monitoring/unpoller-unifi` (or the environment) and are never written to Git.

## Topology views

`netbox-topology-views` provides the cable/topology map. The cable data between
the UniFi devices, the Zyxel switch and the UDM Pro supplies the graph:

<https://netbox.glem.men/plugins/netbox_topology_views/topology/?draw_init=true&show_cables=True&show_unconnected=True>

## First start and verification

1. Create `netbox-secrets`.
2. Reconcile the inventory ConfigMaps and the app:
   `./scripts/flux/manage.py reconcile netbox-inventory netbox`
3. Wait for `netbox`, `netbox-worker`, `netbox-postgres` and `netbox-valkey`.
4. Open <https://netbox.glem.men>, log in as `admin`, and confirm the site,
   devices, interfaces, prefixes and cables.
5. Check the Prometheus target `netbox` in Grafana.
6. Trigger a sync and inspect the result:
   `kubectl -n netbox logs job/netbox-sync-manual`.

## Upgrading

Bump the pinned toolkit tag in the commands, the VS Code settings and the
CronJob image together. NetBox itself is upgraded by bumping
`chart.spec.version` in `flux/apps/netbox/release.yaml`; take a Longhorn
backup/snapshot of `netbox-postgres` first.
