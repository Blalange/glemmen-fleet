# NetBird / Calico filter-table guard

NetBird runs on every node as a Talos system extension. Its nftables firewall
manager tries to open the `wt0` WireGuard interface in the host's
`ip filter` table. Talos does not ship an `iptables` binary, so NetBird falls
back to writing **native nftables rules** into that table:

```
iifname "wt0" accept
oifname "wt0" ct state established,related accept
```

Calico owns the same table through iptables-nft. The iptables 1.8.10 in
`calico-node` cannot parse a table that contains native nft rules and prints
`Table 'filter' is incompatible, use 'nft' tool`. Felix then panics
(`felix/table.go: iptables-nft-save command failed after retries`) and stops
programming workload endpoints. New pods on that node end up without their
veth in `cali-from-wl-dispatch-*`, so their packets hit the `Unknown
interface` DROP and DNS/UDP fails.

NetBird only writes the rules when its firewall manager is created, i.e. on
NetBird start or restart. At boot NetBird starts before Calico, so the table
does not exist and no rules are written. The failure mode is a NetBird
restart after Calico is already running (configuration changes, crashes, or
manual restarts).

`netbird-filter-guard` removes the native `wt0` rules every 5 seconds. It
matches only native nft rules (it skips rules containing `xt match`) and only
for the `wt0` interface, so Calico's own iptables rules are untouched. The
guard keeps NetBird's ACLs intact: those live in NetBird's own `netbird`
nftables table, and the removed rules are redundant because there are no
Calico `HostEndpoint` resources filtering host traffic.

The DaemonSet is deliberately not part of the `infrastructure` Kustomization:
it must be able to run while Calico is unhealthy, and it must not make the
infrastructure health check fail when a node is down.

If the NetBird interface name is ever changed from the default `wt0` (via
`NB_INTERFACE_NAME`), update the match in `daemonset.yaml`.
