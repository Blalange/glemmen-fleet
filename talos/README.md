# Talos

Talos Linux v1.14.2 configuration for the glemmen cluster (Kubernetes 1.37.0).
The `glemmen1xx` machines run control plane + etcd + worker roles; `glemmen80`,
`glemmen60` and `glemmen50` are workers.

## Files

| Path | Purpose |
|------|---------|
| `settings.env` | Versions, endpoint/VIP, node table — used by the scripts, Tuppr and `check-updates.py` |
| `schematic.yaml` | Image Factory extensions: iscsi-tools, util-linux-tools, btrfs, netbird |
| `patches/common.yaml` | Common machine config patch (subnets, DNS, kubelet node IP, btrfs, Longhorn system disk) |
| `patches/netbird.yaml.tpl` | Netbird extension config template (`NB_SETUP_KEY` filled in by `scripts/talos/gen-config.py`) |
| `nodes/glemmen*.yaml` | Per-node: hostname, bond/addresses, Longhorn disks, VIP, Longhorn node annotation |
| `../scripts/talos/gen-iso.py` | Creates the schematic (base image) and prints/downloads the generic ISO + installer image |
| `../scripts/talos/setup-images.py` | Per-node setup ISOs with the maintenance network baked in (`--print` shows the kernel args) |
| `../scripts/talos/gen-config.py` | Renders secrets + configs into `generated/` (gitignored) and validates them |
| `../scripts/talos/manage.py` | Day-2 operations (apply, bootstrap, health, logs, cordon/drain, reboot/shutdown, etcd, vip, wipe, upgrades) |
| `secrets/netbird.env` | Netbird setup key (gitignored, see `secrets/README.md`) |
| `generated/` | Derived PKI, kubeconfig, machine configs (gitignored — **back this up**) |

## First-time setup

```sh
# 1. Download pinned talosctl/kubectl/flux into scripts/bin/
./scripts/download-tools.py

# 2. Netbird setup key (dashboard -> Setup Keys, reusable)
cp talos/secrets/netbird.env.example talos/secrets/netbird.env  # or create it:
echo 'NB_SETUP_KEY=xxxx' > talos/secrets/netbird.env

# 3. Build the schematic and (optionally) download the generic ISO
./scripts/talos/gen-iso.py --download

# 4. Build one setup ISO per machine (maintenance network baked in, no GRUB editing)
./scripts/talos/setup-images.py

# 5. Generate and validate all machine configs (creates the cluster PKI once)
./scripts/talos/gen-config.py
```

`./scripts/download-tools.py` downloads the pinned `talosctl`, `kubectl` and
`flux` binaries into `scripts/bin/`. The other scripts only use those (or a
binary from `PATH`/environment override); they no longer download tools
themselves. `gen-config.py`, `gen-iso.py` and `setup-images.py` need PyYAML
(`python3 -m pip install pyyaml`).

## Installing a node (steps 6 and 8)

1. Boot the Image Factory ISO (USB, virtual media, PXE) with UEFI.
2. Give the node network access in maintenance mode (see below).
3. Apply the node's config and let the installer run:

   ```sh
   ./scripts/talos/manage.py apply glemmen130
   # the config contains UnattendedInstallConfig (wipe + reboot), so the node
   # installs Talos to /dev/sda and reboots into it
   ```

4. Verify the node came up:

   ```sh
   ./scripts/talos/manage.py status
   ./scripts/talos/manage.py disks glemmen130        # check sdb/sdc are detected
   ./scripts/talos/manage.py services glemmen130
   ```

### Maintenance-mode network

The nodes have no DHCP on their bonded interfaces, so each machine normally needs
kernel arguments before it can be reached in maintenance mode.
`scripts/talos/setup-images.py` builds a per-node ISO with exactly those
arguments baked into the boot entries,
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

The arguments are derived from `nodes/<host>.yaml`
(`./scripts/talos/setup-images.py --print` shows them). Each ISO contains two
boot entries: the default one and a
"wipe" variant (`talos.experimental.wipe=system`); use the default.

