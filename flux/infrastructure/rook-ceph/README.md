# Rook Ceph

Rook Ceph provides the cluster's block (RBD), shared filesystem (CephFS) and
S3-compatible object storage (RGW). It is the only storage system in the
cluster.

## Current state

- Ceph is healthy. The only `HEALTH_WARN` items are the expected Tentacle
  warnings about `aes` CephX keys for the CSI clients; Rook uses `aes` because
  the Talos kernel is older than 7.0.
- The Ceph dashboard is at <https://ceph.glem.men>. The admin password is in
  `rook-ceph/rook-ceph-dashboard-password`.
- The CSI plugin image is pinned to cephcsi v3.17.1 by digest. v3.18.x is a
  Rocky Linux 10 build and crashes on the x86-64-v2 CPUs in glemmen50 and
  glemmen60 (rook/rook#18607).
- All application and monitoring volumes are on Ceph: volunteer keys, media
  application config volumes, the qBittorrent downloads and the Hærry Pot
  movie, Minecraft Radar, Frigate config/recordings, WebODM database/media,
  Energisjappa data/PostgreSQL, NetBox PostgreSQL/media, and all monitoring
  volumes. Radarr/Sonarr re-import the media library from the downloads.
- NetBox media was copied to CephFS and NetBox now uses `netbox-media-ceph` and
  `netbox-postgres-ceph`; the old claims have been removed.
- Prometheus and Alertmanager were copied with a single PV-rebind pass, so their
  TSDB and notification state were preserved. Thanos compactor/storegateway
  caches and Loki's local WAL/cache were recreated empty on `fast-rbd`; their
  durable data lives in the `thanos` and `loki` buckets in the fast RGW.
- YugabyteDB runs on `nvme-rbd`.
- Nine OSDs are up and in: two CMR on glemmen130; one CMR each on
  glemmen150/160/170/180; and one raw NVMe partition each on
  glemmen250/180/160.
- The cluster is rebalancing after the last OSD additions. `ceph -s` shows
  `active+remapped`/`backfilling` PGs until recovery finishes.

## Storage classes

| StorageClass | Type | Backing | Protection |
|---|---|---|---|
| `fast-rbd` | RBD | CMR disks on glemmen130/150/160/170/180 | RF2, `failureDomain: host`, `min_size: 1` |
| `fast-fs` | CephFS | same CMR disks, data pool `cephfs-fast` | RF2, `failureDomain: host` |
| `nvme-rbd` | RBD | raw NVMe partitions on glemmen250/180/160 | RF2, `failureDomain: host`, `min_size: 1` |
| `nvme-fs` | CephFS | same NVMe OSDs, data pool `cephfs-nvme` | RF2, `failureDomain: host` |
| `ceph-bucket` | RGW/OBC | fast RGW buckets | RF2 on the CMR OSDs |

- CephFS metadata pool `cephfs-metadata` is on the same CMR OSDs.
- Rook schedules three mons and two mgrs; OSDs are explicitly selected, never
  `useAllDevices`.
- The toolbox is enabled:
  `kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph status`.

## Adding NVMe OSDs to the remaining 1xx nodes

glemmen250, glemmen180 and glemmen160 keep a 100 GiB `EPHEMERAL` partition and
expose the remaining ~140 GiB as the `r-nvme-osd` raw partition. glemmen130,
glemmen150 and glemmen170 still use the whole NVMe disk for Talos.

Add them one node at a time with the same `VolumeConfig`/`RawVolumeConfig`
pattern and a reinstall. Do not reinstall a node while Ceph is still
backfilling.

## `bulk-rbd` and `bulk-fs`

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

## Ceph RGW

The fast RGW (`rook-ceph-rgw-fast`) runs on the CMR tier. Its metadata and data
pools are replicated RF2 on `deviceClass: cmr`, with `preservePoolsOnDelete`
set. Thanos and Loki use the `thanos` and `loki` buckets through this endpoint.

The RGW is intended to move to the bulk EC 8+2 pools on glemmen80 later. The
bucket names and clients do not need to change for that move.

## Unused data disks

These disks are unmounted and not part of Ceph:

| Node | Disks |
|---|---|
| glemmen60 | sdb 8 TB |
| glemmen80 | sdb, sdc 1 TB |
| glemmen130 | sdc 1 TB SMR |
| glemmen150 | sdb 2 TB SMR |
| glemmen160 | sdb 2 TB SMR |
| glemmen170 | sdb 2 TB SMR |

They still contain old data and have not been wiped. Wipe a disk before adding
it to the CephCluster.

## Useful checks

```sh
kubectl -n rook-ceph get cephcluster
kubectl -n rook-ceph get pods
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph status
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph osd tree
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph df detail
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph fs status
kubectl -n rook-ceph get cephfilesystem cephfs
kubectl -n rook-ceph get cephobjectstore fast cephobjectstoreuser
kubectl get storageclass fast-rbd fast-fs nvme-rbd nvme-fs ceph-bucket
```

## Next steps

1. Wait for the rebalance to reach `active+clean`.
2. Reinstall glemmen130, glemmen150 and glemmen170 one at a time and add their
   NVMe OSDs to the `nvme` class.
3. Add the glemmen80 24 × 12 TB disks, the `bulk` EC pools and `bulk-rbd` /
   `bulk-fs` classes; move the media library and RGW to the bulk tier.
4. Decide whether the unmounted CMR/SMR disks are reused for Ceph or retired,
   and wipe them before reuse.
