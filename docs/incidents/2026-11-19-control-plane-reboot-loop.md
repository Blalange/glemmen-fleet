# Incident postmortem: control-plane reboot loop and full cluster outage

**Incident date:** 2026-11-18 / 2026-11-19  
**Report date:** 2026-11-19  
**Severity:** SEV-1 (full Kubernetes API and Ceph data-plane outage)  
**Status:** Resolved; permanent image fix still pending  
**Affected systems:** `glemmen130`, `glemmen150`, `glemmen160`, `glemmen170`, `glemmen180` (Talos control planes and Ceph OSD hosts), Kubernetes API, etcd, Ceph, all workloads that depend on the API or Ceph

---

## Executive summary

The five control-plane nodes entered a **graceful reboot loop every ~72 minutes**. The reboot was not a hardware fault and not a power or network event:

- The global Talos image contained the NVIDIA GPU system extensions (`nvidia-open-gpu-kernel-modules-production` and `nvidia-container-toolkit-production`).
- On nodes without a GPU, the extension services `ext-nvidia-persistenced` and `ext-nvidia-cdi-gen` waited indefinitely for `/sys/bus/pci/drivers/nvidia`, which never appears on a non-GPU host.
- The Talos boot sequence waits for those services, eventually failed with `context deadline exceeded` (about 72 minutes after boot), and Talos rebooted the node.
- The reboot loop took out the in-cluster PXE server (`pxe-dnsmasq`), which was pinned to control-plane nodes.
- The Dell R250 firmware has no valid local boot entry for the Talos NVMe installation; all disk boot options are `Unavailable` and PXE is the only working path. With the PXE server down, the control planes could not boot at all.

The first reboot loop started on 2026-11-18 at 21:53 UTC, immediately after the control planes were booted from the new image. By 2026-11-19 02:42 UTC all five control planes had booted at least once since the loop began, but each was stuck in the NVIDIA boot-sequence wait; none reached `running`. The in-cluster PXE server was on a control-plane node and was no longer available, so any node that timed out and rebooted could not PXE-boot. The cluster was restored between 11:06 and 11:55 UTC after using iDRAC one-time PXE boot and a temporary PXE server on a worker node.

There is **no evidence of data loss**: Ceph reported 12/12 OSDs up at the end of recovery, no unfound objects, and only the expected `aes` CephX warnings. The permanent fix (splitting the base image from the GPU image) has not yet been applied; the immediate mitigation is runtime-only.

---

## Impact

- Kubernetes API (`192.168.6.98:6443`), etcd, kube-scheduler and kube-controller-manager were unavailable for approximately **8.5 hours** (02:42–11:18 UTC).
- Ceph lost all OSDs on the control planes; only the single NVMe OSD on `glemmen250` remained. Ceph was effectively unavailable to workloads.
- All workloads that require the Kubernetes API, Ceph RBD/CephFS, or the missing control-plane services were unavailable: YugabyteDB, Grafana/Prometheus, media applications, NetBox, WebODM, etc.
- Worker nodes (`glemmen50`, `glemmen60`, `glemmen80`, `glemmen250`) stayed up. Their Talos APIs and kubelets remained reachable, but they could not form a usable cluster without the control planes.
- No Ceph data was lost. During recovery Ceph briefly reported degraded PGs and a small number of unfound objects while OSDs returned, but the final state was clean: 12 OSDs up/in, no unfound objects.

---

## Evidence sources

- **Talos logs:** `/var/log/kernel.log` from the control planes (persisted on the EPHEMERAL volume across reboots), `talosctl services`, `talosctl dmesg`, `talosctl get machinestatus`.
- **iDRAC:** Redfish `Managers/iDRAC.Embedded.1/LogServices/Lclog/Entries` and `Systems/System.Embedded.1` (power state, boot options, last reset time).
- **Metrics:** Thanos `node_boot_time_seconds`, `up`, `sum(ceph_osd_up)`, `ceph_health_status`, and `unpoller_device_uptime_seconds`.
- **UniFi:** the controller API at `https://unifi.glem.men`; the event endpoint `/proxy/network/api/s/default/stat/event` returned `api.err.NotFound` for the poller account, so the `unpoller` metrics were used for the network evidence.
- **Kubernetes/Flux:** node status, Flux Kustomizations, PXE pod placement, Ceph status after recovery.

