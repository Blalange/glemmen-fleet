# Talos

Talos Linux v1.14.5 configuration for the glemmen cluster (Kubernetes 1.37.2).
The `glemmen1xx` machines run control plane + etcd + worker roles; `glemmen80`,
`glemmen60`, `glemmen250` and `glemmen50` are workers. `glemmen250` is the GPU
node (2 × GeForce GTX 1660 SUPER).

## Files

| Path                               | Purpose                                                                                                     |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `settings.env`                     | Versions, endpoint/VIP, node table — used by the scripts, Tuppr and `check-updates.py`                      |
| `schematic.yaml`                   | Image Factory extensions: iscsi-tools, util-linux-tools, btrfs, netbird, bnx2 firmware, NVIDIA driver/toolkit |
| `patches/common.yaml`              | Common machine config patch (subnets, DNS, kubelet node IP, btrfs)                                          |
| `patches/netbird.yaml.tpl`         | Netbird extension config template (`NB_SETUP_KEY` filled in by `scripts/talos/gen-config.py`)               |
| `nodes/glemmen*.yaml`              | Per-node: hostname, bond/addresses, Ceph OSD/NVMe partitions, VIP                                           |
| `profiles/*.yaml`                  | Reusable, non-host-specific worker profiles (e.g. `ephemeral-worker`)                                       |
| `pxe.yaml`                         | SMBIOS serial/UUID/MAC identities used by the PXE chainloader                                               |
| `../scripts/talos/gen-iso.py`      | Creates the schematic (base image) and prints/downloads the generic ISO + installer image                   |
| `../scripts/talos/setup-images.py` | Per-node setup ISOs with the maintenance network baked in (`--print` shows the kernel args)                 |
| `../scripts/talos/gen-config.py`   | Renders secrets + configs into `generated/` (gitignored) and validates them                                 |
| `../scripts/talos/gen-pxe.py`      | Renders `flux/infrastructure/pxe/boot.ipxe` from `pxe.yaml` and the node configs                            |
| `../scripts/talos/manage.py`       | Day-2 operations (apply, bootstrap, health, logs, cordon/drain, reboot/shutdown, etcd, vip, wipe, upgrades) |
| `secrets/netbird.env`              | Netbird setup key (gitignored, see `secrets/README.md`)                                                     |
| `generated/`                       | Derived PKI, kubeconfig, machine configs (gitignored — **back this up**)                                    |

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
themselves. `gen-config.py`, `gen-iso.py`, `gen-pxe.py` and
`setup-images.py` need PyYAML (`python3 -m pip install pyyaml`).

## Installing a node (steps 6 and 8)

The cluster runs a PXE chainloader (`flux/infrastructure/pxe/`) for nodes whose
firmware cannot boot the installed disk directly, including Talos installations
on PCIe NVMe drives. Known nodes are matched by SMBIOS serial/UUID/MAC and load
the current Talos kernel/initramfs. If Talos is already installed, the network
boot hands off to the installed system; otherwise the node comes up in
maintenance mode. Unknown machines get a fallback menu offering netboot.xyz or
a generic Talos maintenance-mode boot. The setup ISOs remain the fallback when
the cluster or network boot infrastructure is down. All 1xx nodes use
independent links with PXE on `eno8303`; the switch ports carry the server VLAN
untagged plus VLAN 1 and VLAN 7 tagged. The server and VLAN 7 interfaces run at
MTU 9000; the legacy `bond0.1` VLAN 1 stays at 1500 for PXE and the Default
network, and PXE clients negotiate 1500-byte TCP MSS. Calico pods use 8980
bytes. Test PXE on one node first; if the
firmware cannot reach the proxyDHCP, use a non-LAG provisioning port or the
ISO. glemmen170 is the exception: its system disk is an NVMe drive that the
R250 firmware cannot boot, so it relies on the PXE chainloader for every boot.
Its switch ports are already converted to the same independent-link layout.

