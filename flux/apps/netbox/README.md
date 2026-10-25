# NetBox deployment

This directory contains the NetBox application manifests: the Helm release,
PostgreSQL, Valkey, media storage and the `netbox-sync` CronJob.

The inventory, collector, sync code and device-type images live in
[`inventory/`](../../inventory/README.md). The inventory ConfigMaps
(`netbox-inventory-data`, `netbox-device-type-images`, `netbox-sync-code`) are generated
by a separate Flux Kustomization at `./inventory`; the NetBox Kustomization
depends on it.

## Secrets

The application-specific secrets remain in Kubernetes:

- `netbox-secrets`: Django secret key, database password, API token and
  bootstrap admin credentials.
- `netbox-values`: Helm values ConfigMap generated from `values.yaml`.

Read the admin password with:

```sh
kubectl -n netbox get secret netbox-secrets \
  -o jsonpath='{.data.password}' | base64 -d
```

## Sync job

`sync-cronjob.yaml` runs the entrypoint from the `netbox-sync-code` ConfigMap
every 10 minutes. `PRUNE=true` removes only objects tagged `gitops`; objects
created manually in NetBox are never touched.

See [`inventory/README.md`](../../inventory/README.md) for the inventory schema,
collector usage and source mapping.
