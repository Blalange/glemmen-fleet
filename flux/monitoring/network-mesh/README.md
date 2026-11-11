# Network mesh probe

A cluster-wide node-to-node network test. One `network-mesh-probe` pod runs on
every Linux node with `hostNetwork`, so each test is sourced from that node's
own network stack and targets the other nodes' `InternalIP` addresses on port
`9101`.

Every five minutes each probe:

- sends 4 ICMP echo requests to every peer (200 ms apart, 1 s timeout),
- uploads 32 MiB of zeroes to every peer's `/upload` endpoint,
- downloads 32 MiB of zeroes from every peer's `/download` endpoint.

The results are exposed on `/metrics` and scraped by the `network-mesh-probe`
PodMonitor. The **Network Mesh** Grafana dashboard (`glemmen-network-mesh`)
renders four source/destination matrices: ping RTT, packet loss, upload
throughput and download throughput.

## Why host networking and HTTP transfers?

`hostNetwork` is required for the measurements to reflect the node's network
path rather than a pod overlay. Using HTTP for the throughput tests keeps the
probe self-contained: the same process is both the test client and the test
server, so no extra iperf3 image, port or orchestration is needed. The
`/upload` handler reads and discards the body; `/download` streams zeroes
without buffering the whole transfer.

The 32 MiB payload takes roughly 0.3 s on a 1 Gbit/s link and 0.03 s on a
10 Gbit/s link. Tests are phase-shifted across nodes so the whole mesh is not
saturated at once. For `N` nodes a full cycle sends `N * (N - 1) * 2 * 32 MiB`;
with the eight current nodes that is about 3.5 GiB every five minutes, or
roughly 0.1 Gbit/s of aggregate average background traffic.

## Layout

| File | Purpose |
| --- | --- |
| `daemonset.yaml` | One host-network probe per node, env configuration, probes |
| `probe.py` | ICMP + HTTP test loop and Prometheus/HTTP server (stdlib only) |
| `rbac.yaml` | ServiceAccount and read-only node list permission |
| `kustomization.yaml` | ConfigMap generator for `probe.py` |

The deployed resources are in `flux/monitoring/network-mesh/`. They are applied
by a dedicated Flux Kustomization (`flux/clusters/glemmen/network-mesh.yaml`,
`wait: false`) instead of the `monitoring` group: a node that is down leaves its
DaemonSet pod missing, and the monitoring Kustomization must not wait for the
probe to become ready. The Prometheus `PodMonitor` is in
`flux/monitoring/resources/servicemonitors/network-mesh.yaml` and the dashboard
in `flux/monitoring/resources/dashboards/network-mesh/network-mesh.json`.

## Configuration

The DaemonSet passes these environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `INTERVAL_SECONDS` | `300` | Time between test cycles (minimum 30) |
| `PING_COUNT` | `4` | ICMP echo requests per peer |
| `PING_INTERVAL_MS` | `200` | Delay between ICMP requests |
| `PING_TIMEOUT_SECONDS` | `1` | Per-request reply timeout |
| `TRANSFER_BYTES` | `33554432` | Bytes per HTTP upload/download test |
| `TRANSFER_TIMEOUT_SECONDS` | `15` | HTTP connect/read timeout |
| `PORT` | `9101` | Metrics and transfer server port |

`STATIC_PEERS` can replace Kubernetes node discovery, which is useful for
running the probe outside the cluster:

```sh
STATIC_PEERS='node-b=192.0.2.20,node-c=192.0.2.30' \
NODE_NAME=node-a PORT=9101 python3 flux/monitoring/network-mesh/probe.py
```

Peer entries are `name=host[:port]`; the port defaults to `PORT`. The probe
excludes its own `NODE_NAME` from the target list.

## Metrics

All test metrics carry `source` and `destination` node names. Probe-level
metrics use `node` for the probe's own node; the PodMonitor adds the same
`node` label to every series for consistency with node-exporter (`honorLabels`
keeps the probe's value where both exist).

| Metric | Labels | Meaning |
| --- | --- | --- |
| `mesh_ping_rtt_ms` | source, destination | Average ICMP RTT |
| `mesh_ping_rtt_min_ms` | source, destination | Minimum RTT |
| `mesh_ping_rtt_max_ms` | source, destination | Maximum RTT |
| `mesh_ping_rtt_mdev_ms` | source, destination | Mean RTT deviation |
| `mesh_ping_loss_percent` | source, destination | Packet loss (0-100) |
| `mesh_ping_sent_packets` | source, destination | Echo requests sent |
| `mesh_ping_received_packets` | source, destination | Echo replies received |
| `mesh_ping_success` | source, destination | 1 when no packet was lost |
| `mesh_transfer_mbps` | source, destination, direction | Measured throughput |
| `mesh_transfer_bytes` | source, destination, direction | Bytes transferred |
| `mesh_transfer_seconds` | source, destination, direction | Transfer duration |
| `mesh_transfer_success` | source, destination, direction | 1 when the test completed |
| `mesh_last_test_timestamp_seconds` | source, destination, test | Last test completion time |
| `mesh_probe_info` | node, version | Static probe info |
| `mesh_probe_start_time_seconds` | node | Probe process start time |
| `mesh_probe_peers` | node | Configured peer count |
| `mesh_probe_last_cycle_timestamp_seconds` | node | Last cycle completion time |
| `mesh_probe_cycle_duration_seconds` | node | Last cycle duration |
| `mesh_peer_ready` | source, peer | Peer Ready condition from Kubernetes |
| `mesh_peer_info` | source, peer, address, port | Peer address |

`direction` is `upload` (source -> destination) or `download` (destination ->
source). Failed transfers keep `mesh_transfer_success=0` and omit
`mesh_transfer_mbps`, so stale throughput values are not shown after a failure.

## Verifying and troubleshooting

```sh
kubectl -n monitoring get pods -l app.kubernetes.io/name=network-mesh-probe -o wide
kubectl -n monitoring logs -l app.kubernetes.io/name=network-mesh-probe --tail=50

# metrics from one node
curl -s http://192.168.6.130:9101/metrics | grep mesh_
curl -s http://192.168.6.130:9101/info

# Prometheus targets and a quick query
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
curl -s 'http://127.0.0.1:9090/api/v1/query?query=mesh_ping_rtt_ms'
```

Common causes of failing tests:

- **Ping fails, HTTP works**: raw ICMP is blocked by the network or the
  container could not get `NET_RAW`. Check the probe log and the
  `mesh_ping_*` series.
- **HTTP fails, ping works**: the peer's probe pod is not running, or port
  9101 is blocked between nodes.
- **All metrics missing**: the PodMonitor is not selected. Check
  `kubectl -n monitoring get podmonitor network-mesh-probe -o yaml` and the
  Prometheus targets page.
- **Table cell is `-`**: the source has not completed a test against that
  destination yet, or the peer was removed from the node list.

The probe is intentionally not a privileged pod. It gets only `NET_RAW`,
drops all other capabilities, runs with a read-only root filesystem and
tolerates all taints so it also runs on the control-plane nodes.