1. Boot the node over PXE, or use the Image Factory ISO (USB or virtual media)
   when PXE is unavailable.
2. Give the node network access in maintenance mode (see below). The PXE script
   already includes the same per-node static-IP arguments.
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

The nodes have no DHCP on their primary interfaces, so each machine normally
needs kernel arguments before it can be reached in maintenance mode.
`scripts/talos/setup-images.py` builds a per-node ISO with exactly those
arguments baked into the boot entries,
so no GRUB editing is needed:

| File                                       | Boot with  |
| ------------------------------------------ | ---------- |
| `talos/generated/iso/setup-glemmen130.iso` | glemmen130 |
| `talos/generated/iso/setup-glemmen150.iso` | glemmen150 |
| `talos/generated/iso/setup-glemmen160.iso` | glemmen160 |
| `talos/generated/iso/setup-glemmen170.iso` | glemmen170 |
| `talos/generated/iso/setup-glemmen180.iso` | glemmen180 |
| `talos/generated/iso/setup-glemmen250.iso` | glemmen250 |
| `talos/generated/iso/setup-glemmen60.iso`  | glemmen60  |
| `talos/generated/iso/setup-glemmen80.iso`  | glemmen80  |

The arguments are derived from `nodes/<host>.yaml`
(`./scripts/talos/setup-images.py --print` shows them). The PXE boot script
uses the same arguments (`scripts/talos/gen-pxe.py`); each ISO contains two
boot entries: the default one and a
"wipe" variant (`talos.experimental.wipe=system`); use the default.

