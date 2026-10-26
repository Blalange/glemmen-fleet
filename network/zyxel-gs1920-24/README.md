# Zyxel GS1920-24

Switch for miscellaneous server accessories on the `192.168.4.0/24`
(`Server tilbehør`, VLAN 4) network.

## Management

- **IP:** `192.168.4.2/24` on VLAN 4 (`Server tilbehør`)
- **Default gateway:** `192.168.4.1` (UDM Pro)
- **Web UI:** <http://192.168.4.2/>
- **SSH/Telnet:** `192.168.4.2` (`admin`; password is the existing switch
  password - change the factory default if it has not been changed)
- **Firmware:** V4.10(AAOB.5) | 05/05/2015
- **Model:** GS1920-24 (24 × 1G ports + 4 × SFP)

The old factory management IP `192.168.1.1` and the interim `192.168.50.2` are
no longer used. The switch is reachable from VLAN 4 and from other VLANs via
the UDM Pro's inter-VLAN routing (same firewall zone, no rules).

## Topology

| Switch port | Connected to | VLAN |
|---|---|---|
| 1 | iDRAC `DL51JZ3` | 4, untagged |
| 2 | iDRAC `FL51JZ3` | 4, untagged |
| 3 | iDRAC `4L51JZ3` | 4, untagged |
| 4 | iDRAC `6L51JZ3` | 4, untagged |
| 5 | iDRAC `2L51JZ3` | 4, untagged |
| 6 | server accessory (MAC `5c:f3:fc:de:12:3d`) | 4, untagged |
| 7 | link up, no MAC learned yet | 4, untagged |
| 8 | management laptop / access device | 4, untagged (PVID 4) |
| 9-27 | spare access ports | 4, untagged (PVID 4) |
| 28 | UDM Pro port 8 | 1 untagged (PVID 1) + 4 tagged |

The UDM Pro port 8 is configured with the **Default** network as its native
network (`192.168.50.0/24`) and allows tagged VLAN 4. The switch keeps VLAN 1
untagged on the uplink as the native link to the UDM Default network; all
access devices, including the laptop on port 8, are on VLAN 4.

### VLANs

- **VLAN 1** - only the uplink port 28 is untagged; it has no management IP.
  It is the native link to the UDM Pro port 8 (UDM Default network) and is not
  used by any access device.
- **VLAN 4 `Server tilbehør`** - `192.168.4.0/24`, gateway/DHCP on the UDM Pro
  (`192.168.4.1`, DHCP pool `.6`-`.254`): ports 1-27 untagged with PVID 4;
  port 28 tagged. The switch management IP is `192.168.4.2`.

Access ports use `frame-type untagged`; the uplink uses the default
`frame-type all` so it accepts the untagged VLAN 1 and tagged VLAN 4.

> The GS1920 firmware only supports DHCP relay, not a DHCP server. The UDM Pro
> remains the DHCP server and gateway for `192.168.4.0/24`.

## DHCP / addressing

- The APC automatic transfer switch has a fixed DHCP reservation at
  `192.168.4.3` (UniFi Network -> client MAC `00:c0:b7:f4:7e:d0`).
- The iDRACs and IBM IMM are on VLAN 4 at `192.168.4.50`, `.60`, `.80`,
  `.130`, `.150`, `.160`, `.170` and `.180`.
- The switch itself is static at `192.168.4.2`.

## Configuration backup

`config_GS1920-24.log` is the text configuration backup downloaded from the
switch (**Maintenance -> Backup Configuration**). It is the authoritative
export of the VLAN, PVID and management settings.

To restore it:

1. Open <http://192.168.4.2/>.
2. Go to **Maintenance -> Restore Configuration**.
3. Upload `config_GS1920-24.log` and confirm.

After any change in the web UI, use the **Save** link in the top-right corner
to persist the running configuration to non-volatile memory. Restoring a
configuration may reboot the switch and briefly disconnect its ports.

The config file contains no login credentials, but it does contain the switch
management address and VLAN/port layout.
