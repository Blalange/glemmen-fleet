# Talos

Talos Linux v1.14.2 configuration for the glemmen cluster (Kubernetes 1.36.5).
All nodes are control planes and workers at the same time (etcd + workload scheduling are enabled).

## Files

| Path | Purpose |
|------|---------|
| `settings.env` | Versions, endpoint/VIP, node table — sourced by the scripts |
| `schematic.yaml` | Image Factory extensions: iscsi-tools, util-linux-tools, btrfs, netbird |
| `patches/common.yaml` | Common machine config patch (subnets, DNS, kubelet node IP, btrfs, Longhorn system disk) |
| `patches/netbird.yaml.tpl` | Netbird extension config template (`NB_SETUP_KEY` filled in by `gen-config.sh`) |
| `nodes/glemmen*.yaml` | Per-node: hostname, bond/addresses, Longhorn disks, VIP, Longhorn node annotation |
| `gen-iso.sh` | Creates the schematic (base image) and prints/downloads the generic ISO + installer image |
| `setup-images.sh` | Per-node setup ISOs with the maintenance network baked in (`--print` shows the kernel args) |
| `gen-config.sh` | Renders secrets + configs into `generated/` (gitignored) and validates them |
| `manage.sh` | Day-2 operations (apply, bootstrap, health, vip, wipe, upgrades) |
| `secrets/netbird.env` | Netbird setup key (gitignored, see `secrets/README.md`) |
| `generated/` | Derived PKI, kubeconfig, machine configs (gitignored — **back this up**) |

## First-time setup

```sh
# 1. Netbird setup key (dashboard -> Setup Keys, reusable)
cp talos/secrets/netbird.env.example talos/secrets/netbird.env  # or create it:
echo 'NB_SETUP_KEY=xxxx' > talos/secrets/netbird.env

# 2. Build the schematic and (optionally) download the generic ISO
./talos/gen-iso.sh --download

# 3. Build one setup ISO per machine (maintenance network baked in, no GRUB editing)
./talos/setup-images.sh

# 4. Generate and validate all machine configs (creates the cluster PKI once)
./talos/gen-config.sh
```

The scripts download pinned `talosctl`/`kubectl`/`flux` binaries into
`talos/bin/` / `scripts/bin/` when they are missing.

## Installing a node (steps 6 and 8)

1. Boot the Image Factory ISO (USB, virtual media, PXE) with UEFI.
2. Give the node network access in maintenance mode (see below).
3. Apply the node's config and let the installer run:

   ```sh
   ./talos/manage.sh apply glemmen130
   # the config contains UnattendedInstallConfig (wipe + reboot), so the node
   # installs Talos to /dev/sda and reboots into it
   ```

4. Verify the node came up:

   ```sh
   ./talos/manage.sh status
   ./talos/manage.sh disks glemmen130        # check sdb/sdc are detected
   ./talos/manage.sh services glemmen130
   ```

### Maintenance-mode network

The nodes have no DHCP on their bonded interfaces, so each machine normally needs
kernel arguments before it can be reached in maintenance mode. `setup-images.sh`
builds a per-node ISO with exactly those arguments baked into the boot entries,
so no GRUB editing is needed:

| File | Boot with |
|------|-----------|
| `talos/generated/iso/setup-glemmen130.iso` | glemmen130 |
| `talos/generated/iso/setup-glemmen150.iso` | glemmen150 |
| `talos/generated/iso/setup-glemmen160.iso` | glemmen160 |
| `talos/generated/iso/setup-glemmen170.iso` | glemmen170 |
| `talos/generated/iso/setup-glemmen180.iso` | glemmen180 |
| `talos/generated/iso/setup-glemmen60.iso`  | glemmen60 |
| `talos/generated/iso/setup-glemmen80.iso`  | glemmen80 |

The arguments are derived from `nodes/<host>.yaml` (`./setup-images.sh --print`
shows them). Each ISO contains two boot entries: the default one and a
"wipe" variant (`talos.experimental.wipe=system`); use the default.

If you boot the generic ISO instead, press `e` in GRUB and append the arguments
manually (example for glemmen130):

```
bond=bond0:eno8303,eno8403:mode=802.3ad,xmit_hash_policy=layer3+4,lacp_rate=slow,miimon=100 \
ip=192.168.6.130::192.168.6.1:255.255.255.0:glemmen130:bond0:off:192.168.6.1:1.1.1.1:
```

(For glemmen80 use `enp4s0f0` and glemmen60 `eno1` without `bond=`.)
The machine configuration sets all of this permanently; these arguments only
make the node reachable before the config is applied.

### Bootstrap the cluster (only once, first node)

```sh
./talos/manage.sh bootstrap          # glemmen130
./talos/manage.sh kubeconfig         # writes generated/kubeconfig
KUBECONFIG=talos/generated/kubeconfig kubectl get nodes
./talos/manage.sh health
```

## Storage layout

Longhorn gets its disks from the Talos user volumes (see `nodes/*.yaml`):

* `longhorn-system` — directory volume on the system disk (`/var/mnt/longhorn-system`),
  registered as the Longhorn disk `system` with a fixed reservation.
* `longhorn` — btrfs volume on the 2 TB data disk (`/var/mnt/longhorn`).
* `hdd1` — btrfs volume on the extra 1 TB / 7.3 TB disk where present
  (`/var/mnt/hdd1`, glemmen130/glemmen80/glemmen60).

The Longhorn disk configuration is applied *from the Talos node annotations*
(`node.longhorn.io/default-disks-config`) together with the label
`node.longhorn.io/create-default-disk: config`; Longhorn's Helm values enable
`createDefaultDiskLabeledNodes`. If a disk does not show up in the Longhorn UI,
check the node label/annotation and `talosctl -n <node> get mountstatus`.

btrfs support needs the `siderolabs/btrfs` extension **and** the kernel module;
both are in the schematic / common patch. Node upgrades preserve the volumes.

## Day-2 operations

```sh
./talos/manage.sh status                 # version, etcd members, machine status
./talos/manage.sh health                 # cluster health check
./talos/manage.sh patch glemmen130 file.yaml
./talos/manage.sh vip off glemmen170 glemmen180 glemmen80   # remove the API VIP
./talos/manage.sh wipe glemmen170 /dev/sdb                  # wipe a data disk
./talos/manage.sh reset glemmen170                          # wipe a node (careful)
./talos/manage.sh upgrade-k8s 1.36.6
./talos/manage.sh upgrade-talos v1.14.3
```

Patches are applied live (network/VIP changes are immediate, kubelet changes
need a reboot). `talosctl` can be used directly with
`TALOSCONFIG=talos/generated/talosconfig`.

## Notes for the remaining nodes (step 8)

* **glemmen80**: boot disk is a RAID1 mirror of `sda`+`sdb` (RAIDArrayConfig in
  `nodes/glemmen80.yaml`). **SecureBoot is enabled on this machine** — disable it
  in the BIOS (or build a SecureBoot-signed image) before installing.
* **glemmen60**: the 7.3 TB disk (`hdd1` user volume) is where the migration
  backups live while it is still running Ubuntu. Delete them (or accept that the
  Talos installation reformats the disk) before installing, per the plan.
* Nodes join by applying their config (they are control planes; no join token
  needed beyond the shared PKI in `generated/secrets.yaml`).
* After wiping a node, remove its old peer in the Netbird dashboard first, so
  the new Talos node can claim `<hostname>.blalange.intra` instead of getting a
  `-1` suffix.
