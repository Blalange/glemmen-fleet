# UniFi mirror feeds for ntopng

The cluster's ntopng instance runs on `glemmen80` and listens on two spare
1 Gbit/s NICs. This page records the UDM Pro and USW Pro 24 PoE mirror
configuration, the cabling, and the limitations of the UniFi hardware.

ntopng itself is documented in
[`flux/apps/ntopng/`](../../flux/apps/ntopng/README.md).

## Current configuration

Configured on 2026-11-26. The UDM Pro runs UniFi OS 6.0.17 and the USW Pro
24 PoE runs firmware 7.5.15.

| Device | Mirror destination | Mirror source | Cable to glemmen80 |
| --- | --- | --- | --- |
| UDM Pro | Port 7 | Port 9 (WAN) | `eno1` |
| USW Pro 24 PoE | Port 3 (`ntopng mirror`) | Port 1 (uplink to UDM) | `eno2` |

Both destination ports are 1 Gbit/s, matching the mirrored source links:
the UDM WAN is 1 Gbit/s and the USW uplink to UDM port 1 is 1 Gbit/s. The
`glemmen80` 10G port `enp4s0f1` stays free for a future Nexus SPAN.

> **Cabling still required.** The controller settings are in place, but the
> two patch cables (UDM port 7 → `glemmen80` `eno1`, USW port 3 →
> `glemmen80` `eno2`) must be installed before ntopng sees any traffic. Both
> destination ports currently report `up=false` / no carrier.

The USW has one mirror session (`max_mirror_sessions: 1`). Its Port 26
(`SFP+ 2`) is **not** the mirror destination: the Nexus `SFP4` /
`Ethernet1/52` is reserved as a future USW uplink, and both ends are shut.
Do not enable them while the USW is uplinked to UDM port 1, or the USW will
have two paths into the same network.

## What is captured

- **UDM WAN mirror**: all internet traffic routed by the UDM, including
  traffic from servers on the Nexus, clients on the USW, VPN clients and the
  UDM itself.
- **USW uplink mirror**: all traffic entering or leaving the USW through its
  uplink, i.e. client internet traffic and client-to-server/client-to-client
  traffic that the UDM routes. Traffic switched locally between two ports on
  the USW is not captured, because UniFi only offers 1:1 port mirroring.

Server-to-server traffic that stays on the Cisco Nexus is not captured.
If that is needed, use a Nexus SPAN on the server VLAN to `glemmen80`'s free
10G port (`enp4s0f1`); the `eno1`/`eno2` feeds stay as they are.

## Limitations and the UDM Pro

UniFi's Network application only exposes **Mirroring** as a port operation on
UniFi switches. On the UDM Pro gateway the port 7 Operation dropdown only
lists `Switching` and `Aggregating`. A controller write without the session's
`X-CSRF-Token` header also returns `403 Forbidden`, which initially looked
like a permission problem.

The configuration above was nevertheless accepted by the UDM Pro through the
controller's internal API, and the device reports:

```text
port 7  Port 7  op_mode=mirror  mirror_port_idx=9
port 9  Port 9  op_mode=switch
```

Ubiquiti does not officially support port mirroring on UDM Pro gateways; the
option has historically appeared and disappeared between Network releases.
Treat the UDM feed as best-effort: verify that `eno1` actually receives
packets after cabling, and re-check the port override after a UDM firmware or
Network application upgrade. If the UDM stops mirroring:

1. Re-apply the UDM port override through the API (below).
2. Use the USW uplink mirror as the primary client feed; it already covers
   all internet and internal traffic from USW clients.
3. For server internet/internal traffic, add a Nexus SPAN of the UDM uplink
   (`Ethernet1/51`, VLANs 1/3/4/5) or of server VLAN 3 to a free Nexus port
   connected to `glemmen80`'s 10G `enp4s0f1`.
4. As a last resort, put a small mirror-capable switch or a network TAP on
   the WAN link; a UDM on-boot `tc` script is possible but fragile across
   UniFi OS upgrades.

## Applying the USW mirror in the UI

1. UniFi Network → Devices → **USW Pro 24 PoE** → **Port Manager**.
2. Select **Port 3** (or another unused 1G port) → **Operation: Mirroring**
   → **Mirror From: Port 1** → set **PoE: Off** → **Apply Changes**.
