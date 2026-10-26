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
every 10 minutes. `PRUNE=true` removes only taggable objects tagged `gitops`;
objects created manually in NetBox are never touched. Owners and owner groups
are not taggable and are never pruned; contacts and contact assignments are
tagged and pruned normally.

The job reads public NetBox Device Type Library definitions and elevation
images at runtime (`DTL_ENABLED`) and caches them under `/media/dtl-cache` on
the `netbox-media` volume. DTL data is never committed to Git; manual inventory
metadata, images and component templates take precedence. DTL elevation images
are also attached as extra images when a manual front/rear image already
exists.

See [`inventory/README.md`](../../inventory/README.md) for the inventory schema,
collector usage and source mapping.
