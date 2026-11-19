# Glemmen Fleet

This repository contains the configuration for 2ITA's small Kubernetes cluster. The files in this repository define what runs on the cluster and how it runs.

Everyone is welcome to propose changes and experiment with the cluster. To add or modify something, fork this repository, make the changes in your fork and open a pull request when you are done. We will review the changes and merge them into the main repository.

## Domains and networking

All ingress services using one of these domains are automatically available on the internet over HTTPS:

- `*.glem.blatrix.eu`
- `*.thisisaveryshortdomain.asso.eu.org`
- `*.shrail.eu.org`
- `*.glem.men`

## Infrastructure

The cluster consists of nine physical Talos Linux nodes: `glemmen50`, `glemmen60`, `glemmen80`, `glemmen250` and `glemmen130`–`glemmen180`. They run Talos Linux `v1.14.5` with Kubernetes `v1.37.2`.

`glemmen130`, `glemmen150`, `glemmen160`, `glemmen170` and `glemmen180` are control-plane nodes running etcd. `glemmen50`, `glemmen60`, `glemmen80` and `glemmen250` are workers.

### Hardware

The 1xx nodes are Dell PowerEdge R250 servers:

- Intel Xeon E-2314 CPU at 2.80 GHz
- 32 GB DDR4 RAM
- Disks:
  - `glemmen130`: 2 × 2 TB + 1 TB
  - `glemmen150`, `glemmen160`, `glemmen170`: 2 × 2 TB
  - `glemmen180`: 1 × 2 TB

`glemmen80` is a Dell PowerEdge R630:

- Intel Xeon E5-1650 v3 at 3.50 GHz
- 192 GB DDR4 RAM
- 2 × 1 TB HDD in RAID1 (system disk) + 1 TB HDD

`glemmen250` is a desktop machine used as the GPU worker:

- Intel Core i9-11900K at 3.50 GHz
- 12 GB DDR4 RAM
- 250 GB Kingston SA2000M8250G NVMe (system disk)
- 2 × NVIDIA GeForce GTX 1660 SUPER (Turing TU116, 6 GB each)

`glemmen60` is a Dell PowerEdge R515:

- 2 × AMD Opteron 4386
- 110 GB RAM
- 599 GB system disk + 8 TB data disk

`glemmen50` is an IBM System x3650 M3:

- 2 × Intel Xeon E5645
- 144 GB DDR3 RAM
- 598 GB virtual disk

The Dell iDRACs are scraped over Redfish by `idrac-exporter`
(`flux/monitoring/idrac-exporter/`). The `BMC` and `BMC overview` dashboards in
Grafana show per-server health, power, temperature, fans and disks.

### GPUs

`glemmen250` is the only GPU node. Talos provides the NVIDIA open kernel modules
and userspace libraries through the
`nvidia-open-gpu-kernel-modules-production` and
`nvidia-container-toolkit-production` system extensions in
`talos/schematic-gpu.yaml`; all other nodes use the base
`talos/schematic.yaml` without NVIDIA. The node config loads `nvidia`, `nvidia_uvm`,
`nvidia_drm` and `nvidia_modeset` and gives the node the stable label
`glem.men/gpu=true`.

The NVIDIA GPU Operator (`flux/infrastructure/gpu-operator/`) runs with its
driver container disabled, reuses the cluster's Node Feature Discovery, and
installs the container toolkit under `/var/lib/nvidia`. It exposes both GPUs as
`nvidia.com/gpu` through CDI/NRI, so CUDA pods do not need a
`runtimeClassName`; request a GPU with a `nvidia.com/gpu` limit. The operator's
DCGM exporter is scraped by Prometheus. The driver and toolkit extension
versions are tied to the Talos release: rebuild the schematic and roll the
installer image whenever `TALOS_VERSION` changes.

### Networking