---

## Timeline (UTC)

Times are taken from `node_boot_time_seconds` in Thanos, Talos `kernel.log`, iDRAC Lifecycle logs and Kubernetes/PXE observations. The iDRAC Lifecycle timestamps are labelled `-06:00` but their clock values match UTC; see “iDRAC timezone note” below.

| Time | Event |
|---|---|
| 2026-11-16 09:02 | Commit `1878aa9` adds the NVIDIA extensions to the global `talos/schematic.yaml` for the new glemmen250 GPU worker. |
| 2026-11-18 20:39–20:41 | `glemmen180` boots from the new image (boot 20:41:46; no prior NVIDIA-extension loop was observed on its previous boot). |
| 2026-11-18 20:55 | `glemmen160` boots from the new image (20:55:38). |
| 2026-11-18 21:53 | First loop reboot: `glemmen180` reboots ~72 minutes after boot. |
| 2026-11-18 22:07 / 22:08 | `glemmen160` and `glemmen170` reboot. |
| 2026-11-18 22:29 | `glemmen150` reboots. |
| 2026-11-18 22:45 | `glemmen130` reboots. |
| 2026-11-18 23:05–23:46 | Second round of reboots: 180 (23:05), 160 (23:19), 170 (23:20), 150 (23:46). |
| 2026-11-19 00:02–00:58 | Third round: 130 (00:02), 180 (00:17), 160/170 (00:37), 150 (00:58). |
| 2026-11-19 01:19–02:11 | Fourth round: 130 (01:19), 180 (01:29), 160 (01:54), 170 (01:55), 150 (02:11). |
| 2026-11-19 02:37–02:42 | Last successful boots: 130 (02:37), 180 (02:42). All five control planes were now in the NVIDIA boot-sequence wait; the PXE server was on a control plane and was down, so subsequent reboots could not PXE. |
| 2026-11-19 02:42–11:00 | Full outage: no Kubernetes API, no etcd quorum, Ceph OSDs down. Worker nodes and the UniFi network remained up. |
| 2026-11-19 10:52 | Outage detected: API VIP no route; all control-plane Talos API ports closed; workers still reachable. |
| 2026-11-19 11:06–11:38 | Recovery: temporary PXE server on `glemmen60`; iDRAC one-time PXE boot; control planes 150, 130, 170, 160 and 180 booted. |
| 2026-11-19 11:18 | Kubernetes API reachable again on the individual control-plane addresses; etcd quorum restored. |
| 2026-11-19 11:22–11:38 | `glemmen180` drained and rebooted to clear the rbd/OSD activation deadlock. |
| 2026-11-19 11:45–11:55 | Ceph returned to 12/12 OSDs up/in. NVIDIA services stopped on all non-GPU control planes; boot sequences reached `running`. |
| 2026-11-19 12:00+ | Flux and workloads recovered; Ceph rebalancing continued. |

---

## Detection

The outage was noticed manually at 10:52 UTC when normal `kubectl` commands failed:

```text
Unable to connect to the server: dial tcp 192.168.6.98:6443: connect: no route to host
```

Initial checks showed:

- All nine nodes answered ICMP.
- Workers `glemmen50/60/80/250` had TCP 50000 (Talos API) and 10250 (kubelet) open.
- All five control planes had TCP 50000, 6443, 2379 and 2380 closed.
- iDRAC reported all five control planes powered on and healthy.
- `glemmen130` had `LastResetTime` at 02:31, `glemmen170` at 01:49, `glemmen160` at 03:06, `glemmen180` at 02:41 and `glemmen150` at 03:22 (UTC values; iDRAC label issue below).

The pattern — powered-on hosts, healthy BMCs, open ICMP, no Talos or Kubernetes TCP services — pointed to hosts stuck before userspace, not a network or power outage.

---

## Root cause

### 1. NVIDIA extension services on non-GPU nodes

`talos/schematic.yaml` adds these system extensions to **every node**:

