# NetBox

[NetBox](https://netbox.dev) is the cluster's network documentation, DCIM and
IPAM visualization. It is served at <https://netbox.glem.men>.

Git is the source of truth. The inventory lives in
`inventory/inventory.yaml`, and the `netbox-sync` CronJob reconciles it into
NetBox every 10 minutes. Anonymous visitors get read-only access to the UI; the
sync job is the only writer. Objects created by hand in NetBox are never
deleted by the sync.

## Components

| Resource | Purpose |
|---|---|
| `release.yaml` | Official NetBox Helm chart `8.3.93` (NetBox `v4.7.3`) |
| `postgres.yaml` | PostgreSQL `18.6` StatefulSet with an 8Gi Longhorn volume |
| `media-pvc.yaml` | 2Gi Longhorn RWX volume for uploaded media |
| `valkey.yaml` | Ephemeral Valkey `9.1.2` for the RQ queue and cache |
| `sync-cronjob.yaml` | Runs `sync.py` every 10 minutes |
| `sync.py` | The pynetbox reconciler |
| `inventory/inventory.yaml` | Desired state, edited in Git |
| `storageclass.yaml` | `netbox` (RWO) and `netbox-media` (RWX) Longhorn classes |

Media uploads use a 2Gi Longhorn **ReadWriteMany** volume. Longhorn implements
RWX with its NFS share-manager, so the web and worker pods can run on different
nodes and share the same media directory. Postgres uses a normal RWO volume.

## Topology views

`netbox-topology-views` 5.0.0 (the release for NetBox 4.7.x) is installed at
pod startup. An init container installs the wheel into a writable `/plugins`
emptyDir and runs `collectstatic` into a writable static volume; the web and
worker containers read both volumes through `PYTHONPATH=/plugins`. This keeps
the upstream NetBox image pinned and avoids maintaining a custom registry
image. The plugin adds **Topology Views** to the NetBox navigation; the
existing cable data between the UniFi devices, the Zyxel switch and the UDM Pro
supplies the graph. The graph's supporting models are in
`exemptViewPermissions`, so anonymous viewers can open the map without an
account. The default plugin menu link opens an empty canvas for anonymous
users; the Homepage card links directly to the populated view:

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

The shipped inventory was generated from the live UniFi controller and the
Kubernetes/Talos nodes:

```sh
uv run --with requests --with PyYAML \
  python scripts/netbox/generate_inventory.py --from-cluster \
  --kubectl ./scripts/bin/kubectl --kubeconfig talos/generated/kubeconfig
```

`--from-cluster` reads the `unpoller-unifi` credentials from the monitoring
namespace; they are never written to the file. UniFi devices, ports, radios,
management addresses and uplink cables are generated, as are the Talos node
versions, roles, capacity, serials and addresses. Re-run it after changing the
network, review the Git diff, and commit.

The CronJob tags every object it creates with `gitops`. With `PRUNE=true`, it
deletes objects that still carry that tag but are no longer present in the
inventory. Objects without the tag are left alone, so manual additions are
safe.

Supported top-level keys:

| Key | Notes |
|---|---|
| `sites` | Required for devices, racks, VLANs and prefixes |
| `manufacturers` | Referenced by device types and platforms |
| `platforms` | Referenced by devices |
| `device_roles` | Referenced by devices |
| `device_types` | Referenced by devices |
| `racks` | Optional; devices can be rack-mounted or rackless |
| `vlans` | Site-scoped or global |
| `prefixes` | Optionally attached to a site and VLAN |
| `devices` | Nested `interfaces` and `ip_addresses` |
| `ip_addresses` | Standalone/unassigned IPs (VIPs, for example) |
| `cables` | One declaration per physical cable |
| `defaults` | Inherited by every device (site, role, device type, tags, etc.) |

Example:

```yaml
defaults:
  site: glemmen
  role: kubernetes-node
  device_type: talos-node
  status: active

devices:
  - name: glemmen130
    interfaces:
      - name: bond0
        type: 1000base-t
        ip_addresses:
          - address: 192.168.6.130/24
            primary: true

  - name: zyxel-gs1920-24
    role: switch
    device_type: zyxel-gs1920-24
    interfaces:
      - name: Port 1
        type: 1000base-t
        untagged_vlan: 4

cables:
  - a_device: zyxel-gs1920-24
    a_interface: Port 28
    b_device: udm-pro
    b_interface: Port 8
    type: cat6
```

Natural keys used for matching are slugs for sites, manufacturers, platforms,
roles and device types; `(site, name)` for racks and devices; `(site, vid)` for
VLANs; the prefix for prefixes; `(device, name)` for interfaces; the address
for IPs; and the two terminations for cables. Adding a `slug` explicitly is
recommended. Device names must be unique across the inventory; prefix them with
the site name if you need the same hostname in multiple sites.

Not modelled yet: VRFs, tenants, virtual machines, modules, wireless, circuits,
custom fields, device-type interface templates and multi-termination cables.
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
uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 \
  python flux/apps/netbox/sync.py \
  --inventory flux/apps/netbox/inventory --check

NETBOX_URL=https://netbox.glem.men \
NETBOX_TOKEN="$(kubectl -n netbox get secret netbox-secrets \
  -o jsonpath='{.data.api_token}' | base64 -d)" \
uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 \
  python flux/apps/netbox/sync.py \
  --inventory flux/apps/netbox/inventory --dry-run --prune
```

`--check` validates references and uniqueness without contacting NetBox.
`--dry-run` prints the exact create/update/delete plan. `--prune` is required
for deletions; without it the sync only creates and updates.

The CronJob is intentionally dependency-free apart from an init container that
installs the two pinned Python packages into an emptyDir. This avoids
maintaining a custom image for a short-lived job.

## First start and verification

1. Create `netbox-secrets`.
2. Reconcile the app:
   `./scripts/flux/manage.py reconcile netbox`
3. Wait for `netbox`, `netbox-worker`, `netbox-postgres` and `netbox-valkey`.
4. Open <https://netbox.glem.men>, log in as `admin`, and confirm the site,
   devices, interfaces, prefixes and cable.
5. Check the Prometheus target `netbox` in Grafana.
6. Trigger a sync and inspect the result:
   `kubectl -n netbox logs job/netbox-sync-manual`.

## Upgrading

Bump `chart.spec.version` in `release.yaml`. NetBox runs database migrations
during the web pod startup. Read the upstream release notes before a minor
version jump, and take a Longhorn backup/snapshot of `netbox-postgres` first.
The chart's bundled PostgreSQL/Valkey subcharts stay disabled; keep
`postgres.yaml` and `valkey.yaml` on supported upstream versions.