The 1xx nodes have two 1 Gbit/s NICs. The cluster uses Calico with IPIP CrossSubnet and BGP disabled. Felix route programming is disabled; the `ecmp-route-agent` owns the pod-CIDR routes on every node. kube-proxy is pinned to iptables to match Calico's dataplane. All 1xx nodes use independent links: `eno8303` carries the server VLAN (`192.168.6.0/24`) plus the legacy `bond0.1` VLAN 1 interface used by PXE, and `eno8403.7` carries the ECMP VLAN (`192.168.7.0/24`). `glemmen80` uses its 10G `enp4s0f0` plus tagged `enp4s0f0.7`. `glemmen250` has a single 1 Gbit/s `enp6s0` link at `192.168.6.250`, so it uses the single-path server-VLAN route. The switch ports carry the server VLAN untagged and VLAN 1 and VLAN 7 tagged. The server and ECMP VLANs use MTU 9000 (jumbo frames); Calico is configured for 8980-byte pod veths (20 bytes reserved for IPIP), while the legacy `bond0.1` PXE/Default-VLAN interface stays at 1500. The `ecmp-route-agent` raises the 1xx `tg3` RX/Jumbo rings, `glemmen80`'s `ixgbe` rings, and handles the `bnx2`/`r8169` maxima on `glemmen60`/`glemmen250`. The agent installs two-nexthop multipath pod-CIDR routes and falls back to the single server-VLAN path when the peer's VLAN 7 address is unreachable. It requires consecutive probe failures before collapsing a path and consecutive successes before restoring it, so a single lost packet does not rewrite a live route. Verified 16-flow pod-to-pod throughput after the jumbo/ring change is about 1.9 Gbit/s upload and 1.75–1.83 Gbit/s download between the reachable nodes; single-flow stays around 0.9 Gbit/s. `glemmen60` has a single 1 Gbit/s link. `glemmen50` is online with pod CIDR `10.42.16.0/24`. All nodes have static addresses in `192.168.6.0/24`.

- **Kubernetes API**: `192.168.6.98` (Talos Layer2 VIP, advertised by the control-plane nodes).
- **Ingress**: `192.168.6.99` (MetalLB L2 pool `.96`–`.97` plus `.99`; `.98` is reserved for the Talos API VIP). The UniFi router forwards ports 80 and 443 to this address. Traefik runs as a DaemonSet on every 1xx node and `glemmen80`, and MetalLB speakers are restricted to the same nodes and server-VLAN interfaces. The Service uses `externalTrafficPolicy: Local`, so the VIP only lands on a node with a Traefik pod. Traefik terminates TLS with cert-manager wildcard certificates. Services that need additional ports on the same IP can use MetalLB with `metallb.io/loadBalancerIPs: 192.168.6.99`.
- **PXE/network boot**: a proxyDHCP/TFTP chainloader on the control-plane nodes and a Talos asset server at `http://192.168.6.99:8080` boot BIOS/UEFI machines from the current Talos kernel/initramfs on both the server VLAN (`192.168.6.0/24`) and the UDM Default VLAN (`192.168.50.0/24`). Known nodes boot their installed Talos system (including NVMe disks that the firmware cannot boot directly) or maintenance mode; unknown machines get a fallback menu offering netboot.xyz or a generic Talos maintenance-mode boot. See `flux/infrastructure/pxe/`.
- **Netbird** runs on every node as a Talos system extension, so the nodes remain reachable over the mesh network (`*.blalange.intra`). Kubernetes traffic uses the LAN addresses.
- **Network mesh tests**: `network-mesh-probe` runs a pod-network DaemonSet on every node, so the tests traverse the Calico pod-CIDR routes and the ECMP multipath links. Every 60 seconds each probe sends 10 ICMP echo requests to every other probe pod; every 30 minutes it runs a 3-second iperf3 TCP test with sixteen parallel streams in each direction. Tests are phase-shifted so only one node runs a throughput cycle at a time. Prometheus scrapes the results and the **Network Mesh** Grafana dashboard shows ping RTT, packet loss, upload/download throughput and capacity-relative utilization as source/destination tables. There are alerts for mesh loss/RTT, ECMP route flaps, Calico/MetalLB scrape failures, NIC and switch-port drops, and blackbox API/ingress/DNS probes. See `flux/monitoring/network-mesh/` for configuration and metrics.
- **Server accessories (VLAN 4)**: the Zyxel GS1920-24 at `192.168.4.2` has access ports 1–24 in `192.168.4.0/24` (iDRAC, APC and similar devices) and access ports 25–27 in the `Servere` VLAN (`192.168.6.0/24`); port 28 is a trunk to UDM Pro port 8 carrying VLAN 1 untagged and VLAN 3 and 4 tagged. The UDM provides the gateway and DHCP at `192.168.4.1` and `192.168.6.1`, and the APC has a DHCP reservation at `192.168.4.3`. The switch configuration is documented in `network/zyxel-gs1920-24/`.
- Nodes are managed with `talosctl` (see `talos/`), not over SSH.

