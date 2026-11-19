# Runbook: PG autoscaler blocked by overlapping CRUSH roots

**Applies to:** Rook Ceph with per-device-class pools (`deviceClass: cmr` / `nvme`)
**First seen:** 2026-11-19, Ceph 20.2.4 (Tentacle), Rook 1.21.2
**Symptoms:** `ceph osd pool autoscale-status` returns nothing; the balancer stops optimizing; PGs stay pinned to a subset of OSDs.

## Symptom

`ceph osd pool autoscale-status` prints an empty result, and the mgr log repeats:

```text
[pg_autoscaler WARNING pg_autoscaler] pool 16 contains an overlapping root -18... skipping scaling
[pg_autoscaler WARNING pg_autoscaler] pool nvme-rbd won't scale due to overlapping roots: {-18, -2, -1}
```

The balancer also reports `Too many objects (0.169505 > 0.050000) are misplaced; try again later`, and `ceph osd pool ls detail` shows a high `read_balance_score` (reads concentrated on a few OSDs).

## Root cause

Rook creates a shadow CRUSH root per device class: `default~cmr` and `default~nvme`. Every pool must map to exactly one root. Two groups of pools were still on the legacy `replicated_rule` (rule 0, root `default`, which contains **both** shadow roots):

- `.mgr` — Ceph's internal manager pool.
- `default.rgw.log`, `default.rgw.meta`, `default.rgw.control` — leftovers from before the `fast` object store existed.

Because those pools span both device classes, the autoscaler treats the roots as overlapping and refuses to scale **any** pool, not just the offending ones. This is why the command returns an empty table.

## Fix

Point the four pools at the CMR rule. In Tentacle the `crush_rule` argument is the rule **name**, not the numeric ID. Using the ID fails with:

```text
Error ENOENT: crush rule 1 does not exist
```

```bash
ceph osd pool set .mgr crush_rule fast-rbd
ceph osd pool set default.rgw.log crush_rule fast-rbd
ceph osd pool set default.rgw.meta crush_rule fast-rbd
ceph osd pool set default.rgw.control crush_rule fast-rbd
```

`fast-rbd` is the rule that takes `default~cmr`. If the rule names differ, list them first:

```bash
ceph osd crush rule dump | jq -r '.[] | "\(.rule_id)\t\(.rule_name)"'
```

All four pools are tiny (`.mgr` is one PG; the `default.rgw.*` pools are unused in this cluster), so the remap is effectively free. Moving them is non-destructive; deleting the `default.rgw.*` pools is also possible if no `default` object store exists, but the rule change is the smaller action.

## Verify

Wait one autoscaler tick (`mgr/pg_autoscaler/sleep_interval`, 60 s by default), then:

```bash
ceph osd pool autoscale-status
ceph -s
```

The table should now list every pool with a `PG_NUM` and `AUTOSCALE` column, and the `overlapping root` warnings should stop.

### If `autoscale-status` is still empty

Check for any remaining pool on the legacy rule:

```bash
for p in $(ceph osd pool ls); do
  printf '%-30s %s\n' "$p" "$(ceph osd pool get "$p" crush_rule -f json 2>/dev/null | jq -r .crush_rule)"
done
```

Any pool reporting `replicated_rule` (rule 0) re-triggers the condition. Move it to a class-specific rule.

## Related: `pgp_num` does not move PGs in Tentacle

After the autoscaler was fixed, `nvme-rbd` still had all 32 PGs on two OSDs. The pool displayed `pg_num 32`, `pgp_num 1`, `pgp_num_target 32`, but `ceph osd dump -f json` exposed:

```text
pg_placement_num = 1
pg_placement_num_target = 32
```

In Tentacle, `ceph osd pool set <pool> pgp_num N` only sets the **target**. The mgr does not advance `pg_placement_num` while `pg_num_target != pgp_num_target`, so setting the target alone leaves placement pinned. Apply the actual value with `pgp_num_actual`:

```bash
ceph osd pool set nvme-rbd pgp_num 32
ceph osd pool set nvme-rbd pgp_num_actual 32
```

`pgp_num_actual` is accepted by `osd pool set` (see `src/mon/OSDMonitor.cc`) even though it is not listed in `ceph osd pool get`. It is validated against `pg_num` and `pg_num_pending`, so only use it after `pg_num` is already at the target value.

Verify the placement spread:

```bash
ceph pg dump pgs_brief | awk '$1 ~ /^16\./ {print $3}'
```

The `UP` sets should now include every NVMe OSD, not just one host pair.

## Notes

- `ceph osd pool get <pool> pg_num_min` before Rook applies it can return `ENOENT`; that is normal for pools that have not had a floor set.
- The autoscaler may raise `pg_num` as soon as it is unblocked (for example `cephfs-fast` jumped from 32 to 128). Expect PG splits and backfill; schedule the fix for a quiet window.
- Do not switch the balancer to `read`/`upmap-read` to work around this. `pg-upmap-primary` is not supported by the Linux kernel client, which is what the CSI RBD and CephFS mounts use. Keep the balancer in `upmap` mode and fix the roots instead.