> **Broadcom NICs**: the schematic includes the `bnx2-bnx2x` firmware
> extension. The stock Talos image ships the bnx2 kernel module but *not* its
> firmware, so a Broadcom NetXtreme NIC (e.g. glemmen60's R515 LOM) gets no
> interface at all — neither in maintenance mode nor after install. Any new
> machine with such a NIC must boot an ISO built from this schematic.
>
> **NVIDIA GPUs**: the schematic includes the open kernel modules and the
> container toolkit for glemmen250's GTX 1660 SUPER cards. `nodes/glemmen250.yaml`
> loads the four `nvidia*` kernel modules. The GPU operator in
> `flux/infrastructure/gpu-operator/` consumes them; see the main README for the
> GPU stack.

If you boot the generic ISO instead, press `e` in GRUB and append the arguments
manually (example for glemmen130):

```
ip=192.168.6.130::192.168.6.1:255.255.255.0:glemmen130:eno8303:off:192.168.6.1:1.1.1.1:
```

(For glemmen80 use `enp4s0f0` without `bond=`. glemmen250 uses `enp6s0` with
`192.168.6.250`; its NVMe also cannot boot directly, so it normally comes up
through PXE. glemmen60 uses `eno1` - a single onboard LOM on a plain access
switch port - and its pre-built setup ISO boots without arguments, using DHCP
on the Servere VLAN instead; apply the config with
`./scripts/talos/manage.py apply glemmen60 <dhcp-address>`.)
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

Ceph consumes raw block devices; the former Talos user volumes are gone.
The CMR OSDs are the whole disks on glemmen130/150/160/170/180. The NVMe OSDs
are the `r-nvme-osd` partitions created by `RawVolumeConfig` on every
control-plane node and glemmen250; `EPHEMERAL` is capped at 100 GiB to leave
room.

The former data disks (`hdd1`/`hdd2` on glemmen60/glemmen80 and `smr1` on
glemmen160/glemmen170) are unmounted and available for reuse. Do not recreate
the old `UserVolumeConfig`/`KubeNodeConfig` entries.

btrfs support is retained for future user volumes; it is not needed by Ceph.

## Ephemeral worker nodes

`talos/profiles/ephemeral-worker.yaml` is a reusable worker profile for
short-lived or unstable machines. It is rendered like the named nodes, but it
is not listed in `TALOS_NODES` and contains no hostname or static address:

```sh
./scripts/talos/gen-config.py
./scripts/talos/manage.py apply ephemeral-worker <maintenance-address>
```

The generated file is `talos/generated/ephemeral-worker.yaml` (it contains the
cluster secrets). Every machine gets a unique automatic hostname and uses DHCP;
the DHCP address must be in `192.168.6.0/24` to match the common kubelet
`nodeIP` configuration.

What the profile does:

* installs to the first non-rotational SSD it finds, ignoring USB/CD-ROM and
  disks smaller than 20 GiB;
* leaves other whole, unpartitioned non-system disks untouched for manual use;
* encrypts `STATE` and `EPHEMERAL` with LUKS2. The TPM key is tried first
  (bound to PCR 11 so it also works without SecureBoot), with `nodeID` as the
  fallback on machines without a usable TPM. On a machine without a TPM,
  expect a failed TPM key sync in the volume logs before Talos falls back;
* labels the node `glem.men/ephemeral=true` and taints it
  `glem.men/ephemeral=true:NoSchedule`, so ordinary workloads need an
  explicit node selector and matching toleration.

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
into the new image. The NVIDIA kernel modules and container toolkit are built
for a specific Talos release and driver branch, so they must be rebuilt and
rolled out together when `TALOS_VERSION` changes.

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
./scripts/talos/manage.py mounts glemmen130      # user-volume mount status
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

### NetBird and Calico

NetBird runs as a Talos system extension. On a NetBird start/restart its
nftables firewall manager writes native accept rules for the `wt0` interface
into Calico's iptables `ip filter` table, because the Talos host has no
`iptables` binary. The iptables 1.8.10 in calico-node cannot parse native nft
rules, so Felix panics and stops programming new workload endpoints; new pods
then fail DNS/UDP with packets hitting Calico's `Unknown interface` DROP.

The `netbird-guard` Flux Kustomization
(`flux/clusters/glemmen/netbird-guard.yaml`) removes those specific native
rules every five seconds. It is independent of the `infrastructure`
Kustomization so it can still run while Calico is unhealthy. The guard keeps
NetBird's own ACL table intact; the removed rules are redundant because there
are no Calico `HostEndpoint` resources.

Calico's `FelixConfiguration` also sets `xdpEnabled: false`: Calico's XDP
prefilter cannot detach the XDP program that NetBird's eBPF WireGuard proxy
attaches to `lo`, which made Felix retry the removal forever and delayed
endpoint programming. XDP is not used with `bpfEnabled: false`.

After `./scripts/talos/manage.py netbird`, confirm the guard pods are running
and calico-node stays ready on every node:

```sh
kubectl -n netbird-guard get pods -o wide
kubectl -n calico-system get pods -l k8s-app=calico-node
```

## Notes for the remaining nodes

* **glemmen80** is a **worker** node. Its disk slots were shuffled: it boots from
  the ST91000640NS disk (`naa.5000c500647fdfa5`); the two WD 1 TB disks are
  unmounted after the storage migration. **SecureBoot is enabled on this machine** —
  disable it in the BIOS (or build a SecureBoot-signed image) before installing.
  The APC automatic transfer switch is handled by the Zyxel GS1920-24 switch on
  VLAN 4 (`192.168.4.0/24`), not by this node. Do not configure a VLAN/bridge
  for `eno1` on glemmen80; see `network/zyxel-gs1920-24/`.
* **glemmen60** is a **worker** node (config ready, installed). The 7.3 TB
  disk is unmounted after the storage cleanup; the old migration
  NFS export was retired when the node was wiped and joined.
* Worker nodes join by applying their config; they do not run etcd.
  Control plane nodes are the 1xx machines only.
* After wiping a node, remove its old peer in the Netbird dashboard first, so
  the new Talos node can claim `<hostname>.blalange.intra` instead of getting a
  `-1` suffix.