### Storage

> **Ceph storage is live.** Rook Ceph provides `fast-rbd`/`fast-fs` on the
> CMR disks of glemmen130/150/160/170/180 and `nvme-rbd`/`nvme-fs` on the raw
> NVMe partitions of glemmen130/150/160/170/180/250, plus the `fast` RGW
> object store. The media library, Thanos blocks, Loki logs and YugabyteDB data
> live on Ceph;
> SeaweedFS has been removed. `bulk-rbd`/`bulk-fs` and the bulk RGW pools are
> deferred until `glemmen80` receives its 24 × 12 TB disks and external SAS
> HBA. The Ceph dashboard is at <https://ceph.glem.men>. See
> `flux/infrastructure/rook-ceph/README.md`.

The CMR disks are dedicated whole-disk Ceph OSDs. The remaining data disks are
unmounted:

| Node | System disk | Data storage |
|------|-------------|--------------|
| 130 | NVMe | sda+sdb: CMR Ceph OSDs (`cmr`); NVMe: raw Ceph OSD (`nvme`); sdc: SMR, unused |
| 150 | NVMe | sda: CMR Ceph OSD; NVMe: raw Ceph OSD (`nvme`); sdb: SMR, unused |
| 160 | nvme0n1 | sda: CMR Ceph OSD; NVMe: raw Ceph OSD (`nvme`); sdb: SMR, unused |
| 170 | nvme0n1 | sda: CMR Ceph OSD; NVMe: raw Ceph OSD (`nvme`); sdb: SMR, unused |
| 180 | nvme0n1 | sda: CMR Ceph OSD; NVMe: raw Ceph OSD (`nvme`) |
| 250 | 250 GB NVMe | 100 GiB EPHEMERAL + 140 GiB raw Ceph OSD (`nvme`) |
| 50 | 598 GB virtual disk | system disk only |
| 80 | sda | sdb/sdc: unmounted |
| 60 | sda | sdb: unmounted |

Ceph volume snapshots and off-cluster backups are not configured. The media
library is re-downloadable; Thanos and Loki protect their data with RF2 in the
same cluster, but there is no off-site copy yet.

Prometheus only retains the most recent metrics locally (15 GiB, see `retentionSize`). The Thanos side in `flux/monitoring/thanos/` uploads blocks to the `thanos` bucket in the Ceph RGW object store (`rook-ceph`), compacts and downsamples them, and exposes the history through a dedicated Grafana data source.

The media stack uses a 2.3 TiB CephFS `ReadWriteMany` volume, `media-data-ceph`, mounted at `/data` by Jellyfin, Sonarr, Radarr and qBittorrent. It lives on the `fast-fs` class (`cephfs-fast`, RF2 on the CMR OSDs). The migration copied the qBittorrent downloads and the existing Hærry Pot movie; the rest of the media library will be re-imported/moved from `/data/downloads` by Radarr and Sonarr after the cutover. See `flux/apps/media/README.md`.

Ceph RGW is exposed in-cluster at `rook-ceph-rgw-fast.rook-ceph.svc.cluster.local:80`. The `thanos` and `loki` buckets and their users are managed by Rook; the access keys are copied into the hand-created `thanos-objstore-config` and `loki-s3` Secrets in `monitoring`, not stored in Git.

### Databases

