# Cisco Nexus 3048TP-1GE

Glemmen server/access switch. It replaces the Zyxel GS1920-24 and becomes the
main switch for the Talos servers and all iDRAC/IMM management ports. The
UniFi USW Pro 24 PoE remains uplinked to UDM Pro port 1; SFP4 is not used.

## As built

Configured and saved to startup on 2026-11-23 with NX-OS `7.0(3)I7(4)`.

- `mgmt0` is reachable at `192.168.4.2` over SSH; UDM port 8 is VLAN 4 access.
- `Ethernet1/1–20` are up in VLAN 4 where cabled. Ports 7, 17, 19 and 20 were
  not connected at the time of the build.
- SFP3 (`Ethernet1/51`) is up at 10G to UDM port 10; tagged VLAN 4 reaches the
  iDRACs through the UDM.
- SFP1 (`Ethernet1/49`) is up at 10G to glemmen80. SFP2 is empty. SFP4 has a
  DAC but is administratively shut; the USW remains on UDM port 1.
- `port-channel10`–`14` are up with glemmen130/150/160's six LACP members
  and glemmen170/180's two members.
- PSU 1 reports failed/absent; PSU 2 is OK, so the switch is running
  non-redundant.

## Hardware and management

- **Model:** Cisco Nexus 3048TP-1GE (`N3K-C3048TP-1GE`)
- **Ports:** 48 × 10/100/1000BASE-T + 4 × 1/10G SFP+
- **Hostname:** `cisco-nexus-3048tp`
- **Management:** `mgmt0`, `192.168.4.2/24` on VLAN 4
  (`Server tilbehør`), gateway `192.168.4.1` (UDM Pro)
- **SNMP:** v2c community `public`, read-only, used by the inventory collector
- **Console:** RS-232 console, currently connected to glemmen80
- **Config:** [`config_nexus-3048tp.cfg`](config_nexus-3048tp.cfg)
- **Boot image:** `bootflash:nxos.7.0.3.I7.4.bin` (set with `boot nxos`)

The management address is the address previously used by the Zyxel. `mgmt0`
is patched to UDM Pro port 8, which is configured as a VLAN 4 access port.
Keep that UDM port on VLAN 4; if it is reset to the Default network,
`192.168.4.2` becomes unreachable.

The admin password is deliberately not stored in Git. Set it interactively
before saving the configuration.

## Topology

| Port | Device | Mode |
| --- | --- | --- |
| `Ethernet1/1–20` | iDRAC/IMM ports | Access VLAN 4 |
| `Ethernet1/21–26` | glemmen130, 6 × 1G | LACP `port-channel10` |
| `Ethernet1/27–32` | glemmen150, 6 × 1G | LACP `port-channel11` |
| `Ethernet1/33–38` | glemmen160, 6 × 1G | LACP `port-channel12` |
| `Ethernet1/39–40` | glemmen170, 2 × 1G | LACP `port-channel13` |
| `Ethernet1/41–42` | glemmen180, 2 × 1G | LACP `port-channel14` |
| `Ethernet1/43` | glemmen250, 1G | Server trunk |
| `Ethernet1/44` | glemmen50, 1G | Server trunk |
| `Ethernet1/45` | glemmen60, 1G | Server trunk |
| `Ethernet1/46–48` | spare | Server trunk, shut down |
| `Ethernet1/49` (SFP1) | glemmen80, 10G | Server trunk |
| `Ethernet1/50` (SFP2) | future glemmen70, 10G | Server trunk, shut down |
| `Ethernet1/51` (SFP3) | UDM Pro port 10 | Uplink trunk |
| `Ethernet1/52` (SFP4) | unused | Uplink trunk, shut down |

The iDRAC/IMM ports are deliberately not labelled with individual device
names; the patch cabling determines which iDRAC is on which port. The APC ATS
and any other non-iDRAC VLAN 4 accessory can stay on a USW VLAN 4 access
port.

The 3048TP defaults its Ethernet ports to routed mode. The configuration puts
an explicit `switchport` command on every configured port before applying
access/trunk mode.

## VLANs and tagging

| VLAN | Name | Address | Purpose |
| --- | --- | --- | --- |
| 1 | Default | `192.168.50.0/24` | UDM native VLAN; tagged on server ports for the legacy PXE/`bond0.1` interface |
| 3 | Servere | `192.168.6.0/24` | Server VLAN; untagged/native on all server ports |
| 4 | Server tilbehør | `192.168.4.0/24` | iDRAC/IMM and switch management |
| 5 | JubA | `10.23.42.0/24` | Carried on the uplinks for the USW and wireless devices |

- UDM Pro port 10 is the uplink. It is currently an all-network trunk and
  carries tagged VLAN 3, 4 and 5. The switch side uses native VLAN 1; if
  untagged VLAN 1 (PXE/Default) is needed over this trunk, set the UDM port's
  native network to Default.
- The USW Pro 24 PoE stays uplinked to UDM Pro port 1. It is not connected to
  the Nexus; SFP4 is shut down.
- Server ports are trunks with native VLAN 3 and tagged VLAN 1. They do
  not carry VLAN 4; iDRACs are on separate access ports.
- The iDRAC access ports are ordinary edge ports with BPDU guard enabled.

## LACP and PXE

- The switch uses `port-channel load-balance ethernet source-dest-port`. On
  NX-OS this hashes source/destination MAC, IP and TCP/UDP port, i.e. the
  Layer 3 + Layer 4 hash requested for the server bonds.
