# PXE chainloader

This directory runs a small proxyDHCP/TFTP chainloader and a static Talos
boot-asset server. It exists for machines whose firmware cannot boot the
installed disk directly (for example, a Talos installation on a PCIe NVMe
drive that is not exposed as a boot device).

The chainloader does not serve machine configs. It boots the current Talos
kernel/initramfs; Talos then boots the installation it finds on disk, or enters
maintenance mode if the disk has no Talos installation.

## Boot flow

1. UniFi DHCP leases an address. It must **not** provide PXE options.
2. The `pxe-dnsmasq` pod answers the PXE options on `192.168.6.0/24`:
   - BIOS → `undionly.kpxe` over TFTP
   - UEFI → `ipxe.efi` over TFTP
3. iPXE asks for DHCP again and is served the generated `boot.ipxe`.
4. `boot.ipxe` matches the machine by SMBIOS serial, UUID or MAC:
   - known Talos node → current `vmlinuz` and `initramfs.xz` from
     `http://192.168.6.99:8080`;
   - installed Talos → the kernel boots the installed system;
   - no installation → Talos maintenance mode with the node's static
     maintenance address;
   - unknown machine → `http://boot.netboot.xyz`.
5. If an asset download fails, the same script falls back to netboot.xyz.

No `talos.config` URL is passed. Machine configs are applied manually with
`./scripts/talos/manage.py apply <node>` after a node comes up in maintenance
mode. This keeps the cluster PKI and machine tokens out of the boot server.

## Endpoints

| URL | Purpose |
| --- | --- |
| `http://192.168.6.99:8080/vmlinuz` | Current Talos kernel |
| `http://192.168.6.99:8080/initramfs.xz` | Current Talos initramfs |
| `http://192.168.6.99:8080/boot.ipxe` | Generated iPXE script (HTTP copy) |
| `http://192.168.6.99:8080/healthz` | Asset server health check |
| TFTP on every control-plane node | `undionly.kpxe`, `ipxe.efi`, `boot.ipxe` |

## UniFi configuration

On the `192.168.6.0/24` network:

- Leave UniFi as the DHCP address server.
- Remove/disable UniFi's PXE boot filename and next-server options. The
  in-cluster proxyDHCP is the only PXE authority; two option-67 sources would
  conflict.
- Keep the normal boot order disk-first. Select network boot one time when a
  node needs to be chainloaded.

The dnsmasq pod uses proxyDHCP only: it does not allocate addresses, routers or
DNS servers.

## Matching and adding a node

Machine identities live in [`talos/pxe.yaml`](../../../talos/pxe.yaml):

```yaml
nodes:
  glemmen130:
    serial: 6L51JZ3          # Dell service tag / IBM serial (SMBIOS)
    uuid: 4c4c4544-...       # talosctl get systeminformation
    macs:
      - 6c:3c:8c:75:3d:94    # permanentAddr of the cabled NICs
```

The generator renders [`boot.ipxe`](boot.ipxe) from that file and
`talos/nodes/*.yaml`:

```sh
./scripts/talos/gen-pxe.py
./scripts/talos/gen-pxe.py --check
```

To add a node, add its serial/UUID/MACs, add `talos/nodes/<host>.yaml`, run the
generator, and commit both files. Unknown machines automatically use
netboot.xyz.

## Talos version updates

The asset server resolves the schematic from `talos/schematic.yaml` and
downloads the kernel/initramfs for `TALOS_VERSION` from `talos/settings.env`
during pod startup. Updating either file changes a generated ConfigMap hash and
Flux rolls the asset pod, which downloads the new assets. No manual upload is
needed.

`talos/settings.env` also sets `PXE_ASSET_URL` (`http://192.168.6.99:8080`).

## Operations

```sh
kubectl -n pxe get pods -o wide
kubectl -n pxe logs deploy/pxe-dnsmasq
kubectl -n pxe logs deploy/pxe-assets -c fetch-assets
curl http://192.168.6.99:8080/healthz
curl -I http://192.168.6.99:8080/vmlinuz
curl -I http://192.168.6.99:8080/initramfs.xz
tftp <control-plane-ip> -c get ipxe.efi
tftp <control-plane-ip> -c get undionly.kpxe
tftp <control-plane-ip> -c get boot.ipxe
```

The dnsmasq logs show the DHCP exchange and which boot file was served. A
known node should request `boot.ipxe`; an unknown node should reach the
netboot.xyz menu after the script's fallback.

## Notes and limitations

- The dnsmasq pod is pinned to control-plane nodes and expects `bond0`. If it
  reschedules, it moves to another control plane; there is a short PXE outage
  during the move.
- The service is part of the cluster. If the cluster is down, neither the PXE
  server nor the asset server is available; keep the setup ISOs as
  disaster-recovery media.
- The dnsmasq image's bundled iPXE binaries are old; the pod's init container
  refreshes `ipxe.efi` and `undionly.kpxe` from `boot.ipxe.org` at startup and
  falls back to the bundled copies if the download fails.
- The chainloaded iPXE binaries are unsigned. Secure Boot must be disabled
  (this is already required for glemmen80).
- glemmen60's Broadcom BCM5716 may not work with `undionly.kpxe`. If it fails,
  add a native-driver `ipxe.pxe` to the TFTP root and use it for BIOS clients.
- The 1xx nodes are cabled to LACP LAG ports. The firmware PXE phase runs before
  Talos creates `bond0`, and iPXE does not implement 802.3ad. If the switch does
  not forward single-link PXE traffic on a LAG member, use a non-LAG
  provisioning port for the initial boot or keep using the setup ISO. Test one
  1xx node before relying on PXE there.
- The asset server always serves the current Talos version. Verify that a
  network-booted newer kernel still hands off correctly to an older installed
  Talos version before relying on it across upgrades.
- Netboot.xyz and Image Factory are external dependencies during a boot. The
  Talos assets are cached locally in the pod; the netboot.xyz menu itself is
  fetched from the internet.