```yaml
- siderolabs/nvidia-open-gpu-kernel-modules-production
- siderolabs/nvidia-container-toolkit-production
```

They were added for `glemmen250`, the only GPU node. The image is shared by all nodes, so the control planes also received the NVIDIA extension services.

On a non-GPU host, `talosctl services` showed:

```text
ext-nvidia-cdi-gen        Waiting   Waiting for service "ext-nvidia-persistenced" to be "up"
ext-nvidia-persistenced   Waiting   Waiting for file "/sys/bus/pci/drivers/nvidia" to exist
```

Manual loading of the module confirmed why the path never appears:

```text
nvidia-nvlink: Nvlink Core is being initialized, major device number 236
NVRM: No NVIDIA GPU found.
nvidia-nvlink: Unregistered Nvlink Core, major device number 236
```

### 2. Talos boot sequence timeout

The Talos `kernel.log` from `glemmen180` shows the end of the boot sequence before the reboot at 01:29:56:

```text
2026-11-19T01:28:07.998927505Z [talos] task startAllServices (1/1): service "ext-nvidia-cdi-gen" to be up/finished, service "ext-nvidia-persistenced" to be up/finished
2026-11-19T01:28:07.998978964Z [talos] task startAllServices (1/1): failed: 2 errors occurred:
2026-11-19T01:28:07.998996211Z  * context deadline exceeded
2026-11-19T01:28:07.998996211Z  * context deadline exceeded
2026-11-19T01:28:07.999026053Z [talos] phase startEverything (9/9): failed
2026-11-19T01:28:07.999037136Z [talos] boot sequence: failed
```

Immediately afterwards Talos stopped `kubelet`, `etcd`, `apid`, `trustd`, containerd and the other services, unmounted pod volumes and rebooted. This was a **graceful software-initiated reboot**, not a panic, watchdog or power event. The iDRAC Lifecycle log for the same reboot says:

```text
2026-11-19T01:28:43-06:00  System CPU Resetting.
2026-11-19T01:29:17-06:00  The server power action is initiated because the host device initiated a warm-reset operation.
```

The interval from boot to boot-sequence failure was consistently about 71–72 minutes, which explains the regular reboot cadence.

### 3. Why only the control planes

- `glemmen50`, `glemmen60` and `glemmen80` run an older image without the NVIDIA extension. Their `talosctl services` output has no `ext-nvidia-*` services, and they stayed up for days.
- `glemmen250` has a real GPU, so the NVIDIA driver path and services work there.
- `glemmen130/150/160/170/180` were reinstalled/booted from the new global image during the NVMe/OSD work on 2026-11-18. Their boot times line up exactly with the start of the loop.

### 4. PXE deadlock

The Dell R250 firmware does not expose the Talos NVMe installation as a bootable local device. Redfish `BootOptions` for `glemmen130` showed all disk entries as `Unavailable`:

```text
Boot0000: PXE Device 1: Embedded NIC 1 Port 1 Partition 1
Boot0001: Unavailable: Ubuntu
Boot0002: Unavailable: Windows Boot Manager
Boot0003: Unavailable: Ubuntu
Boot0004: Unavailable: Fedora
```

The `pxe-dnsmasq` Deployment is pinned to control-plane nodes:

```yaml
nodeSelector:
  node-role.kubernetes.io/control-plane: ""
```

It was running on `glemmen130`/`glemmen150` during the incident. As soon as the control planes started rebooting, the only proxyDHCP/PXE server disappeared. The nodes had no local boot path, so the next reboot left them stuck in firmware/iPXE.

This is the chain that turned a per-node boot failure into a full outage:

```text
bad image service wait
  -> nodes never reach Talos "running"
  -> Talos boot sequence timeout after ~72 minutes
  -> graceful reboot
  -> pxe-dnsmasq (on a control plane) goes down with the first reboot
  -> no PXE for the next control plane
  -> all control planes stuck in the boot wait or at PXE
  -> no etcd quorum
  -> no Kubernetes API, no Ceph
```

---

## Metrics evidence

### Node boot times

`node_boot_time_seconds` from Thanos shows the reboot loop directly. Each entry is a new boot:

