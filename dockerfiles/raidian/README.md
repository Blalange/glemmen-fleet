# RAIDian

RAIDian is a Debian 12 (bookworm) toolbox for inspecting and managing hardware
RAID controllers, the disks behind them and Linux software RAID. It is built
for immutable hosts such as Talos, where the vendor utilities cannot be
installed directly.

The image is published to GHCR:

```text
ghcr.io/blalange/raidian:latest
```

## Included tools

| Tool | Version | Source |
|---|---|---|
| MegaCLI (`megacli`, `MegaCli64`) | 8.07.14 | [hwraid Debian repository](https://hwraid.le-vert.net/debian/) |
| PERCCLI (`perccli64`) | 7.3208.0.0 | [Dell](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=5m4hp) |
| StorCLI (`storcli64`) | 007.3703.0000.0000 (SAS3.5 P38) | [Broadcom](https://www.broadcom.com/support/download-search) |
| Linux software RAID | `mdadm` | Debian |
| Disk health | `smartctl` (`smartmontools`) | Debian |
| NVMe | `nvme` (`nvme-cli`) | Debian |
| SCSI/SAS | `sg3_utils`, `lsscsi`, `sdparm` | Debian |
| Enclosure LEDs | `ledmon` | Debian |
| Hardware inventory | `dmidecode`, `pciutils`, `usbutils`, `ipmitool` | Debian |
| Disk utilities | `parted`, `hdparm`, `file`, `procps` | Debian |

The vendor utilities run as root and need access to the controller devices.
The image is `linux/amd64` only because MegaCLI and PERCCLI are published for
x86-64. The vendor binaries are proprietary and subject to their own licenses.

## Build and push

```sh
docker login ghcr.io
./scripts/raidian/build-image.sh

# default: ghcr.io/blalange/raidian:latest
# override the tag or registry:
IMAGE=ghcr.io/blalange/raidian:2026-11-26 ./scripts/raidian/build-image.sh
```

The build downloads the pinned vendor packages and verifies each archive
against the SHA-256 checksum in `Dockerfile` before installing it. To upgrade a
tool, update its URL, version and checksum together, rebuild, and run the smoke
tests in the Dockerfile.

GHCR packages are private by default. Make the package public if anonymous
pulls are wanted, or create an image pull secret for workloads that use it.

## Run

The vendor CLIs need privileged access to the RAID controller's ioctl and block
devices. Use `--privileged` (or an equivalent set of device and capability
mounts):

```sh
docker run --rm -it --privileged ghcr.io/blalange/raidian:latest
```

Examples:

```sh
# Broadcom/LSI MegaRAID
storcli64 /c0 show
storcli64 /c0 /eall /sall show

# Dell PERC
perccli64 /c0 show

# Legacy LSI MegaRAID
megacli -AdpAllInfo -aALL
megacli -PDList -aALL

# Linux software RAID and disks
mdadm --detail --scan
cat /proc/mdstat
smartctl --scan
smartctl -a /dev/sda
nvme list
lsscsi -s
```

Because the image has no long-running entrypoint, it can also be used as a
debug container:

```sh
kubectl debug node/glemmen130 -it --image=ghcr.io/blalange/raidian:latest
```

## Files

| Path | Purpose |
|---|---|
| `Dockerfile` | Debian base, package pins, checksums and smoke tests |
| `scripts/raidian/build-image.sh` | Builds and pushes the GHCR image |