3. Rename the destination port to `ntopng mirror` so it is not reused by
   accident.
4. Connect that port to `glemmen80` `eno2`.

The USW must have only one mirror session. Restore the previous port to
**Operation: Switching** before moving the mirror to a different port.

## Applying via the controller API

The UI hides Mirroring for the UDM, so the UDM override was written through
the legacy controller API. Both devices use the same payload shape:

```json
{
  "port_overrides": [
    {
      "port_idx": 7,
      "setting_preference": "manual",
      "name": "Port 7",
      "op_mode": "mirror",
      "mirror_port_idx": 9
    }
  ]
}
```

The `PUT` goes to:

```text
/proxy/network/api/s/default/rest/device/<device-id>
```

Unsafe methods need the session's `X-CSRF-Token` header in addition to the
`TOKEN` cookie. The token is in the `csrfToken` claim of the `TOKEN` JWT
returned by `/api/auth/login`; the controller also exposes it in the
`X-CSRF-Token` response header. A minimal flow from a host that can reach the
controller is:

```sh
# 1. Log in (UniFi OS)
curl -k -s -c cookies -H 'Content-Type: application/json' \
  -d '{"username":"...","password":"..."}' \
  https://192.168.50.1/api/auth/login > login.json

# 2. Extract the CSRF token from the TOKEN cookie (or use the response header)
# 3. PUT the full port_overrides array, preserving every port you do not
#    want to change. Read the current array from /stat/device first.
curl -k -s -b cookies -X PUT \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  --data-binary @payload.json \
  https://192.168.50.1/proxy/network/api/s/default/rest/device/<device-id>
```

Use the `unpoller` Network admin account or another admin with
`network.management` permission. Do not store the credentials in Git. The
controller is reachable from the server VLAN at `192.168.50.1`; the public
`unifi.glem.men` endpoint may be unavailable during WAN maintenance.

The UDM and USW device IDs and the full current overrides can be read from:

```sh
curl -k -s -b cookies \
  https://192.168.50.1/proxy/network/api/s/default/stat/device
```

## Verification

`talosctl` and the ntopng pod both show the mirror NICs:

```sh
talosctl -n 192.168.6.80 get links | grep -E 'eno1|eno2'
kubectl -n ntopng logs deploy/ntopng | grep -E 'Reading packets|Unable to open'
```

The controller's device state should show the mirror source on the
destination port:

```sh
# On the UDM/USW
curl -k -s -b cookies \
  https://192.168.50.1/proxy/network/api/s/default/stat/device |
  jq -r '.data[] | select(.model=="UDMPRO" or .model=="US24PRO") |
    .name as $n | .port_table[] |
    select(.op_mode=="mirror") |
    [$n,.port_idx,.name,.op_mode,(.mirror_port_idx|tostring)] | @tsv'
```

Expected:

```text
Dream Machine Pro B bygg  7  Port 7  mirror  9
USW Pro 24 PoE            3  ntopng mirror  mirror  1
```

Then in ntopng, both interface tabs must show increasing packet counters.
If `eno1` is flat, the UDM did not mirror; if `eno2` is flat, check the
USW port 3 link and cable.

## Rollback

- **USW**: Port Manager → Port 3 → **Operation: Switching** → Apply Changes.
  The port returns to its previous native VLAN 3 server-trunk profile.
- **UDM**: read the current full `port_overrides` array, replace only the
  port 7 entry with the original switching profile (remove `op_mode` and
  `mirror_port_idx`, restore `setting_preference: auto`), and PUT the full
  array back. The port 7 entry looked like this before the change:

```json
{
  "port_idx": 7,
  "setting_preference": "auto",
  "name": "Port 7",
  "forward": "all",
  "native_networkconf_id": "3f065cb4-46c0-4593-bec2-fb84f93b2ff8",
  "port_security_enabled": false,
  "port_security_mac_address": [],
  "autoneg": true,
  "tagged_vlan_mgmt": "auto"
}
```

After rollback, remove the corresponding interface from the ntopng
`-i` arguments (or leave it configured; ntopng will simply see no traffic).