> **Broadcom NICs**: the schematic includes the `bnx2-bnx2x` firmware
> extension. The stock Talos image ships the bnx2 kernel module but *not* its
> firmware, so a Broadcom NetXtreme NIC (e.g. glemmen60's R515 LOM) gets no
> interface at all — neither in maintenance mode nor after install. Any new
> machine with such a NIC must boot an ISO built from this schematic.

If you boot the generic ISO instead, press `e` in GRUB and append the arguments
manually (example for glemmen130):

```
bond=bond0:eno8303,eno8403:mode=802.3ad,xmit_hash_policy=layer3+4,lacp_rate=slow,miimon=100 \
ip=192.168.6.130::192.168.6.1:255.255.255.0:glemmen130:bond0:off:192.168.6.1:1.1.1.1:
```

(For glemmen80 use `enp4s0f0` without `bond=`. glemmen60 uses `eno1` - a
single onboard LOM on a plain access switch port - and its pre-built setup ISO
boots without arguments, using DHCP on the Servere VLAN instead; apply the
config with `./scripts/talos/manage.py apply glemmen60 <dhcp-address>`.)
The machine configuration sets all of this permanently; these arguments only
make the node reachable before the config is applied.

### Bootstrap the cluster (only once, first node)

```sh
./scripts/talos/manage.py bootstrap          # glemmen130
./scripts/talos/manage.py kubeconfig         # writes generated/kubeconfig
KUBECONFIG=talos/generated/kubeconfig kubectl get nodes
./scripts/talos/manage.py health
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

## Checking for updates

`scripts/check-updates.py` compares the pinned versions with upstream releases
and scans the Flux manifests for Helm chart and container-image updates:

```sh
./scripts/check-updates.py
./scripts/check-updates.py --no-images
./scripts/check-updates.py --json
```

Tuppr's `TalosUpgrade` and `KubernetesUpgrade` versions are not separate pins:
`flux/infrastructure/tuppr/resources/kustomization.yaml` generates them from
`TALOS_VERSION` and `KUBERNETES_VERSION` in `settings.env` via Kustomize
replacements. Change the settings file and let Flux reconcile; no Tuppr YAML
edit is needed.

## Updating nodes after a config or schematic change

### Common patch, control-plane patch or per-node config

1. Edit `patches/common.yaml`, `patches/control-plane.yaml` or
   `nodes/<host>.yaml`.
2. Render and validate all configs:

   ```sh
   ./scripts/talos/gen-config.py
   ```

3. Preview the change on a running node:

   ```sh
   ./scripts/talos/manage.py update glemmen130 --dry-run
   ```

   The dry-run diff is the full machine config, so it includes secrets such as
   `NB_SETUP_KEY`; do not paste that output into a ticket or chat.

4. Apply it. `update` uses the normal (non-insecure) Talos API and applies the
   generated `talos/generated/<host>.yaml`:

   ```sh
   ./scripts/talos/manage.py update glemmen130
   ./scripts/talos/manage.py update --workers-first      # all nodes, workers first
   ```

   `--mode auto` is the default and lets Talos apply and reboot when required;
   use `--mode no-reboot` to stage a change for the next reboot. `update` waits
   for each node to answer again unless `--no-wait` is given.

5. Verify with `./scripts/talos/manage.py status` and
   `./scripts/talos/manage.py health`.

`apply <node>` and `apply-all` remain the maintenance-mode commands for a node
booted from the ISO (`--insecure`). Do not use them for already-installed
nodes. The `netbird` and `vip` commands still apply those specific changes
directly.

### Schematic / system extensions

System extensions are part of the installer image, so changing
`schematic.yaml` cannot be applied with `apply-config`; the node must reboot
into the new image.

1. Edit `talos/schematic.yaml`.
2. Create the new schematic and installer image:

   ```sh
   ./scripts/talos/gen-iso.py
   ```

   This updates `talos/generated/schematic-id` and prints the new installer
   image.

3. Re-render the machine configs so they reference the new installer image:

   ```sh
   ./scripts/talos/gen-config.py
   ```

4. Roll the new image out to the running nodes:

   ```sh
   ./scripts/talos/manage.py upgrade-talos --workers-first
   ```

   With no version argument it uses `TALOS_VERSION` from `settings.env`, so a
   schematic-only change keeps the current version. The schematic/installer
   image is different, and `upgrade-talos` reads the new schematic ID from
   `generated/schematic-id`, reboots each node and waits for it to come back.

5. Rebuild the per-node setup ISOs for future installs:

   ```sh
   ./scripts/talos/setup-images.py --force
   ```

6. Verify extensions and versions with `talosctl get extensions`, or with
   `./scripts/talos/manage.py version` and `status`.

## Day-2 operations

```sh
./scripts/talos/manage.py status                 # version, etcd members/status, machine status
./scripts/talos/manage.py update glemmen130      # apply regenerated config to a running node
./scripts/talos/manage.py update --workers-first # update all running nodes, workers first
./scripts/talos/manage.py health                 # cluster health check
./scripts/talos/manage.py wait                   # wait until every node's Talos API answers
./scripts/talos/manage.py version                # client/server versions
./scripts/talos/manage.py etcd members           # etcd membership
./scripts/talos/manage.py etcd status            # per-member etcd health
./scripts/talos/manage.py etcd snapshot          # save a snapshot to generated/
./scripts/talos/manage.py roles                  # control planes = all roles, workers = worker only
./scripts/talos/manage.py cordon glemmen170      # mark a node unschedulable
./scripts/talos/manage.py drain glemmen170       # cordon + evict workloads
./scripts/talos/manage.py uncordon glemmen170    # make it schedulable again
./scripts/talos/manage.py reboot glemmen170      # drain + reboot, wait for it to come back
./scripts/talos/manage.py shutdown glemmen170    # graceful Talos shutdown (cordons/drains first)
./scripts/talos/manage.py links glemmen60        # interface names/MACs of a node
./scripts/talos/manage.py disks glemmen130       # disks for selectors
./scripts/talos/manage.py mounts glemmen130      # user-volume mount status (Longhorn)
./scripts/talos/manage.py logs glemmen130 etcd   # service logs (add -f to follow)
./scripts/talos/manage.py dmesg glemmen130       # kernel log
./scripts/talos/manage.py patch glemmen130 file.yaml
./scripts/talos/manage.py netbird                # re-render + apply the Netbird config
./scripts/talos/manage.py vip off glemmen170 glemmen180  # remove the API VIP
./scripts/talos/manage.py wipe glemmen170 /dev/sdb       # wipe a data disk
./scripts/talos/manage.py reset glemmen170               # wipe a node (careful)
./scripts/talos/manage.py upgrade-k8s 1.36.6
./scripts/talos/manage.py upgrade-talos v1.14.3
```

`vip`, `netbird`, `apply-all`, `shutdown`, `reboot` and `upgrade-talos` default to
the node table (VIP uses the control planes only). Node arguments accept names,
addresses and comma-separated lists. Commands that access a node before its
config is applied (`links`, `disks`, `mounts`, `services`, `logs`, `dmesg`,
`wipe`) accept `--insecure`; `apply <config-name> <address>` (or `--config`)
handles a maintenance-mode address that is not in the node table.

`scripts/talos/manage.py` prefers the pinned `scripts/bin/talosctl` (matching
`TALOS_VERSION`) over a `talosctl` in `PATH`, and uses the control-plane nodes
as Talos API endpoints. Worker nodes cannot forward API requests: including them
in the talosconfig causes intermittent
`PermissionDenied: no request forwarding` errors.
`scripts/talos/gen-config.py` writes control-plane-only endpoints; rerun it once
if you have an older `generated/talosconfig`. `TALOS_ENDPOINTS` can override the
endpoint list.

Patches are applied live (network/VIP changes are immediate, kubelet changes
need a reboot). `talosctl` can be used directly with
`TALOSCONFIG=talos/generated/talosconfig`.

## Notes for the remaining nodes

* **glemmen80** is a **worker** node. Its disk slots were shuffled: it boots from
  the ST91000640NS disk (`naa.5000c500647fdfa5`) and uses the two WD 1 TB disks as
  Longhorn data disks (`hdd1`/`hdd2`). **SecureBoot is enabled on this machine** —
  disable it in the BIOS (or build a SecureBoot-signed image) before installing.
  The APC automatic transfer switch is handled by the Zyxel GS1920-24 switch on
  VLAN 4 (`192.168.4.0/24`), not by this node. Do not configure a VLAN/bridge
  for `eno1` on glemmen80; see `network/zyxel-gs1920-24/`.
* **glemmen60** is a **worker** node (config ready, installed). The
  7.3 TB disk (`hdd1` user volume) is used for Longhorn data; the old
  migration NFS export was retired when the node was wiped and joined.
* Worker nodes join by applying their config; they do not run etcd.
  Control plane nodes are the 1xx machines only.
* After wiping a node, remove its old peer in the Netbird dashboard first, so
  the new Talos node can claim `<hostname>.blalange.intra` instead of getting a
  `-1` suffix.
