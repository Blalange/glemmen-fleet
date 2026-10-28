# YugabyteDB

Three-node [YugabyteDB](https://www.yugabyte.com/) (YSQL) cluster used by
Grafana and by the SeaweedFS filer. NetBox continues to use its own PostgreSQL
StatefulSet; the shared YugabyteDB instance was retired because NetBox's schema
needs index and collation features that YSQL does not implement.

## Components

| Resource | Purpose |
|---|---|
| `release.yaml` | `yugabytedb/yugabyte` HelmRepository and HelmRelease (chart `2026.1.2`, image `2026.1.2.0-b137`) |
| `values.yaml` | Three YB-Masters, three YB-TServers, RF3, storage and probe settings |
| `init-job.yaml` | One-shot `yugabyte-init-v1` Job that creates the application roles/databases and sets the superuser password |
| `pdb.yaml` | `policy/v1` PodDisruptionBudgets for masters and tservers |
| `storageclass-fast.yaml` | `yugabyte-fast-master` (two Longhorn replicas) and `yugabyte-fast-tserver` (one Longhorn replica) |
| `storageclass.yaml` | Legacy `yugabyte` class used by the retired hand-written cluster's PVCs |

The chart runs YB-Master and YB-TServer as separate StatefulSets, with three
pods each. `oldNamingStyle` is on, so the stable services are `yb-masters` and
`yb-tservers`; the latter is the YSQL endpoint on port 5433. Grafana and
SeaweedFS connect to `yb-tservers.yugabyte.svc.cluster.local:5433`.

## Topology and replication

- Three masters in a quorum and three tservers, one pod per node with
  required pod anti-affinity.
- YSQL replication factor 3. `yugabyted configure` is not used; the chart
  starts the supported master/tserver topology directly.
- The YB pods prefer the 1xx control-plane nodes (and `glemmen80` as a
  fallback), matching the rest of the performance-sensitive workloads.
- Pod readiness requires the tserver UI HTTP endpoint; liveness is disabled.
  The chart's default liveness probe syncs the data directory, which can time
  out on Longhorn and restart a healthy database.

## Storage

The YB volumes use Longhorn disks tagged `fast` (the CMR system disks and
`glemmen130`'s CMR data disk). The large SMR/HDD data disks are deliberately
excluded.

- **Master volumes:** `yugabyte-fast-master`, two Longhorn replicas. The
  volumes are small and low-write; keeping a second replica makes replacing a
  master easier.
- **Tserver volumes:** `yugabyte-fast-tserver`, one Longhorn replica. YB RF3 is
  the data durability layer; a second Longhorn replica would only add
  synchronous write latency. The trade-off is that if the node holding a
  tserver's only Longhorn replica is lost, the pod's PVC cannot attach
  elsewhere. The cluster stays available on the other two tservers; delete the
  affected `datadir0-yb-tserver-*` PVC/PV and let the StatefulSet create a new
  volume, then let YB remote-bootstrap the tserver from its peers. Do this only
  while the other two tservers are healthy.

`yugabyte-fast` is the class used by the PVCs from the first chart install;
those PVCs are already bound and cannot change class in place. New volumes use
the master/tserver classes above.

## Secrets

Nothing secret is stored in Git. Create the three keys before the init Job
runs:

```sh
SUPERUSER_PW="$(openssl rand -hex 24)"
GRAFANA_PW="$(openssl rand -hex 24)"
SEAWEEDFS_PW="$(openssl rand -hex 24)"

kubectl -n yugabyte create secret generic yugabyte-auth \
  --from-literal=superuser-password="$SUPERUSER_PW" \
  --from-literal=grafana-password="$GRAFANA_PW" \
  --from-literal=seaweedfs-password="$SEAWEEDFS_PW" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n monitoring create secret generic grafana-db \
  --from-literal=password="$GRAFANA_PW" \
  --dry-run=client -o yaml | kubectl apply -f -
```

`grafana-password` must match the `grafana-db` Secret in `monitoring`.
`seaweedfs-password` must match the password in the `seaweedfs-filer-config`
Secret in `seaweedfs`; see `flux/README.md`.

The `yugabyte-init-v1` Job sets the default `yugabyte` superuser password from
`yugabyte-auth` and creates the `grafana` and `seaweedfs` roles/databases. It is
idempotent. If the SQL changes, delete the completed Job and bump its name (or
delete and re-apply it) so it runs again.

## Administration

Use the YSQL service and the superuser password from the Secret:

```sh
SUPERUSER_PW="$(kubectl -n yugabyte get secret yugabyte-auth \
  -o jsonpath='{.data.superuser-password}' | base64 -d)"

kubectl -n yugabyte exec -it yb-tserver-0 -- \
  env PGPASSWORD="$SUPERUSER_PW" ysqlsh \
  -h yb-tservers.yugabyte.svc.cluster.local -U yugabyte
```

Useful checks:

```sh
kubectl -n yugabyte exec yb-tserver-0 -- \
  /home/yugabyte/bin/yb-admin \
  --master_addresses=yb-master-0.yb-masters.yugabyte.svc.cluster.local:7100,yb-master-1.yb-masters.yugabyte.svc.cluster.local:7100,yb-master-2.yb-masters.yugabyte.svc.cluster.local:7100 \
  list_all_masters

kubectl -n yugabyte exec yb-tserver-0 -- \
  /home/yugabyte/bin/yb-admin \
  --master_addresses=yb-master-0.yb-masters.yugabyte.svc.cluster.local:7100,yb-master-1.yb-masters.yugabyte.svc.cluster.local:7100,yb-master-2.yb-masters.yugabyte.svc.cluster.local:7100 \
  list_all_tablet_servers
```

## Grafana

Grafana runs two replicas against this cluster. `/var/lib/grafana` is an
emptyDir; dashboards, data sources and the admin user live in YSQL. Grafana's
startup probe allows an hour for a cold start/migration, and liveness only
checks the HTTP listener so a database outage does not cause a restart loop.

On a fresh or restored database, start with **one** Grafana replica until the
initial dashboard provisioning has finished, then scale to two. Both replicas'
sidecars otherwise try to write the unified-storage `resource_version` row at
the same time; YSQL's read-committed implementation can return `40001`
serialization errors (`Restart read required`) and one sidecar's dashboards are
skipped. The provisioning lock normally serializes them, but the initial sync
is safest with a single replica.

Grafana 13 also runs a unified-storage data migration that locks the legacy
`dashboard` table with `LOCK TABLE ... IN SHARE MODE`. YSQL does not support
that lock mode. On a fresh database, mark the migration complete after the core
schema exists:

```sql
INSERT INTO unifiedstorage_migration_log
  (migration_id, sql, success, error, timestamp)
VALUES
  ('folders and dashboards migration',
   'manually marked complete: YSQL does not support LOCK TABLE IN SHARE MODE',
   true, '', now());
```

Run it as `yugabyte` in the `grafana` database, then restart the Grafana pods.
Without this row Grafana exits with `failed to lock table "dashboard": pq: SHARE
not supported yet`.

## Sizing

Each tserver has a 4GiB memory limit (85% is the YB hard limit), and the chart
uses the tserver's JVM/PG shared-memory defaults. Grafana's and SeaweedFS's
schemas are small, so RF3 fits comfortably. The tserver metrics snapshotter is
set to every five minutes to reduce internal write load on the Longhorn-backed
disks.

## Retired hand-written cluster

The previous `yugabyted` StatefulSet, its Service and ConfigMap are gone. Its
PVCs are named `data-yugabyte-0/1/2` and remain bound to the legacy `yugabyte`
StorageClass for rollback. After the chart-managed cluster has been verified,
they can be deleted, together with their retained PVs and Longhorn volumes.