| Node | Boot times (UTC) |
|---|---|
| glemmen130 | 2026-11-18 22:45:56; 2026-11-19 00:02:46; 01:19:36; 02:37:09; 11:14:04 |
| glemmen150 | 2026-11-18 22:29:00; 23:46:55; 2026-11-19 00:58:54; 02:11:01; 11:09:29 |
| glemmen160 | 2026-11-18 20:55:38; 22:07:27; 23:19:25; 2026-11-19 00:37:29; 01:54:35; 11:21:31 |
| glemmen170 | 2026-11-18 22:08:29; 23:20:27; 2026-11-19 00:37:38; 01:55:40; 11:16:59 |
| glemmen180 | 2026-11-18 20:41:46; 21:53:40; 23:05:44; 2026-11-19 00:17:52; 01:29:56; 02:42:02; 11:38:23 |

The last entries (11:xx) are the successful recovery boots. The earlier entries show the 72-minute loop. The old boot-time series for these nodes (for example glemmen130 at 2026-11-17 07:51:50, glemmen160 at 2026-11-10 09:08:37 and glemmen180 at 2026-11-14 17:44:03) confirm they had been stable for days before the new image was booted.

### Ceph OSD availability

`sum(ceph_osd_up)` dropped from 12 to 8–10 each time a control plane rebooted, then recovered to 12 while the node was briefly back:

| Time (UTC) | OSDs up |
|---|---|
| 2026-11-18 20:00 | 4 (initial OSD roll-out in progress) |
| 2026-11-18 20:35 | 7 |
| 2026-11-18 21:05 | 9 |
| 2026-11-18 22:20 | 10 |
| 2026-11-18 22:55 | 12 |
| 2026-11-18 23:05 | 10 |
| 2026-11-18 23:20 | 8 |
| 2026-11-18 23:25 | 12 |
| 2026-11-19 00:00 | 9 |
| 2026-11-19 00:40 | 12 |
| 2026-11-19 01:15 | 9 |
| 2026-11-19 01:25 | 12 |
| 2026-11-19 01:50 | 8 |
| 2026-11-19 02:00 | 12 |
| 2026-11-19 02:35 | 9 |
| 2026-11-19 02:40 | 12 |
| 2026-11-19 03:10 | 10 |
| 2026-11-19 03:10–11:45 | no Thanos data (Thanos query/store gateway were on the control planes) |
| 2026-11-19 11:45 | 12 |

The gap after 03:10 is a **monitoring gap**, not an absence of Ceph state: Thanos Query/Store Gateway ran on the control planes and could not serve data while the cluster API and control planes were down.

### UniFi network

The UniFi controller/switch did not reboot. `unpoller_device_uptime_seconds` increased monotonically through the entire incident:

| Device | Uptime at 2026-11-18 20:00 | Uptime at 2026-11-19 11:50 | Increase |
|---|---:|---:|---:|
| USW Pro 24 PoE | 6,087,865 s | 6,144,885 s | 57,020 s |
| Dream Machine Pro B bygg | 640,806 s | 697,807 s | 57,001 s |

The increase matches the wall-clock interval (~15 h 50 m), so neither the switch nor the UDM restarted. This rules out a switching/uplink outage as the cause of the node failures.

The UniFi event endpoint ` /proxy/network/api/s/default/stat/event` returned `api.err.NotFound` for the `unifipoller` account, so a detailed controller event stream was not available. The poller metrics above were used instead. The controller API is reachable at `https://unifi.glem.men` and the poller account has `network.management: admin`, but the events endpoint is not exposed by this Network application version.

---

## Recovery

### 1. Break the PXE deadlock

Because the control planes were at firmware/iPXE with no Talos API, recovery required out-of-band access:

- Used the iDRAC admin account (`idrac`) to set a one-time PXE boot override and issue `ForceRestart`.
- Started a temporary proxyDHCP/TFTP/HTTP server on `glemmen60` (a worker that was still up).
- Suspended the `pxe` Flux Kustomization and scaled the in-cluster `pxe-dnsmasq` to 0 while the temporary server was in use, to avoid two proxyDHCP responders.

