# SeaweedFS

The cluster runs SeaweedFS 4.53 as the storage backend for the media library,
the Thanos bucket and the Loki bucket. The four operator-managed volume servers
use erasure coding (EC) 10+4, so every EC volume is split into 14 shards and
needs at least 10 shards to be readable.

This directory contains the operator manifests, the volume servers, the CSI
driver and the storage classes. The media library is mounted through the
`seaweedfs-media` StorageClass as a 2500Gi `ReadWriteMany` volume.

The operator also manages the S3 buckets used by the observability stack:
`thanos` for long-term metrics and `loki` for logs. Their key pairs are
generated into the `thanos-s3-credentials` and `loki-s3-credentials` Secrets;
copy them into the `monitoring` namespace as documented in
[`../../README.md`](../../README.md#seaweedfs-object-storage) and
[`../../README.md`](../../README.md#loki-and-log-collection).

## EC `.vif` stub metadata bug (November 2026)

### Symptoms

Reads through the CSI mount failed with HTTP 500 for a subset of media files.
The CSI mount logs showed lines like:

```
read http://seaweedfs-volume-1...:8444/869,0918106631f9b7 failed, err: ... 500 Internal Server Error
```

The volume servers returned one of:

```
ec shard 869.0 belongs to a different encode run
ReadEcShardIntervals: cannot recover shard 869.0: only 9 shards available [3 5 6 7 8 9 10 11 12], need at least 10
ec volume 598 needle ... Size:-1331941428, DataSize:0, Name:, Mime: Compressed:false ...: size mismatch
```

`ec.check.replication` reported the volumes as healthy because every volume had
14 physical shards. The problem was metadata, not missing shard files.

### Root cause

An EC volume has a `<volume>.vif` on every disk that holds one of its shards.
The `.vif` records the EC geometry (`10+4`), the encode-run identity
(`encodeTsNs`), the block size and the data size. Time-stamping the encode run
lets a volume server reject shards from a different generation.

Some disks still had a leftover **regular-volume `.vif` stub** from before the
volume was erasure-coded:

```json
{
  "files": [],
  "version": 3,
  "replication": "000",
  "bytesOffset": 4,
  "datFileSize": "0",
  "expireAtSec": "0",
  "readOnly": true,
  "ecShardConfig": null,
  "readOnlyCanDelete": false
}
```

When the disk later received EC shards, the shards and the `.ecx` index were
written but the stub `.vif` was not replaced. On load, the server interprets the
stub as `EncodeTsNs=0`. A coordinator that uses that disk then either:

- rejects shards from the real encode run (`belongs to a different encode run`),
  leaving fewer than 10 usable shards, or
- accepts a mix of shards from different runs and reconstructs garbage, which
  surfaces as the `size mismatch` error with an absurd needle size.

Volumes with 8–9 usable shards are the visible failures. Many more volumes had
a mix of stub and canonical `.vif` files and failed only intermittently,
depending on which coordinator served the read.

### Diagnosis

Run these checks from a volume server pod. `/data`, `/data0` ... are the disk
mounts; adapt the list for the server.

1. Find `.vif` files that have EC shards on the same disk but no EC config:

   ```sh
   for d in /data /data0 /data1 /data2 /data3; do
     [ -d "$d" ] || continue
     ls "$d"/*.ec[0-9][0-9] 2>/dev/null | sed 's/\.ec[0-9][0-9]$//' | sort -u |
       while read -r b; do
         [ -f "$b.vif" ] || continue
         grep -q '"ecShardConfig": {' "$b.vif" || echo "stub: $b.vif"
       done
   done
   ```

2. For each affected volume, compare the `.ecx` hash on every holder. All
   holders must have the same hash; this proves the shards are from the same
   encode run and that only the `.vif` is wrong:

   ```sh
   sha256sum /data*/<collection>_<volume>.ecx
   ```

3. Read the canonical `encodeTsNs` from a non-stub `.vif` and verify that all
   non-stub `.vif` files for the volume are byte-identical:

   ```sh
   grep -A4 '"ecShardConfig"' /data*/<collection>_<volume>.vif
   sha256sum /data*/<collection>_<volume>.vif
   ```

4. Count usable shards per encode run. A volume with fewer than 10 shards in
   the canonical run is critical; a volume with 10 or more still needs the
   stubs fixed so coordinators do not mix runs.

### Repair

Only repair a volume when:

- at least one non-stub `.vif` with a non-zero `encodeTsNs` exists,
- every non-stub `.vif` for the volume is byte-identical, and
- every holder's `.ecx` hash matches.

Then copy the canonical `.vif` over each stub **on the same volume**. Do not
copy a `.vif` between different volumes, and do not invent an `encodeTsNs` for
an all-stub volume.

```sh
f=/data0/<collection>_<volume>.vif
[ -e "$f.stub-backup" ] || cp "$f" "$f.stub-backup"
cat > "$f" <<'EOF'
{
  "files": [],
  "version": 3,
  "replication": "",
  "bytesOffset": 0,
  "datFileSize": "<canonical datFileSize>",
  "expireAtSec": "0",
  "readOnly": false,
  "ecShardConfig": {
    "dataShards": 10,
    "parityShards": 4,
    "encodeTsNs": "<canonical encodeTsNs>",
    "blockSize": "108003328"
  },
  "readOnlyCanDelete": false
}
EOF
```

The `.vif` must match the canonical file exactly, including `datFileSize` and
`blockSize`. Keeping the original as `.vif.stub-backup` makes the change
reversible.

The running volume server holds the old metadata in memory, so restart it after
the files are written. Restart in this order:

1. `seaweedfs-glemmen60-volume-0` first. While the other four servers still
   serve their shards, the repaired glemmen60 shards become usable without
   dropping any volume below 10 shards.
2. `seaweedfs-volume-0`, `-1`, `-2`, `-3` one at a time, waiting for each pod to
   become Ready before continuing.

Deleting the pod is enough; the StatefulSet recreates it with the same name and
the same PVCs.

### Verification

```sh
# Structural check (requires the weed shell lock):
printf 'lock\nec.check.replication\nunlock\n' | weed shell ...

# Read-only scrub of the repaired volumes:
printf 'lock\nec.scrub -volumeId=869,590,470 -mode=local -details\nunlock\n' | weed shell ...

# Read a known needle through every volume server:
wget -qS -O /dev/null 'http://<server>:8444/869,0918106631f9b7?readDeleted=true'
```

Also watch the CSI mount pod for new `failed, err` lines. A successful repair
shows no `size mismatch` or `different encode run` errors for the volume, and
the same needle returns `200 OK` from all five volume servers.

### All-stub volumes

Some EC volumes have **only** stub `.vif` files. They have no canonical
`encodeTsNs` to copy, and the servers currently read them leniently because the
caller and the holder both use `EncodeTsNs=0`. Leave them alone until there is a
canonical run to stamp; writing an arbitrary timestamp would make the shards
unreadable.

### Upstream

This is a SeaweedFS metadata bug: a regular-volume `.vif` stub can survive the
transition to an EC volume, and the EC shard write path does not replace it.
The error strings above are useful for an upstream report. Related upstream
work includes the EC source-volume cleanup/re-encode path
([seaweedfs/seaweedfs#9448](https://github.com/seaweedfs/seaweedfs/issues/9448),
[PR #9458](https://github.com/seaweedfs/seaweedfs/pull/9458)); re-check the
behavior on each SeaweedFS upgrade. The repair is data-plane only, so it cannot
be expressed in the Flux manifests in this directory.

## Orphaned volume cleanup

`orphan-cleanup.yaml` defines the suspended CronJob
`seaweedfs-orphan-cleanup` and its script. It is manual only: the CronJob has
`suspend: true`, so its schedule never fires. Trigger a run with:

```sh
name="seaweedfs-orphan-cleanup-$(date +%Y%m%d-%H%M%S)"
kubectl -n seaweedfs create job --from=cronjob/seaweedfs-orphan-cleanup "$name"
kubectl -n seaweedfs logs -f "job/$name"
```

A run does the following:

1. Runs `volume.fsck` read-only over every volume and calculates the orphaned
   bytes per volume. It keeps the `MAX_VOLUMES` (default 100) volumes with the
   most orphaned bytes.
2. Decodes each selected EC volume with `ec.decode` so the deletion phase can
   operate on a regular volume.
3. Runs `volume.fsck -reallyDeleteFromVolume` and `volume.vacuum` only on the
   selected volumes.
4. Runs `volume.deleteEmpty -apply -quietFor=24h` to remove empty volumes.
5. Re-encodes the decoded volumes with `ec.encode` only when they are still at
   least `EC_FULL_PERCENT` (default 95%) full after vacuum. Volumes below that
   threshold stay regular so a later run can clean them again without decoding
   the same volume twice.

The scan uses `-cutoffTimeAgo=5h` so chunks whose filer metadata has not been
committed yet are not mistaken for orphans. Deletion deliberately does not use
`-forcePurging`: SeaweedFS only purges orphans that all online replicas agree
on. The job takes the SeaweedFS admin lock once at startup, keeps it for the
whole run, and releases it after the last step, so other admin maintenance
waits for the entire cleanup. If `volume.list` reports duplicate volume IDs
in more than one collection, the run aborts before decoding or deleting because
volume-ID operations would be ambiguous.

Decoding selected EC volumes touches their shards. Review the EC `.vif` stub
section above before the first production run; an all-stub volume should not be
decoded until a canonical encode run exists. A failed decode is logged and that
volume is left untouched.

For a safe preview, set `DRY_RUN=true` on the CronJob and create the Job
immediately, before Flux restores the manifest value:

```sh
kubectl -n seaweedfs set env cronjob/seaweedfs-orphan-cleanup DRY_RUN=true
name="seaweedfs-orphan-cleanup-dryrun-$(date +%Y%m%d-%H%M%S)"
kubectl -n seaweedfs create job --from=cronjob/seaweedfs-orphan-cleanup "$name"
kubectl -n seaweedfs logs -f "job/$name"
```

The preview still performs the read-only scan but stops before decoding,
deleting or vacuuming anything. The running Job keeps the values it was created
with even after Flux reconciles the CronJob back to `DRY_RUN=false`.
`MAX_VOLUMES=1` can be set the same way for a first small run.

A run exits non-zero if a decode, purge, vacuum or re-encode fails. Volumes
whose purge is skipped are excluded from re-encoding and are retried by a
later run. The job uses the same `chrislusf/seaweedfs:4.53` image as the
cluster; keep the tag in sync when upgrading SeaweedFS.
