# Glemmen Fleet

Dette git-repositoriet inneholder konfigurasjon for 2ITA sin lille Kubernetes cluster, filene i denne repoen definerer hva som kjører og hvordan det kjører.

Alle får lov til å komme med endringer og eksperimentere med clusteret.
For å legge til eller modifisere noe, lag en fork av dette repositoriet, gjør endringene i din egen fork og lag en pull request når du er ferdig. Vi vil da se over endringene og merge dem inn i hovedrepoet.

## Domene og nettverk
Alle ingress tjenester med et av disse domenene vil automatisk være tilgjengelig på internett via HTTPS:
- *.glem.blatrix.eu
- *.thisisaveryshortdomain.asso.eu.org
- *.shrail.eu.org
- *.glem.men

## Infrastruktur

Clusteret består av 7 noder (glemmen130-180, glemmen80, glemmen60), alle fysiske maskiner.
De kjører [Talos Linux](https://www.talos.dev) v1.14 med Kubernetes 1.36 og alle nodene har alle rollene (control plane + worker, etcd).

Hardware på 1xx-nodene:
- Intel(R) Xeon(R) E-2314 CPU @ 2.80GHz
- 32 GB DDR4 RAM
- 2 TB HDD + 1 TB HDD (glemmen130: 2 stk 2 TB + 1 TB)

Hardwaren på glemmen80:
- Intel(R) Xeon(R) CPU E5-1650 v3 @ 3.50GHz
- 192 GB DDR4 RAM
- 2x 1 TB HDD (RAID1, systemdisk) + 1 TB HDD

Hardwaren på glemmen60:
- AMD Opteron(tm) 4386, 110 GB RAM
- 558 GB systemdisk + 7.3 TB datadisk

### Nettverk
1xx-nodene har to 1 Gbit/s-kort i LACP-bond (`bond0`, 802.3ad, layer3+4). glemmen80 og glemmen60 har ett kort hver. Alle nodene har statiske adresser i `192.168.6.0/24`.

- **Kubernetes API**: `192.168.6.98` (Talos Layer2 VIP, annonseres av control plane-nodene).
- **Ingress**: `192.168.6.99` (MetalLB L2-pool `.96-.99`). UniFi-routeren portvideresender 80/443 til denne. Traefik kjører i `kube-system` og terminerer TLS med cert-manager sine wildcard-sertifikater. Nye tjenester som skal ha egne porter på samme IP kan bruke MetalLB (sett `metallb.io/loadBalancerIPs: 192.168.6.99` og egen port).
- **Netbird** kjører på alle nodene som Talos system-extension, slik at de fortsatt er tilgjengelige på mesh-nettet (`*.blalange.intra`). Kubernetes-trafikken går på LAN-adressene.
- Nodene administreres med `talosctl` (se `talos/`), ikke SSH.

### Lagring
Longhorn (v1.12) leverer alle PVC-ene. Datadiskene er btrfs-volumer som Talos monterer:

| Node | Systemdisk | Longhorn-datadisker |
|------|------------|---------------------|
| 130 | sda | sdb (`/var/mnt/longhorn`) + sdc (`/var/mnt/hdd1`) + systemdisken |
| 150/160/170 | sda | sdb (`/var/mnt/longhorn`) + systemdisken |
| 180 | sda | systemdisken |
| 80 | RAID1 sda+sdb | sdc (`/var/mnt/hdd1`) + systemdisken |
| 60 | sda | sdb (`/var/mnt/hdd1`) + systemdisken |

Backup av Longhorn-volumene går til en Google Drive-mappe via en rclone S3-bro i clusteret (`flux/infrastructure/longhorn/rclone/`). Longhorn bruker backup target `s3://longhorn-backup@us-east-1/`; `rclone.conf` og S3-nøklene ligger i secret `rclone-backup-config` i `longhorn-system`.

## GitOps og konfigurasjon

- `flux/` – Flux v2 konfigurasjonen. `flux/setup.sh` bootstrapper Flux mot dette repositoriet, `flux/manage.sh` viser status og suspenderer/gjenopptar synken. Applikasjons-manifester ligger fortsatt i rot-katalogene (`frigate/`, `snipe-it/`, ...), Flux peker på dem.
- `talos/` – Talos maskinkonfigurasjon: `schematic.yaml` (Image Factory extensions), `nodes/` (per-node dokumenter), `patches/`, `gen-iso.sh`, `setup-images.sh` (per-node oppsetts-ISO-er med nettverksoppsett i bootloader), `gen-config.sh`, `manage.sh` og en runbook i `talos/README.md`. Genererte konfigurasjoner og secrets ligger i `talos/generated/` (ikke i git).
- `scripts/` – verktøy, inkludert `backup-old-cluster.sh` og `restore-to-new-cluster.sh` brukt under migreringen fra den gamle K3s-clusteret.

## Historikk

Clusteret ble migrert fra K3s (Ubuntu 24.04, Rancher + Fleet) til Talos Linux + Flux i oktober 2026.
Migreringsplanen og bakgrunnen er dokumentert i PR/commit-historikken; den gamle konfigurasjonen (fleet.yaml-filer) er fjernet.