The temporary dnsmasq initially only handled `client-arch=9` (x86-64 EFI). The R250 firmware reported:

```text
PXEClient:Arch:00007:UNDI:003001
```

so it matched neither `efi64` nor a boot filename. The first reset produced a DHCP response but no TFTP boot file. The fix was to serve the full architecture set from the repository's `dnsmasq.conf`:

```text
bios   (arch 0)  -> undionly.kpxe
efi32  (arch 6)  -> ipxe.efi
efibc  (arch 7)  -> ipxe.efi
efi64  (arch 9)  -> ipxe.efi
iPXE user class  -> boot.ipxe
```

After that, the nodes fetched `ipxe.efi`, `boot.ipxe`, `vmlinuz` and `initramfs.xz` and booted the installed Talos system.

### 2. Restore etcd quorum

`glemmen150`, `glemmen130` and `glemmen170` were booted first, giving three of five etcd members. The Kubernetes API was then reachable on individual control-plane addresses. Normal access through the VIP still failed until the VIP moved, so recovery used:

```sh
kubectl --server=https://192.168.6.150:6443 --insecure-skip-tls-verify ...
```

The remaining control planes `glemmen160` and `glemmen180` were booted afterwards.

### 3. Ceph OSD recovery and the glemmen180 rbd deadlock

Ceph OSDs on the control planes came back as the nodes booted, but `glemmen180`’s OSDs 5 and 7 were stuck in the init container:

```text
rook-ceph-osd-5-...   0/2  Init:0/4
rook-ceph-osd-7-...   0/2  Init:0/4
```

The `activate` init container was blocked in `ceph-volume raw activate`:

```text
+ ceph-volume raw activate --device /dev/sda --no-systemd --no-tmpfs
Running command: /usr/bin/ceph-authtool --gen-print-key
```

The corresponding processes were in uninterruptible sleep (`D` state). `glemmen180` had CSI RBD devices `rbd0`/`rbd1` mapped for pods that were using PVCs. Those RBD images were backed by the same Ceph cluster, including the OSDs that were down, so opening them during OSD activation deadlocked.

The recovery was:

1. Drain `glemmen180` so the CSI volumes were unmounted:
   ```sh
   kubectl drain glemmen180 --ignore-daemonsets --delete-emptydir-data --force
   ```
2. The `rbd0` mapping remained with `inflight=1`; the init process was gone but the kernel I/O was still stuck.
3. A reboot with the node kept cordoned cleared the stale mappings; no new RBD devices were mapped because no non-OSD pods were assigned to the node.
4. Uncordoned the node; OSD 5 and 7 activated and both reached `2/2 Running`.

Ceph then reported 12/12 OSDs up/in. The temporary unfound objects seen while OSDs were returning resolved as the OSDs came back; the final state had no unfound objects.

### 4. Stop the NVIDIA services and restore PXE

On each non-GPU control plane:

```sh
talosctl -n <node> service ext-nvidia-cdi-gen stop
talosctl -n <node> service ext-nvidia-persistenced stop
```

The services transitioned to `Finished` and the Talos machine status changed from `booting` to `running`. This prevented the pending 72-minute timeout on all five nodes.

Then:

- Stopped the temporary PXE server on `glemmen60`.
- Resumed the `pxe` Kustomization and scaled `pxe-dnsmasq` back to 1.
- Verified the in-cluster PXE asset VIP `192.168.6.99:8080` from the server VLAN.
- Verified the API VIP `192.168.6.98:6443` and all Flux Kustomizations.

---

## Verification

Final state:

- All 9 nodes `Ready`.
- All Flux Kustomizations `Ready`.
- Ceph: `12 osds: 12 up, 12 in`; no degraded PGs or unfound objects at the end of recovery.
- Ceph health: only the expected `AUTH_INSECURE_*` warnings for the `aes` CephX keys used by the CSI clients.
- `pxe-assets` and `pxe-dnsmasq` pods running.
- The temporary PXE server on `glemmen60` is stopped.
- No repository changes were required for the runtime recovery; the working tree was clean after the recovery.

