# Rook Ceph

Rook Ceph replaces Longhorn and SeaweedFS. The migration is being rolled out in
phases because the hardware and the Longhorn/SeaweedFS data are not all
available at once.

## Live migration state

- Flux Kustomizations are **suspended** (`./scripts/flux/manage.py freeze`)
  while PVCs are copied and workloads are switched. Resume Flux only after the
  manifest changes in this branch have been committed/pushed. The Rook
  HelmReleases are still reconciled by Helm because freezing a Kustomization
  does not stop helm-controller.
- Ceph itself is healthy. The only `HEALTH_WARN` items are the expected
  Tentacle warnings about `aes` CephX keys for the CSI clients; Rook uses
  `aes` because the Talos kernel is older than 7.0.
- The Ceph dashboard is at <https://ceph.glem.men>. The admin password is in
  `rook-ceph/rook-ceph-dashboard-password`.
- The CSI plugin image is pinned to cephcsi v3.17.1 by digest. v3.18.x is a
  Rocky Linux 10 build and crashes on the x86-64-v2 CPUs in glemmen50 and
  glemmen60 (rook/rook#18607).
- Migrated to Ceph: volunteer keys, all media application config volumes, the
  qBittorrent downloads and the Hærry Pot movie, Minecraft Radar, Frigate
  config/recordings, WebODM database/media, Energisjappa data/PostgreSQL,
  NetBox PostgreSQL/media, all monitoring volumes, and the Thanos/Loki S3 data
  (to the fast RGW). Radarr/Sonarr will re-import the media library from the
  downloads.
- NetBox media was copied to CephFS, a final delta pass was run, and NetBox was
  switched to `netbox-media-ceph`. The old `netbox-media` PVC is retained for
  rollback.
- Prometheus and Alertmanager were copied with a single PV-rebind pass, so their
  TSDB and notification state were preserved. Thanos compactor/storegateway
  caches and Loki's local WAL/cache were recreated empty on `fast-rbd`; their
  durable data was copied from SeaweedFS to the `thanos` and `loki` buckets in
  the fast RGW.
- The unused `*-old` Prometheus/Alertmanager Longhorn PVCs and PVs were removed
  after the new `fast-rbd` claims were verified.
- YugabyteDB remains on Longhorn. SeaweedFS and its Longhorn volumes were
  removed after the media and object-store migrations were verified.
- glemmen60 `hdd1` and the CMR disks on glemmen160/170/180 still carry Longhorn
  replicas and are not Ceph OSDs yet. The SeaweedFS hostPath on glemmen60 is
  gone.

## Current status (Phase 1)

Available now:

| StorageClass | Type | Backing | Protection |
|---|---|---|---|
| `fast-rbd` | RBD | CMR disks on glemmen130 and glemmen150 | RF2, `failureDomain: host`, `min_size: 1` |
| `fast-fs` | CephFS | same CMR disks, data pool `cephfs-fast` | RF2, `failureDomain: host` |
| `ceph-bucket` | RGW/OBC | fast RGW buckets | RF2 on the same CMR OSDs |

- CephFS metadata pool `cephfs-metadata` is on the same CMR OSDs.
- Four OSDs: two on glemmen130, one on glemmen150 and one on glemmen170.
  The glemmen170 CMR disk was evicted from Longhorn to give Ceph a third
  failure-domain host.
- Rook schedules three mons and two mgrs; OSDs are explicitly selected, never
  `useAllDevices`.
- The dashboard is exposed at <https://ceph.glem.men>. The admin password is in
  the `rook-ceph-dashboard-password` Secret.
- The toolbox is enabled:
  `kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph status`.

## Deferred

The following are intentionally **not** configured yet:

### `nvme-rbd` and `nvme-fs`

The 1xx nodes have only their Talos system NVMe disk. Giving Ceph a raw NVMe
OSD requires capping `EPHEMERAL` at ~100 GiB and adding a Talos
`RawVolumeConfig`, which only takes effect on a reinstall. That phase also
requires every Longhorn volume to be off the 1xx system disks first. Until then
there is no `nvme-rbd` or `nvme-fs` class and no NVMe OSD.

### `bulk-rbd` and `bulk-fs`

glemmen80 has not received the 24 × 12 TB disks or the external SAS HBA yet.
When they are installed:

- Add the 24 disks as `deviceClass: bulk`.
- Add the `bulk` data pool to the existing `cephfs` filesystem and expose
  `bulk-fs` with `pool: cephfs-bulk`.
- Add the `bulk-rbd` erasure-coded RBD pool (8+2, `failureDomain: osd`) and
  expose `bulk-rbd` with `pool: bulk-rbd-metadata`, `dataPool: bulk-rbd`.
- Set `min_size: "9"` and `allow_ec_overwrites: "true"` on the EC data pools.

Until then the media library stays on `fast-fs` and Frigate recordings stay on
`fast-rbd`.

### Ceph RGW

The fast RGW (`rook-ceph-rgw-fast`) is live on the CMR tier. Its metadata and
data pools are replicated RF2 on `deviceClass: cmr`, with `preservePoolsOnDelete`
set. Thanos and Loki use the `thanos` and `loki` buckets through this endpoint;
their durable data was copied from the SeaweedFS S3 gateway before it was
removed.

The RGW is intended to move to the bulk EC 8+2 pools on glemmen80 later. The
bucket names and clients do not need to change for that move.

## Longhorn disks still being drained

Longhorn eviction has completed on the CMR/SMR disks on glemmen130,
glemmen150 and the glemmen170 CMR disk; those disks were wiped and handed to
Ceph.

glemmen60 `hdd1` still carries Longhorn replicas and the 6 TB Longhorn
reservation that was originally made for the SeaweedFS hostPath. The SeaweedFS
data has been removed; the disk can be repurposed after Longhorn is drained.
The Longhorn disks on glemmen160, the glemmen170 SMR disk and glemmen180 still
carry Longhorn replicas.

## Useful checks

```sh
kubectl -n rook-ceph get cephcluster
kubectl -n rook-ceph get pods
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph status
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph osd tree
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph df detail
kubectl -n rook-ceph get cephfilesystem cephfs
kubectl -n rook-ceph get cephobjectstore fast cephobjectstoreuser
kubectl get storageclass fast-rbd fast-fs ceph-bucket
```

## Next migration steps

1. Keep the last Longhorn volumes (YugabyteDB and any remaining application
   PVCs) available until their Ceph replacements are verified.
2. Free the remaining CMR disks as Longhorn replicas move off them; add them to
   the CephCluster as `deviceClass: cmr`.
3. Add the glemmen80 24 × 12 TB disks, the `bulk` EC pools and `bulk-rbd` /
   `bulk-fs` classes; move the media library and RGW to the bulk tier.
4. Reinstall the 1xx nodes and add `nvme-rbd`/`nvme-fs`.
5. Remove Longhorn completely.
