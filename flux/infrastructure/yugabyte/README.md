# YugabyteDB

Three-node [YugabyteDB](https://www.yugabyte.com/) (YSQL) cluster used by
Grafana. The media apps use SQLite; the Servarr apps
cannot parse YB's `server_version` string (`15.12-YB-...`). NetBox continues
to use its own PostgreSQL StatefulSet; the shared YugabyteDB instance was
retired because NetBox's schema needs index and collation features that YSQL
does not implement. All media applications stay on SQLite for the same class of
compatibility reasons.

## Components

| Resource | Purpose |
|---|---|
| `release.yaml` | `yugabytedb/yugabyte` HelmRepository and HelmRelease (chart `2026.1.3`, image `2026.1.3.0-b100`) |
| `values.yaml` | Three YB-Masters, three YB-TServers, RF3, storage and probe settings |
| `init-job.yaml` | One-shot `yugabyte-init-v7` Job that creates the Grafana role/database and sets the superuser password |
| `pdb.yaml` | `policy/v1` PodDisruptionBudgets for masters and tservers |

The chart runs YB-Master and YB-TServer as separate StatefulSets, with three
pods each. `oldNamingStyle` is on, so the stable services are `yb-masters` and
`yb-tservers`; the latter is the YSQL endpoint on port 5433. Grafana connects
to `yb-tservers.yugabyte.svc.cluster.local:5433`.

## Topology and replication

- Three masters in a quorum and three tservers. Required pod anti-affinity
  keeps masters off nodes that already run another master, and tservers off
  nodes that already run another tserver. A master and tserver may share a
  node: only six nodes are eligible for YB pods, so a node loss must not leave
  a replica permanently unschedulable. A preferred anti-affinity still keeps
  the two components apart when a free node exists.
- YSQL replication factor 3. `yugabyted configure` is not used; the chart
  starts the supported master/tserver topology directly.
- The YB pods prefer the 1xx control-plane nodes (and `glemmen80` as a
  fallback), matching the rest of the performance-sensitive workloads.
- Pod readiness requires the tserver UI HTTP endpoint; liveness is disabled.
  The chart's default liveness probe syncs the data directory, which can time
  out on the backing volume and restart a healthy database.

## Storage

The YB volumes use the Ceph NVMe block class (`nvme-rbd`) on the raw NVMe OSDs
in the control-plane nodes and glemmen250. YB still provides RF3 across the
three masters and three tservers; Ceph adds RF2 at the block layer.

- **Master volumes:** 10 GiB on `nvme-rbd`.
- **Tserver volumes:** 30 GiB on `nvme-rbd`.

The original `yugabyte-fast` volumes were copied with `pv-migrate`, the
`datadir0-*` PVC names were rebound to the migrated Ceph RBD PVs, and the old
volumes were deleted after verification.

## Secrets

Nothing secret is stored in Git. Create the two keys before the init Job runs:

```sh
SUPERUSER_PW="$(openssl rand -hex 24)"
GRAFANA_PW="$(openssl rand -hex 24)"

kubectl -n yugabyte create secret generic yugabyte-auth \
  --from-literal=superuser-password="$SUPERUSER_PW" \
  --from-literal=grafana-password="$GRAFANA_PW" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n monitoring create secret generic grafana-db \
  --from-literal=password="$GRAFANA_PW" \
  --dry-run=client -o yaml | kubectl apply -f -
```

`grafana-password` must match the `grafana-db` Secret in `monitoring`. The
`yugabyte-init-v7` Job sets the default `yugabyte` superuser password from
`yugabyte-auth` and creates:

- `grafana` → `grafana`

It is idempotent. If the SQL changes, bump the Job name (or delete and re-apply
it) so it runs again; Jobs are immutable.

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

## Monitoring

The `yugabyte` and `yugabyte-tserver-apis` ServiceMonitors in
`flux/monitoring/resources/servicemonitors/yugabyte.yaml` scrape
`/prometheus-metrics` from YB-Master (:7000), YB-TServer (:9000), YCQL
(:12000) and YSQL (:13000) every 60 seconds. YEDIS is disabled.

The raw tserver endpoint includes per-tablet RocksDB and DocDB metrics. A
255-tablet tserver exposes about 90k such series per pod, so the
`metricRelabelings` drop every series with a `table_id` label. The upstream
YugabyteDB Grafana dashboard was trimmed to the panels that use the remaining
server-level metrics; see `flux/monitoring/resources/dashboards/yugabyte/`.
The scrape relabeling also rewrites the upstream
`handler_latency_yb_<server>_<service>_<method>_{sum,count}` names to
`rpc_latency_{sum,count}` with `server_type`, `service_type` and
`service_method` labels, which is the format the dashboard expects.

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
uses the tserver's JVM/PG shared-memory defaults. Grafana's schema is small, so
RF3 fits comfortably. The tserver metrics snapshotter is
set to every five minutes to reduce internal write load on the Ceph NVMe
disks.

## Retired hand-written cluster

The previous `yugabyted` StatefulSet, its Service, ConfigMap, PVCs and
volumes are gone. The chart-managed cluster is now the only
YugabyteDB deployment.