Ceph rebalancing was still in progress after recovery (~23% misplaced PGs), with recovery running at ~17–80 MiB/s. That is normal after bringing 12 OSDs back and was not related to the outage.

---

## Issues encountered during recovery

| Issue | Detail | Resolution |
|---|---|---|
| PXE architecture mismatch | First temporary dnsmasq handled only `client-arch=9`; R250 firmware reported `PXEClient:Arch:00007:UNDI:003001` | Added `bios`, `efi32`, `efibc` and `efi64` tags from the repository config |
| Two proxyDHCP responders | In-cluster `pxe-dnsmasq` and temporary dnsmasq would conflict | Suspended the `pxe` Kustomization and scaled `pxe-dnsmasq` to 0 while booting |
| MetalLB VIP false negative | `ping 192.168.6.99` failed because ICMP is not DNATed to a MetalLB service; `curl` from the server VLAN worked | Used TCP/HTTP checks from a server-VLAN node, not ICMP |
| API VIP down | `192.168.6.98:6443` was unavailable until etcd quorum returned | Used `https://192.168.6.150:6443` directly |
| iDRAC privilege | The `readonly` exporter account could not power-cycle or change boot options | Used the iDRAC admin account |
| Stuck rbd device | `ceph-volume` blocked in `D` state on `/dev/rbd0`; drain could not release it | Rebooted the drained/cordoned node with no CSI volumes mapped |
| Kernel module test | Adding `KernelModuleConfig nvidia` on a non-GPU node caused repeated `NVRM: No NVIDIA GPU found` retries | Removed the test patch; the module cannot load without a GPU |
| iDRAC clock label | Lifecycle log timestamps say `-06:00` but the clock values align with UTC | Treat the numeric values as UTC; fix the iDRAC timezone separately |

---

## Prevention and follow-up actions

### P0 — must be done before the next reboot of a control plane

1. **Split the image into base and GPU variants.**
   - Remove the two NVIDIA extensions from the base `talos/schematic.yaml`.
   - Add a separate `talos/schematic-gpu.yaml` for `glemmen250`.
   - Add per-node schematic selection to `gen-config.py`, `gen-iso.py`, `setup-images.py` and the PXE asset generation.
   - Acceptance: `talosctl services` on every non-GPU node has no `ext-nvidia-*` services; glemmen250 still has working NVIDIA drivers and GPU Operator.

2. **Move the PXE server off the control planes.**
   - Pin `pxe-dnsmasq` and `pxe-assets` to a stable worker (for example `glemmen60` or `glemmen80`), or run the proxyDHCP/TFTP service outside Kubernetes.
   - Keep the asset server and proxyDHCP on the same L2 network as the nodes.
   - Add a documented manual fallback using iDRAC virtual media or the setup ISOs.
   - Acceptance: losing all control planes does not remove the ability to PXE boot a control plane.

### P1 — detection and resilience

3. **Alert on reboot loops and boot stalls.**
   - `changes(node_boot_time_seconds[2h]) > 1` for any node.
   - `time() - node_boot_time_seconds < 600` repeated (node just rebooted).
   - Node `Ready` false for > 5 minutes.
   - `sum(ceph_osd_up) < 12`.
   - A synthetic external probe of `https://192.168.6.98:6443/healthz` and the PXE asset VIP.
   - Alertmanager currently runs on the control planes; add an external receiver or a dead-man’s-switch outside the cluster.

4. **Add a boot-timeout guard.**
   - A small worker-side DaemonSet/CronJob or external script should check Talos `machinestatus` and `talosctl services`; if a known extension service is waiting for hardware for more than 15 minutes, stop it and alert.
   - This is a safety net, not a replacement for the image split.

5. **Store break-glass access properly.**
   - Keep the iDRAC admin credentials in the password manager/break-glass store, not only the read-only exporter account.
   - Document the iDRAC one-time PXE override and `ForceRestart` procedure in the Talos runbook.

### P2 — process and validation

6. **Test image/schematic changes on one non-GPU node first.**
   - Boot the new image on a single control plane, wait at least 90 minutes, and verify `machinestatus` reaches `running` and no extension service is waiting for hardware.
   - Only then roll the rest of the fleet.