- The server side should use Linux bonding mode 802.3ad with
  `xmit_hash_policy layer3+4`.
- `no lacp suspend-individual` is configured on every port-channel. On the
  Nexus 3000 this is the LACP fallback mechanism: if a server firmware is in
  PXE and has not started sending LACPDUs, its member links stay up as
  individual ports. Because every member has the same trunk configuration,
  untagged VLAN 3 (DHCP and proxyDHCP) works during PXE.
- `no lacp graceful-convergence` is also set on the server port-channels.
  The default Nexus graceful-convergence behaviour is intended for Nexus
  peers; disabling it gives faster failover to a Linux server bond.
- This configuration uses `no lacp suspend-individual` rather than the
  `lacp mode delay` alternative. NX-OS also has delayed LACP, but the two
  must not be combined on the same port-channel; `no lacp suspend-individual`
  is the simpler fallback for firmware PXE.
- Once the OS bond starts LACP, the links aggregate into the port-channel.
- Keep `lacp min-links` at the default (1). A six-link LAG must stay up when
  one cable fails.

The six-port LAGs replace the current two independent links on
glemmen130/150/160. All six links become one bundle with VLAN 3 native and
VLAN 1 tagged. Update the Talos node configs when the new NICs are installed;
the switch configuration itself is ready for either the old individual-link
layout or the new bond.

## Jumbo frames

The Nexus 3048TP does not support per-port Layer 2 MTU. Jumbo frames are
enabled globally with a network-qos policy:

```text
policy-map type network-qos jumbo
  class type network-qos class-default
    mtu 9216
system qos
  service-policy type network-qos jumbo
```

The server VLAN uses MTU 9000 end to end. The iDRAC VLAN and the
legacy VLAN 1 PXE path stay at 1500 on the hosts; the global switch MTU only
sets the maximum frame size.

## Applying the configuration

1. Connect to the console. Do not connect `mgmt0` yet if the Zyxel is still
   using `192.168.4.2`.
2. Check the NX-OS image and feature set:

   ```text
   show version
   show feature
   ```

3. Apply the configuration from [`config_nexus-3048tp.cfg`](config_nexus-3048tp.cfg).
   It is a running-config fragment without `exit` lines. Load it with
   `copy`/`configure replace`, or paste it block by block and leave each
   submode before the next global block. Apply the global/VLAN/QoS block
   first, then the interfaces.
4. Set the admin password and save:

   ```text
   configure terminal
   username admin password <password> role network-admin
   end
   copy running-config startup-config
   ```

5. Remove the Zyxel, patch `mgmt0` to a VLAN 4 access port, and verify:

   ```text
   ping 192.168.4.1 vrf management
   show ip interface mgmt0
   show ip route vrf management
   ```

## Verification

```text
show vlan brief
show interface status
show interface trunk
show port-channel summary
show port-channel database
show lacp neighbor
show run interface port-channel10
show queuing interface Ethernet1/21
```

Expected port-channel state after the servers start LACP:

```text
show port-channel summary
```

Each server LAG should show `P` (up in port-channel) members and `U` for the
port-channel. During PXE, before LACP starts, members may show `I`
(individual); that is the intended fallback state. After the OS bond comes up,
they change to `P`.

Verify the jumbo MTU on a server-facing port:

```text
show queuing interface Ethernet1/21
```

The output should report `HW MTU: 9216`.

## Migration order

1. **Prepare the UDM.** Confirm UDM Pro port 10 carries tagged VLAN 3, 4 and
   5, and set its native network to Default if untagged VLAN 1 is needed.
   Keep UDM port 8 on VLAN 4 for `mgmt0`.
2. **Install the Nexus.** Apply the configuration over the console. Leave
   SFP2, SFP4 and the spare ports shut down.
3. **Connect SFP3.** Uplink to UDM Pro port 10. Verify VLANs, STP and
   management reachability.
4. **Move the iDRACs.** Patch iDRAC/IMM cables from the Zyxel to
   `Ethernet1/1–20`, then remove the Zyxel and move `mgmt0` to its VLAN 4
   access port. Verify Redfish/IPMI.
5. **Move the single-link servers.** glemmen250 to `Ethernet1/43`, glemmen50
   to `Ethernet1/44` and glemmen60 to `Ethernet1/45`.
6. **Move glemmen80.** Move its 10G link from USW port 26 to SFP1
   (`Ethernet1/49`). Verify the server VLAN.
7. **Move glemmen170 and glemmen180.** Cable the two links to
   `Ethernet1/39–40` and `Ethernet1/41–42`, then configure the server LACP
   bonds.
8. **Install the 4-port NICs and move glemmen130/150/160.** Cable all six
   links to `Ethernet1/21–26`, `27–32` and `33–38`, then configure the
   six-member LACP bonds with `xmit_hash_policy layer3+4`.
9. **Leave the USW on UDM port 1.** SFP4 is not used and stays shut down.
10. **Save and document.** Run `copy running-config startup-config`, update
    the inventory collector after the cutover, and record the final cable
    mapping.

## Rollback

The Zyxel configuration is kept at
[`../zyxel-gs1920-24/`](../zyxel-gs1920-24/) for reference. The old Zyxel can
be reconnected to UDM Pro port 8 (native VLAN 1, tagged VLAN 3 and 4) if the
Nexus has to be removed. Do not run both switches with `192.168.4.2` at the
same time.
