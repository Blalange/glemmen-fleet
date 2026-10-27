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

The cluster consists of eight physical Talos Linux nodes: `glemmen50`, `glemmen60`, `glemmen80` and `glemmen130`–`glemmen180`. They run Talos Linux `v1.14.4` with Kubernetes `v1.37.1`.

`glemmen130`, `glemmen150`, `glemmen160`, `glemmen170` and `glemmen180` are control-plane nodes running etcd. `glemmen50`, `glemmen60` and `glemmen80` are workers.

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

### Networking

The 1xx nodes have two 1 Gbit/s NICs in an LACP bond (`bond0`, 802.3ad, layer3+4). `glemmen80` has a 10 Gbit/s link and `glemmen60` has a 1 Gbit/s link. All nodes have static addresses in `192.168.6.0/24`.

- **Kubernetes API**: `192.168.6.98` (Talos Layer2 VIP, advertised by the control-plane nodes).
- **Ingress**: `192.168.6.99` (MetalLB L2 pool `.96`–`.99`). The UniFi router forwards ports 80 and 443 to this address. Traefik runs in `kube-system` and terminates TLS with cert-manager wildcard certificates. Services that need additional ports on the same IP can use MetalLB with `metallb.io/loadBalancerIPs: 192.168.6.99`.
- **Netbird** runs on every node as a Talos system extension, so the nodes remain reachable over the mesh network (`*.blalange.intra`). Kubernetes traffic uses the LAN addresses.
- **Server accessories (VLAN 4)**: the Zyxel GS1920-24 at `192.168.4.2` has access ports 1–27 in `192.168.4.0/24` (iDRAC, APC and similar devices); port 28 is a trunk to UDM Pro port 8. The UDM provides the gateway and DHCP at `192.168.4.1`, and the APC has a DHCP reservation at `192.168.4.3`. The switch configuration is documented in `network/zyxel-gs1920-24/`.
- Nodes are managed with `talosctl` (see `talos/`), not over SSH.

### Storage

Longhorn (`v1.12`) provides all PVCs. The data disks are btrfs volumes mounted by Talos:

| Node | System disk | Longhorn data disks |
|------|-------------|---------------------|
| 130 | sda | sdb (`/var/mnt/longhorn`) + sdc (`/var/mnt/hdd1`) + system disk |
| 150/160/170 | sda | sdb (`/var/mnt/longhorn`) + system disk |
| 180 | sda | system disk |
| 50 | 598 GB virtual disk | system disk |
| 80 | RAID1 sda+sdb | sdc (`/var/mnt/hdd1`) + system disk |
| 60 | sda | sdb (`/var/mnt/hdd1`) + system disk |

Longhorn volume backups go to a Google Drive folder through an in-cluster rclone S3 bridge (`flux/infrastructure/longhorn/rclone/`). Longhorn uses the backup target `s3://longhorn-backup@us-east-1/`; `rclone.conf` and the S3 keys are stored in the `rclone-backup-config` Secret in `longhorn-system`.

Prometheus only retains the most recent metrics locally (15 GiB, see `retentionSize`). The Thanos side in `flux/monitoring/thanos/` uploads blocks to the `thanos` bucket on the same rclone bridge, compresses and downsamples them, and exposes the history through a dedicated Grafana data source.

### Databases

NetBox uses its own PostgreSQL instance in the `netbox` namespace (see `flux/apps/netbox/`). Grafana uses a separate three-node YugabyteDB (YSQL) cluster in the `yugabyte` namespace (`flux/infrastructure/yugabyte/`) with replication factor 3 and two Grafana replicas. Grafana connects to `yugabyte.yugabyte.svc.cluster.local:5433`; the database is not exposed outside the cluster.

Passwords are stored in Kubernetes Secrets, not in Git: `netbox-secrets` in `netbox` for NetBox, and `yugabyte-auth` in `yugabyte` (superuser and `grafana`) plus `grafana-db` in `monitoring` for Grafana. See `flux/infrastructure/yugabyte/README.md` for setup and administration.

## Repository layout

- `inventory/` — the NetBox inventory, collector, sync code, language server and device-type images. See [`inventory/README.md`](inventory/README.md) for the inventory model, source mapping, collector usage and sync instructions. The inventory ConfigMaps are generated by the separate Flux Kustomization `flux/clusters/glemmen/apps/netbox-inventory.yaml`.
- `editors/vscode/inventory-lsp/` — thin VS Code client for the inventory language server (diagnostics, Ctrl-click, hover, completion and rename).
- `flux/` — Flux v2 configuration. `scripts/flux/setup.py` bootstraps Flux against this repository, and `scripts/flux/manage.py` shows status and suspends/resumes reconciliation. Application manifests live under `flux/apps/`, infrastructure under `flux/infrastructure/`, monitoring under `flux/monitoring/` and ingress under `flux/ingress/`.
- `talos/` — Talos machine configuration: `schematic.yaml` (Image Factory extensions), `nodes/` (per-node documents), `patches/` and a runbook in `talos/README.md`. Generated configurations and secrets live in `talos/generated/` (not committed).
- `scripts/` — operational and generation scripts: `scripts/talos/` (gen-iso, setup-images, gen-config, manage), `scripts/flux/` (setup, manage), `scripts/download-tools.py` (pinned `talosctl`/`kubectl`/`flux` into `scripts/bin/`), `scripts/check-updates.py` (Talos/Kubernetes/Flux/Helm/image updates), `scripts/redfish.py` (parallel multi-server Redfish power/fan control), plus `network-test.py` and `update-grafana-dashboards.py`.
- `network/` — network hardware documentation and switch backups.

## GitOps and configuration

Flux is the source of truth for the cluster. Changes are made in Git and reconciled by Flux. The inventory has two layers:

- `inventory/generated/` is written by `inventory/scripts/collect.py` from UniFi, Talos, Redfish, IPMI and SNMP. Do not edit it by hand.
- `inventory/manual/` is maintained by hand and owns models, rack layout, physical servers, accessories, manual cables, VIPs, ownership and contacts.

The `netbox-sync` CronJob merges both layers, with manual values winning, enriches device and module types from the public NetBox Device Type Library at runtime, and reconciles the result into NetBox. Every taggable object created by the job carries the `gitops` tag and its provenance source tags (`manual`, `unifi`, `talos`, `redfish`, `ipmi`, `snmp`, `dtl`); pruning only removes tagged objects and never touches objects created manually in NetBox. Owner groups and owners are not taggable and are never pruned; contacts and contact assignments are tagged and pruned like other inventory objects.

The collector and sync are separate processes:

- The collector runs locally with access to UniFi, Talos, Redfish/IPMI and SNMP and commits the generated inventory.
- The sync runs in Kubernetes with the committed inventory, device images, a NetBox API token and an optional DTL definition/image cache on the media volume.

## History

The cluster was migrated from K3s (Ubuntu 24.04, Rancher + Fleet) to Talos Linux + Flux in October 2026. The migration plan and background are documented in the pull request and commit history; the old configuration (`fleet.yaml` files) has been removed.