7. **Document the R250 PXE dependency.**
   - Record that the R250 firmware cannot boot the Talos NVMe installation and that PXE is mandatory for the 1xx nodes.
   - Keep the per-node setup ISOs and the temporary dnsmasq configuration in the runbook.
   - Consider adding a supported local boot entry if a future BIOS/firmware update makes the NVMe bootable.

8. **Fix iDRAC time configuration.**
   - The Lifecycle log timezone is misleading. Set the iDRAC timezone to UTC (or correct local time) so incidents correlate cleanly with Kubernetes and Thanos.

9. **Improve UniFi event collection.**
   - The `unifipoller` account cannot read `/stat/event` on this controller version. Send UniFi controller syslog/events to Loki, or use an admin account with event permissions, so switch port and device events are available during an incident.

10. **Review the disabled Tuppr upgrade path.**
    - Tuppr is intentionally disabled (`a8d0f3f disable tuppr`) and was not the cause of this incident. When re-enabled, ensure the image-split and pre-flight checks are in place before it can roll a new schematic to the cluster.

---

## What went well

- The iDRACs were reachable and the admin account could force PXE boot and power-cycle the hosts.
- Talos persisted `kernel.log` across reboots, which made the boot-sequence failure and the graceful shutdown sequence visible.
- `node_boot_time_seconds` and `sum(ceph_osd_up)` in Thanos provided an independent, precise timeline.
- The worker nodes and the UniFi network stayed up, so the temporary PXE server could run on `glemmen60`.
- etcd quorum was restored after three control planes returned; Ceph recovered without data loss.

## What could have gone better

- A global image change for one GPU node was rolled to all nodes without a non-GPU validation.
- The PXE server was allowed to run on the same nodes it needs to recover.
- Monitoring and Alertmanager were hosted on the control planes, so the failure removed the alerting path.
- The runbook did not include the dnsmasq architecture tags or the iDRAC recovery procedure.
- MetalLB VIPs were initially checked with `ping`, which is not a valid service health check.

---

## Appendix A — Useful queries

Thanos queries used for this report (Thanos Query at `thanos-query:9090`):

```promql
node_boot_time_seconds{instance=~"192.168.6.(130|150|160|170|180):9100"}
up{job=~".*node-exporter.*",instance=~"192.168.6.(130|150|160|170|180):9100"}
sum(ceph_osd_up)
ceph_health_status
unpoller_device_uptime_seconds{name="USW Pro 24 PoE"}
unpoller_device_uptime_seconds{name="Dream Machine Pro B bygg"}
```

Ceph and node checks:

```sh
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph -s
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd tree
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health detail
talosctl -n <node> services | grep ext-nvidia
talosctl -n <node> get machinestatus
```

## Appendix B — Temporary PXE server used during recovery

The temporary server ran on `glemmen60` and served the existing Talos assets from the `pxe-assets` pod. The dnsmasq configuration used the full architecture set:

```text
port=0
bind-dynamic
dhcp-range=192.168.6.0,proxy,255.255.255.0
enable-tftp
tftp-root=/tmp/tftpboot
dhcp-match=set:bios,option:client-arch,0
dhcp-vendorclass=set:bios,PXEClient:Arch:00000
dhcp-boot=tag:bios,undionly.kpxe
pxe-service=tag:bios,x86PC,"Talos PXE boot",undionly.kpxe
dhcp-match=set:efi32,option:client-arch,6
dhcp-vendorclass=set:efi32,PXEClient:Arch:00006
dhcp-boot=tag:efi32,ipxe.efi
pxe-service=tag:efi32,IA32_EFI,"Talos PXE boot",ipxe.efi
dhcp-match=set:efibc,option:client-arch,7
dhcp-vendorclass=set:efibc,PXEClient:Arch:00007
dhcp-boot=tag:efibc,ipxe.efi
pxe-service=tag:efibc,BC_EFI,"Talos PXE boot",ipxe.efi
dhcp-match=set:efi64,option:client-arch,9
dhcp-vendorclass=set:efi64,PXEClient:Arch:00009
dhcp-boot=tag:efi64,ipxe.efi
pxe-service=tag:efi64,X86-64_EFI,"Talos PXE boot",ipxe.efi
dhcp-userclass=set:ipxe,iPXE
dhcp-boot=tag:ipxe,boot.ipxe
log-dhcp
log-facility=-
```