NetBox uses its own PostgreSQL instance in the `netbox` namespace (see `flux/apps/netbox/`). Grafana uses a three-node YugabyteDB (YSQL) cluster in the `yugabyte` namespace (`flux/infrastructure/yugabyte/`) with replication factor 3. The cluster is managed by the official `yugabytedb/yugabyte` Helm chart (three YB-Master and three YB-TServer pods), not the earlier hand-written StatefulSet. Clients connect to `yb-tservers.yugabyte.svc.cluster.local:5433`; the database is not exposed outside the cluster. The media apps use SQLite on Ceph `nvme-rbd` volumes: the Servarr apps cannot parse YB's `server_version` string, Jellyfin has no supported PostgreSQL backend, and Seerr's TypeORM migrations do not complete reliably on YSQL.

Passwords are stored in Kubernetes Secrets, not in Git: `netbox-secrets` in `netbox` for NetBox, `yugabyte-auth` in `yugabyte` (superuser and `grafana` role passwords) plus `grafana-db` in `monitoring` for Grafana. See `flux/infrastructure/yugabyte/README.md` for setup and administration.

## Repository layout

- `inventory/` — the NetBox inventory data and device-type images. The collector, sync job and language server live in the separate [netbox-inventory-toolkit](https://github.com/Blalange/netbox-inventory-toolkit) repository. See [`inventory/README.md`](inventory/README.md) for the inventory model, source mapping, collector usage and sync instructions. The inventory ConfigMaps are generated by the separate Flux Kustomization `flux/clusters/glemmen/apps/netbox-inventory.yaml`.
- `flux/` — Flux v2 configuration. `scripts/flux/setup.py` bootstraps Flux against this repository, and `scripts/flux/manage.py` shows status and suspends/resumes reconciliation. Application manifests live under `flux/apps/`, infrastructure under `flux/infrastructure/`, monitoring under `flux/monitoring/` and ingress under `flux/ingress/`.
- `talos/` — Talos machine configuration: `schematic.yaml` (base Image Factory extensions), `schematic-gpu.yaml` (GPU extensions for `TALOS_GPU_NODES`), `nodes/` (per-node documents), `patches/` and a runbook in `talos/README.md`. Generated configurations and secrets live in `talos/generated/` (not committed).
- `scripts/` — operational and generation scripts: `scripts/talos/` (gen-iso, setup-images, gen-config, gen-pxe, manage), `scripts/flux/` (setup, manage), `scripts/download-tools.py` (pinned `talosctl`/`kubectl`/`flux`/`pv-migrate` into `scripts/bin/`), `scripts/check-updates.py` (Talos/Kubernetes/Flux/Helm/image updates), `scripts/redfish.py` (parallel multi-server Redfish power/fan control), plus `network-test.py` and `update-grafana-dashboards.py`.
- `network/` — network hardware documentation and switch backups.

## GitOps and configuration

Flux is the source of truth for the cluster. Changes are made in Git and reconciled by Flux. The inventory has two layers:

- `inventory/generated/` is written by `netbox-inventory-toolkit collect` from UniFi, Talos, Redfish, IPMI and SNMP. Do not edit it by hand.
- `inventory/manual/` is maintained by hand and owns models, rack layout, physical servers, accessories, manual cables, VIPs, ownership and contacts.

The `netbox-sync` CronJob merges both layers, with manual values winning, enriches device and module types from the public NetBox Device Type Library at runtime, and reconciles the result into NetBox. Every taggable object created by the job carries the `gitops` tag and its provenance source tags (`manual`, `unifi`, `talos`, `redfish`, `ipmi`, `snmp`, `dtl`); pruning only removes tagged objects and never touches objects created manually in NetBox. Owner groups and owners are not taggable and are never pruned; contacts and contact assignments are tagged and pruned like other inventory objects.

The collector and sync are separate processes:

- The collector runs locally (from the pinned toolkit revision) with access to whichever sources are configured, and commits the generated inventory.
- The sync runs in Kubernetes with the committed inventory, device images, a NetBox API token and an optional DTL definition/image cache on the media volume.

## History

The cluster was migrated from K3s (Ubuntu 24.04, Rancher + Fleet) to Talos Linux + Flux in October 2026. The migration plan and background are documented in the pull request and commit history; the old configuration (`fleet.yaml` files) has been removed.
