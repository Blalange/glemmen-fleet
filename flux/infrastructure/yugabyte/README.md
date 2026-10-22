# YugabyteDB

Three-node [YugabyteDB](https://www.yugabyte.com/) (YSQL) cluster used by
Grafana. NetBox continues to use its own PostgreSQL StatefulSet; the shared
YugabyteDB instance was retired because NetBox's schema needs index and
collation features that YSQL does not implement.

## Components

| Resource | Purpose |
|---|---|
| `statefulset.yaml` | Three `yugabytedb/yugabyte:2026.1.0.1-b1` pods, 20Gi each |
| `service.yaml` | Headless `yugabyte` Service (YSQL `5433`, RPC/UI ports) |
| `configmap.yaml` | Seed/join/startup scripts and the readiness helper |
| `storageclass.yaml` | `yugabyte` Longhorn class (Retain, two Longhorn replicas) |

Grafana connects to `yugabyte.yugabyte.svc.cluster.local:5433` with the
`grafana` role and SCRAM password authentication. The database is
cluster-internal and unencrypted; nothing is exposed through Traefik.

## Cluster topology and replication

- Three pods, one per Kubernetes node (required pod anti-affinity).
- Three masters and three tservers.
- `yugabyted configure data_placement --fault_tolerance=zone --rf=3` after all
  nodes join; each pod advertises a different logical zone (`z0`, `z1`, `z2`).
  This makes the cluster survive one node or zone failure.
- Each pod has its own Longhorn volume with two Longhorn replicas, so a pod can
  be rescheduled to another node if its original node fails.

Pod 0 is the seed. Pods 1 and 2 wait for the seed's master port and start with
`--join=<seed IP>`. Once three masters and three tservers are alive, pod 0
configures the RF3 zone placement and creates the `grafana` role/database. The
readiness probe for pod 0 only passes after that, so the StatefulSet is not
considered ready until RF3 is in place.

## Secrets

Nothing secret is stored in Git. Create the superuser and Grafana role
passwords before the StatefulSet starts:

```sh
kubectl create namespace yugabyte --dry-run=client -o yaml | kubectl apply -f -

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

The `grafana-db` Secret in `monitoring` must contain the same password as
`grafana-password` in `yugabyte-auth`. Grafana reads it through
`GF_DATABASE_PASSWORD`; the startup script sets the role password from
`yugabyte-auth` on every pod-0 start.

## Administration

The local unix socket is trusted for the `yugabyte` user:

```sh
kubectl -n yugabyte exec -it yugabyte-0 -- /opt/yugabyte/ysql-local.sh -U yugabyte
```

Useful checks:

```sh
kubectl -n yugabyte exec yugabyte-0 -- \
  /home/yugabyte/bin/yb-admin \
  --master_addresses=yugabyte-0.yugabyte.yugabyte.svc.cluster.local:7100 \
  list_all_masters

kubectl -n yugabyte exec yugabyte-0 -- \
  /home/yugabyte/bin/yb-admin \
  --master_addresses=yugabyte-0.yugabyte.yugabyte.svc.cluster.local:7100 \
  list_all_tablet_servers
```

## Grafana

Grafana runs two replicas against this cluster. `/var/lib/grafana` is an
emptyDir, so both replicas can start without a ReadWriteOnce volume; dashboards,
data sources and the admin user live in YSQL. The first startup after switching
to YSQL migrates Grafana's schema, which takes roughly 20-30 minutes on the
three-node Longhorn-backed cluster. Keep the liveness probe relaxed (or
temporarily disabled) during that one-time migration.

Grafana 13 also runs a unified-storage data migration that locks the legacy
`dashboard` table with `LOCK TABLE ... IN SHARE MODE`. YSQL does not support
that lock mode. On a fresh database there are no legacy dashboards to migrate
(the sidecars provision directly into unified storage), so mark the migration
complete after the core schema exists:

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

Each tserver has a 4GiB memory limit, and 10% of that is the tablet-overhead
budget used by YugabyteDB's tablet-replica limit. Grafana's schema is small, so
three RF3 replicas fit comfortably. The startup script also raises
`transaction_max_missed_heartbeat_periods` to 240 so a busy node does not expire
long DDL transactions.
