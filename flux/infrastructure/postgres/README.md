# PostgreSQL

Single shared [PostgreSQL](https://www.postgresql.org/) instance for NetBox and
Grafana. It runs in the `postgres` namespace as one StatefulSet with one
Longhorn volume. A single node does not need a replicated database, and both
applications are fully supported on PostgreSQL.

## Components

| Resource | Purpose |
|---|---|
| `statefulset.yaml` | One `postgres:18.6-alpine` pod, 20Gi Longhorn volume |
| `service.yaml` | Headless `postgres` Service on port `5432` |
| `configmap.yaml` | First-boot init script that creates the roles/databases |
| `storageclass.yaml` | `postgres` Longhorn class (Retain, one replica) |

Clients connect to `postgres.postgres.svc.cluster.local:5432`. The database is
cluster-internal, unencrypted inside the cluster, and uses SCRAM passwords.

## Roles and databases

The official image creates the `postgres` superuser/database first. The init
script then creates:

| Role | Database | Used by |
|---|---|---|
| `netbox` | `netbox` | NetBox, with the `ltree` extension enabled |
| `grafana` | `grafana` | Grafana |

The init script only runs while the data directory is empty. Changing the
Secrets later does not change the passwords inside PostgreSQL; rotate with
`ALTER ROLE` as well.

## Secrets

Nothing secret is stored in Git. Create the passwords once, before the
StatefulSet starts:

```sh
kubectl create namespace postgres --dry-run=client -o yaml | kubectl apply -f -

ADMIN_PW="$(openssl rand -hex 24)"
NETBOX_PW="$(openssl rand -hex 24)"
GRAFANA_PW="$(openssl rand -hex 24)"

kubectl -n postgres create secret generic postgres-auth \
  --from-literal=admin-password="$ADMIN_PW" \
  --from-literal=netbox-password="$NETBOX_PW" \
  --from-literal=grafana-password="$GRAFANA_PW" \
  --dry-run=client -o yaml | kubectl apply -f -
```

The application Secrets must contain the same role passwords:

```sh
# Existing NetBox Secret: replace the value of db-password.
kubectl -n netbox patch secret netbox-secrets --type merge \
  -p "{\"data\":{\"db-password\":\"$(printf %s "$NETBOX_PW" | base64 -w0)\"}}"

kubectl -n monitoring create secret generic grafana-db \
  --from-literal=password="$GRAFANA_PW" \
  --dry-run=client -o yaml | kubectl apply -f -
```

`netbox-secrets` is created by the NetBox runbook
(`flux/apps/netbox/README.md`). The values in `postgres-auth` and the two app
Secrets must be rotated together.

## Administration

The local socket accepts the `postgres` superuser without a password, so no
Secret is needed inside the database pod:

```sh
kubectl -n postgres exec -it postgres-0 -- psql -U postgres
```

To connect from another pod over the network, use the superuser password from
`postgres-auth` (or an application role and its password) and host
`postgres.postgres.svc.cluster.local`, port `5432`.

## Backup

The Longhorn volume is included in the normal Longhorn backup target. For a
logical backup of the NetBox database:

```sh
kubectl -n postgres exec postgres-0 -- \
  pg_dump -U netbox -d netbox -Fc > netbox.dump
```