The boot script and kernel/initramfs were fetched from the `pxe-assets` pod and served from `192.168.6.60:8080` after replacing the `192.168.6.99` asset URL in `boot.ipxe`.

## Appendix C — Key log excerpts

Talos boot sequence failure (`glemmen180`, previous boot):

```text
[talos] task startAllServices (1/1): service "ext-nvidia-cdi-gen" to be up/finished, service "ext-nvidia-persistenced" to be up/finished
[talos] task startAllServices (1/1): failed: 2 errors occurred:
 * context deadline exceeded
 * context deadline exceeded
[talos] phase startEverything (9/9): failed
[talos] boot sequence: failed
```

NVIDIA service state on a non-GPU node before mitigation:

```text
ext-nvidia-cdi-gen        Waiting   Waiting for service "ext-nvidia-persistenced" to be "up"
ext-nvidia-persistenced   Waiting   Waiting for file "/sys/bus/pci/drivers/nvidia" to exist
```

Kernel module failure on the non-GPU node during the mitigation test:

```text
nvidia-nvlink: Nvlink Core is being initialized, major device number 236
NVRM: No NVIDIA GPU found.
nvidia-nvlink: Unregistered Nvlink Core, major device number 236
```

Ceph OSD init deadlock (`glemmen180`):

```text
+ ceph-volume raw activate --device /dev/sda --no-systemd --no-tmpfs
Running command: /usr/bin/ceph-authtool --gen-print-key
```

Process state:

```text
/usr/bin/python3 -s /usr/sbin/ceph-volume raw activate --device /dev/sda --no-systemd --no-tmpfs   D
```

`/sys/class/block/rbd0/inflight`:

```text
       1        0
```

iDRAC Lifecycle log (`glemmen180`, normalized to UTC by clock value):

```text
2026-11-18 21:52:24  System CPU Resetting.
2026-11-18 21:52:56  host device initiated a warm-reset operation
2026-11-18 23:04:32  System CPU Resetting.
2026-11-18 23:05:06  host device initiated a warm-reset operation
2026-11-19 00:16:36  System CPU Resetting.
2026-11-19 00:17:08  host device initiated a warm-reset operation
2026-11-19 01:28:43  System CPU Resetting.
2026-11-19 01:29:17  host device initiated a warm-reset operation
2026-11-19 02:40:49  System CPU Resetting.
2026-11-19 02:41:23  host device initiated a warm-reset operation
```

The iDRAC log labels these as `-06:00`; the clock values match the UTC boot times from Thanos. This is a separate iDRAC timezone configuration bug.

### iDRAC timezone note

All Lifecycle log entries report an offset of `-06:00`, but the clock values align with the UTC boot times from Thanos. For example, the recovery reset of `glemmen150` was logged at `11:06:24` while the current UTC time was 11:06, and Thanos recorded the resulting boot at `11:09:29Z`. The iDRAC timezone should be corrected to UTC so future incident correlations do not need this adjustment.

---

## Appendix D — Files and commits involved

- `talos/schematic.yaml` — global NVIDIA extensions; root cause.
- `talos/nodes/glemmen250.yaml` — `KernelModuleConfig` for the GPU modules; correct for the GPU node.
- `flux/infrastructure/pxe/dnsmasq.yaml` — PXE server pinned to control-plane nodes.
- `flux/infrastructure/pxe/assets.yaml` — PXE asset server on `glemmen60`.
- `1878aa9` — add glemmen250 GPU worker and NVIDIA GPU Operator.
- `a8d0f3f` — disable tuppr (Tuppr was not running during the incident).
- `8991a31` — last Git revision before the incident (NVMe OSD documentation).

No permanent configuration change was committed as part of the incident recovery; the stop of the NVIDIA services is a runtime mitigation. The image split and PXE relocation should be tracked as follow-up work before the next control-plane reboot.
