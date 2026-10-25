# NetBox

[NetBox](https://netbox.dev) is the cluster's network documentation, DCIM and
IPAM visualization. It is served at <https://netbox.glem.men>.

Git is the source of truth. The inventory is split into two layers:

- `inventory/generated/` is written by
  `scripts/netbox/generate_inventory.py` from the live UniFi controller and the
  Kubernetes/Talos nodes. Do not edit it by hand.
- `inventory/manual/` is edited by hand. It owns everything that is not
  discoverable: real device models, the physical rack layout, physical servers,
  accessories, hand-documented cables and VIPs.

The `netbox-sync` CronJob merges both layers every 10 minutes and reconciles
the result into NetBox. Manual values win when the same object appears in both
layers. Anonymous visitors get read-only access to the UI; the sync job is the
only writer. Objects created by hand in NetBox are never deleted by the sync.

## Components

| Resource | Purpose |
|---|---|
| `release.yaml` | Official NetBox Helm chart `8.3.93` (NetBox `v4.7.3`) |
| `postgres.yaml` | PostgreSQL `18.6` StatefulSet with an 8Gi Longhorn volume |
| `media-pvc.yaml` | 2Gi Longhorn RWX volume for uploaded media |
| `valkey.yaml` | Ephemeral Valkey `9.1.2` for the RQ queue and cache |
| `sync-cronjob.yaml` | Runs `sync.py` every 10 minutes |
| `sync.py` | The pynetbox reconciler |
| `inventory/generated/` | UniFi, server and Talos facts from the collector |
| `inventory/manual/` | Human-maintained inventory |
| `inventory/images/` | Device-type front/rear/other images |
| `storageclass.yaml` | `netbox` (RWO) and `netbox-media` (RWX) Longhorn classes |

Media uploads use a 2Gi Longhorn **ReadWriteMany** volume. Longhorn implements
RWX with its NFS share-manager, so the web and worker pods can run on different
nodes and share the same media directory. Postgres uses a normal RWO volume.

## Inventory layout

```
inventory/
├── generated/
│   ├── unifi.yaml       # adopted UniFi devices, VLANs, prefixes, uplinks
│   ├── servers.yaml     # physical servers discovered through UniFi
│   └── talos.yaml       # Talos nodes, interfaces, addresses, status, modules
├── manual/
│   ├── site.yaml        # site, manufacturers, platforms, roles, module bay types
│   ├── device-types.yaml# real models and image references
│   ├── module-types.yaml# module types for manually modelled components
│   ├── racks.yaml       # rack height and top-to-bottom layout
│   ├── devices.yaml     # servers, nodes, accessories, unifi_ip/talos_node/idrac_ip links
│   ├── cables.yaml      # manual cables not discoverable from UniFi
│   └── ipam.yaml        # VIPs and standalone addresses
└── images/              # device-type images
```

Every YAML document declares its layer:

```yaml
layer: manual
```

The loader merges by natural key (device name, device-type slug, rack/site,
VLAN/site+VID, prefix, IP address, cable endpoints). Duplicate keys inside one
layer are an error; a manual object merges into the generated object with the
same key.

### Rack layout

Racks use a top-to-bottom list instead of per-device `position` fields. The sync
job computes NetBox positions from each device type's `u_height` and validates
bounds and overlaps.

```yaml
racks:
  - name: Glemmen rack
    site: glemmen
    u_height: 42
    layout:
      - gap: 1
      - device: Dream Machine Pro B bygg
        face: rear
      - device: USW Pro 24 PoE
        face: rear
      - gap: 2
      - device: 240emy
      # ...
      - device: glemmen60
      - gap: 4
```

Each entry is a device name, or `gap: N` for empty units. An optional
`face: front|rear` marks devices that are mounted facing the back of the rack
(the two rack switches, for example).

Moving a device is a one-line layout change; everything above/below follows.
Changing the rack height means editing `u_height` and the layout together.

### Physical servers and accessories

`manual/devices.yaml` is the hardware catalogue. Entries with `unifi_ip` are
enriched by the collector; entries with `talos_node` are merged with the Talos
node data. Devices without either are created from the manual layer only.

```yaml
devices:
  - name: server70
    role: server
    device_type: dell-poweredge-r630
    unifi_ip: 192.168.6.70
    uplinks:
      - interface: Port 1
        switch: USW Pro 24 PoE
        port: 24
        type: cat6

  - name: server200
    role: server
    device_type: dell-poweredge-r250
    status: failed
    description: Physical server between glemmen210 and server190. Currently broken.
```

`uplinks` entries are expanded into cables, so a server's NIC-to-switch-port
map lives next to the server instead of in Python. The collector creates the
matching physical interfaces; the management address is assigned to the
physical uplink interface rather than a virtual `mgmt` interface. The same
applies to UniFi switches: their management IP goes on the uplink port. The
UDM's LAN/VPN gateway addresses use a virtual `lan` interface because they are
logical L3 interfaces, while its WAN address stays on Port 9.

### Device components

Field-replaceable components are modelled as NetBox **modules**, not
inventory items (inventory items are deprecated in NetBox). Each component
installed in a physical slot is represented by a module type, a module bay on
the device, and a module instance in that bay.

The collector discovers CPU, memory and disk modules from Talos. Any manual
device with an `idrac_ip` also uses the Redfish API for richer component data
(exact memory manufacturer/part number, disk RPM and cooling fans). This works
for Talos nodes, UniFi-discovered servers and servers that exist only in the
manual layer. Redfish uses the read-only `readonly` account (password
`readonly`) by default; override it with `IDRAC_USER` / `IDRAC_PASS`. The
default is a read-only account; any override stays in the environment and is
never written to Git.

`idrac_ip` can be added to any manual device, including servers that have no
`unifi_ip` and no `talos_node`. If an iDRAC is unreachable or rejects the
credentials, Talos nodes fall back to Talos hardware data and non-Talos servers
simply keep their manually modelled components.

Manual devices can declare their own components:

```yaml
module_bay_types:
  - name: 3.5in drive
    slug: drive-3-5

module_types:
  - manufacturer: generic
    model: 12 TB SAS HDD
    slug: generic-12tb-sas-hdd
    profile: Hard disk
    attributes:
      size: 12000
      type: HD
      speed: 7200
    bay_types:
      - drive-3-5

devices:
  - name: storwize-01
    module_bays:
      - name: Drive 1
        position: 1
        bay_type: drive-3-5
    modules:
      - bay: Drive 1
        module_type: generic-12tb-sas-hdd
        status: staged
```

Module types use the built-in profiles (`CPU`, `Memory`, `Hard disk`, `Fan`,
`Power supply`, `Expansion card`, …). Their typed attributes are validated by
NetBox, and module bay types enforce compatibility (for example, a 3.5in drive
cannot be installed in a 2.5in bay).

### Device types and images

Device types use real manufacturer/model names. `manual/device-types.yaml`
references image files by name:

```yaml
device_types:
  - manufacturer: dell
    model: PowerEdge R250
    slug: dell-poweredge-r250
    u_height: 1
    images:
      front: images/dell-poweredge-r250-front.jpg
      rear: images/dell-poweredge-r250-rear.jpg
      other:
        - name: Server front
          file: images/dell-poweredge-r250-other.jpg
          description: Front view with drive bays.
```

`sync.py` uploads `front_image` and `rear_image` through the NetBox multipart
API and stores additional images as `dcim.devicetype` image attachments. NetBox
renames uploaded files, so the sync uploads an image once and leaves it alone
on later runs. Use `--force-images` (or `FORCE_IMAGES=true`) to replace an
image after changing its file.

## Topology views

`netbox-topology-views` 5.0.0 (the release for NetBox 4.7.x) is installed at
pod startup. An init container installs the wheel into a writable `/plugins`
emptyDir and runs `collectstatic` into a writable static volume; the web and
worker containers read both volumes through `PYTHONPATH=/plugins`. This keeps
the upstream NetBox image pinned and avoids maintaining a custom registry
image. The plugin adds **Topology Views** to the NetBox navigation; the existing
cable data between the UniFi devices, the Zyxel switch and the UDM Pro supplies
the graph. The graph's supporting models are in `exemptViewPermissions`, so
anonymous viewers can open the map without an account. The default plugin menu
link opens an empty canvas for anonymous users; the Homepage card links
directly to the populated view:

<https://netbox.glem.men/plugins/netbox_topology_views/topology/?draw_init=true&show_cables=True&show_unconnected=True>

## Secrets

Nothing secret is stored in Git. Create the namespace and `netbox-secrets`
before reconciling the app:

```sh
kubectl create namespace netbox --dry-run=client -o yaml | kubectl apply -f -

API_TOKEN="$(openssl rand -hex 20)"
API_TOKEN_KEY="$(openssl rand -hex 6)"
kubectl -n netbox create secret generic netbox-secrets \
  --from-literal=username=admin \
  --from-literal=password="$(openssl rand -base64 24)" \
  --from-literal=email=admin@glem.men \
  --from-literal=api_token="$API_TOKEN" \
  --from-literal=api_token_key="$API_TOKEN_KEY" \
  --from-literal=api_token_full="nbt_${API_TOKEN_KEY}.${API_TOKEN}" \
  --from-literal=secret_key="$(openssl rand -hex 32)" \
  --from-literal=api_token_peppers="{\"1\":\"$(openssl rand -hex 32)\"}" \
  --from-literal=db-password="$(openssl rand -base64 24)" \
  --from-literal=valkey-password="$(openssl rand -base64 24)" \
  --from-literal=email_password="" \
  --dry-run=client -o yaml | kubectl apply -f -
```

| Key | Used by |
|---|---|
| `username`, `password`, `email`, `api_token`, `api_token_key` | NetBox superuser bootstrap (v2 token) |
| `api_token_full` | Sync CronJob (`nbt_<key>.<secret>`) |
| `secret_key`, `api_token_peppers`, `email_password` | NetBox Django config |
| `db-password` | PostgreSQL and NetBox |
| `valkey-password` | Valkey and NetBox |

Read the generated admin password with:

```sh
kubectl -n netbox get secret netbox-secrets \
  -o jsonpath='{.data.password}' | base64 -d
```

The PostgreSQL entrypoint only creates the database and user while the data
directory is empty. Changing `db-password` later does not change the password
inside PostgreSQL; rotate it with SQL as well. The superuser password and API
token are bootstrap values: rotate them in the NetBox UI/API and then update
the Secret to match.

## Inventory sync

Collect live UniFi and Talos facts into `inventory/generated/`:

```sh
# Optional; the collector defaults to the read-only iDRAC account.
IDRAC_USER=... IDRAC_PASS=... \
uv run --with requests --with PyYAML \
  python scripts/netbox/generate_inventory.py --from-cluster \
  --kubectl ./scripts/bin/kubectl --kubeconfig talos/generated/kubeconfig \
  --talosctl ./scripts/bin/talosctl \
  --talosconfig talos/generated/talosconfig
```

`--from-cluster` reads the `unpoller-unifi` credentials from the monitoring
namespace; they are never written to the file. The collector reads the manual
`settings`, `unifi_models` and `devices` sections, then writes only the
generated layer. It never edits `inventory/manual/`. Devices with `idrac_ip`
use the Redfish credentials from `IDRAC_USER` / `IDRAC_PASS` (default
`readonly` / `readonly`) for additional components; overrides are never
written to Git.

The generated layer contains:

- UniFi adopted devices with ports/radios, management addresses and uplinks
- VLANs and prefixes from UniFi
- physical servers listed with `unifi_ip`, including their NIC/IP/uplink facts
- Talos node versions, roles, capacity, serials, addresses and interfaces
- NIC MAC addresses and CPU, memory and disk modules for every Talos node
- fan modules and richer component attributes from iDRAC Redfish where configured

Manual files contain the rest: real models, device-type images, module types,
rack layout, physical devices, accessories, manual cables and VIPs.

The CronJob tags every object it creates with `gitops`. With `PRUNE=true`, it
deletes objects that still carry that tag but are no longer present in the
inventory. Objects without the tag are left alone, so manual additions are
safe.

Supported top-level keys:

| Key | Notes |
|---|---|
| `layer` | `generated` or `manual`; mandatory for deterministic merging |
| `settings` | Collector settings (`site`, `default_tag`, `rack`); ignored by NetBox |
| `unifi_models` | UniFi model code → device-type slug; ignored by NetBox |
| `unifi_vlan_names` | UniFi VLAN ID → corrected name; ignored by NetBox |
| `sites` | Required for devices, racks, VLANs and prefixes |
| `manufacturers` | Referenced by device types and platforms |
| `platforms` | Referenced by devices |
| `device_roles` | Referenced by devices |
| `module_bay_types` | Referenced by module types and device module bays |
| `module_types` | Referenced by device modules; uses a built-in profile |
| `device_types` | Referenced by devices; may declare `images` |
| `racks` | Rack definitions plus declarative `layout` |
| `vlans` | Site-scoped or global |
| `prefixes` | Optionally attached to a site and VLAN |
| `devices` | Nested `interfaces`, `module_bays`, `modules`, `ip_addresses` and optional `uplinks` |
| `ip_addresses` | Standalone/unassigned IPs (VIPs, for example) |
| `cables` | One declaration per physical cable |
| `defaults` | Inherited by every device (site, tags, etc.) |

Natural keys used for matching are slugs for sites, manufacturers, platforms,
roles, module bay types and device types; `(site, name)` for racks;
`(site, vid)` for VLANs; the prefix for prefixes; the device name for devices;
`(device, name)` for interfaces and module bays; `(device, bay)` for modules;
the address for IPs; and the two terminations for cables. Adding a `slug`
explicitly is recommended.

Not modelled yet: VRFs, tenants, virtual machines, wireless, circuits, custom
fields, device-type interface templates and multi-termination cables.
IP addresses are global (no VRF). Add those objects in the UI if needed; they
will not carry the `gitops` tag and will not be pruned.

### Running the sync

The CronJob is the normal path:

```sh
# trigger a run now instead of waiting for the next 10-minute slot
kubectl -n netbox create job --from=cronjob/netbox-sync netbox-sync-manual
kubectl -n netbox logs -f job/netbox-sync-manual
```

Validate or preview the inventory locally with `uv`:

```sh
uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 --with requests==2.32.5 \
  python flux/apps/netbox/sync.py \
  --inventory flux/apps/netbox/inventory --check

NETBOX_URL=https://netbox.glem.men \
NETBOX_TOKEN="$(kubectl -n netbox get secret netbox-secrets \
  -o jsonpath='{.data.api_token_full}' | base64 -d)" \
uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 --with requests==2.32.5 \
  python flux/apps/netbox/sync.py \
  --inventory flux/apps/netbox/inventory --dry-run --prune
```

`--check` validates references, unique keys, rack bounds and overlaps without
contacting NetBox. `--dry-run` prints the exact create/update/delete plan.
`--prune` is required for deletions; without it the sync only creates and
updates. For rack moves that also delete an old device or cable, use `--prune`
so the old reservations are released before the new objects are created.

The CronJob is intentionally dependency-free apart from an init container that
installs the pinned Python packages into an emptyDir. This avoids maintaining a
custom image for a short-lived job.

## First start and verification

1. Create `netbox-secrets`.
2. Reconcile the app:
   `./scripts/flux/manage.py reconcile netbox`
3. Wait for `netbox`, `netbox-worker`, `netbox-postgres` and `netbox-valkey`.
4. Open <https://netbox.glem.men>, log in as `admin`, and confirm the site,
   devices, interfaces, prefixes and cables.
5. Check the Prometheus target `netbox` in Grafana.
6. Trigger a sync and inspect the result:
   `kubectl -n netbox logs job/netbox-sync-manual`.

## Upgrading

Bump `chart.spec.version` in `release.yaml`. NetBox runs database migrations
during the web pod startup. Read the upstream release notes before a minor
version jump, and take a Longhorn backup/snapshot of `netbox-postgres` first.
The chart's bundled PostgreSQL/Valkey subcharts stay disabled; keep
`postgres.yaml` and `valkey.yaml` on supported upstream versions.
