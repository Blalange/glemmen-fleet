# Network mesh probe

A cluster-wide pod-network mesh test. One `network-mesh-probe` pod runs on
every Linux node on the pod network, so each test traverses the Calico
pod-CIDR routes and the ECMP multipath links. Peers are the other probe pods'
IPs on port `9101`; the throughput tests use iperf3 on port `5201`. The node
and VLAN underlay uses MTU 9000, Calico pods use 8980 bytes, and the route
agent raises the NIC rings, so the probe measures the same jumbo/ECMP path as
application pods.

Every five minutes each probe:

- sends 4 ICMP echo requests to every peer probe pod (200 ms apart, 1 s
  timeout),
- runs a 2-second iperf3 TCP test with 16 parallel streams in each direction
  and records the receiver-side throughput.

The results are exposed on `/metrics` and scraped by the `network-mesh-probe`
PodMonitor. The **Network Mesh** Grafana dashboard (`glemmen-network-mesh`)
renders four source/destination matrices: ping RTT, packet loss, upload
throughput and download throughput. Each matrix has an `avg` and a `max` row
per source, aggregated over the dashboard time range.

## Why pod networking and iperf3?

The probe deliberately runs on the pod network: the tests then traverse the
same Calico pod-CIDR routes as application pods, including the ECMP multipath
links between the server VLAN and VLAN 7. Running with `hostNetwork` would
measure only the directly connected server-VLAN link and would never exceed
1 Gbit/s on a 1xx node.

The throughput tests use iperf3 with 16 parallel TCP streams. The earlier
Python HTTP transfer was CPU-bound in its write path at roughly 1 Gbit/s and
could not show the 2 Gbit/s ECMP aggregate. Jumbo frames raise the practical
TCP ceiling from about 1.88 to 1.96 Gbit/s; live 16-flow results are about
1.9 Gbit/s upload and 1.75–1.83 Gbit/s download. iperf3 runs in C and measures the
receiver-side rate; `-R` reverses the direction so each probe records both
upload and download. The probe image is `nicolaka/netshoot`, which already
provides Python 3 and iperf3 and is used by the ECMP route agent.

Each test lasts 2 seconds per direction. Tests are phase-shifted across nodes so
only one pair runs at a time; a full cycle moves roughly
`N * (N - 1) * 2 * rate * seconds`. At 1.8 Gbit/s and two seconds per direction,
the six current nodes move about 27 GB every five minutes, or roughly
0.72 Gbit/s of aggregate average background traffic. Increase
`INTERVAL_SECONDS` if that is too much for the storage/media traffic. The HTTP
`/download` and `/upload` endpoints remain available for manual checks;
`TRANSFER_BYTES` only controls those endpoints.

## Layout

| File | Purpose |
| --- | --- |
| `daemonset.yaml` | One pod-network probe per node, env configuration, probes |
| `probe.py` | ICMP + iperf3 test loop and Prometheus/HTTP server (stdlib + iperf3) |
| `rbac.yaml` | ServiceAccount and read-only pod list permission |
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
| `TRANSFER_BYTES` | `33554432` | Bytes per manual HTTP `/download`/`/upload` test |
| `TRANSFER_FLOWS` | `16` | Parallel iperf3 streams (and HTTP flows) |
| `TRANSFER_TIMEOUT_SECONDS` | `15` | iperf3 subprocess timeout |
| `IPERF_PORT` | `5201` | Peer iperf3 server port |
| `IPERF_SECONDS` | `2` | Duration of each iperf3 test |
| `IPERF_OMIT_SECONDS` | `1` | Initial seconds omitted from iperf3 statistics |
| `PORT` | `9101` | Metrics, health and manual-transfer HTTP port |

`STATIC_PEERS` can replace Kubernetes pod discovery, which is useful for
running the probe outside the cluster:

```sh
STATIC_PEERS='node-b=192.0.2.20,node-c=192.0.2.30' \
NODE_NAME=node-a PORT=9101 IPERF_PORT=5201 \
python3 flux/monitoring/network-mesh/probe.py
```

Peer entries are `name=host[:port]`; the port defaults to `PORT` and is the
HTTP/info port. The throughput test always uses `IPERF_PORT`, so each peer must
run an iperf3 server there. The probe excludes its own `NODE_NAME` from the
target list.

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
| `mesh_transfer_mbps` | source, destination, direction | iperf3 receiver-side throughput |
| `mesh_transfer_bytes` | source, destination, direction | Bytes received in the measured interval |
| `mesh_transfer_seconds` | source, destination, direction | Measured interval duration |
| `mesh_transfer_flows` | source, destination, direction | Parallel iperf3 streams |
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

# metrics from one probe pod (pod IP from the get pods output)
POD_IP=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=network-mesh-probe -o jsonpath='{.items[0].status.podIP}')
curl -s "http://${POD_IP}:9101/metrics" | grep mesh_
curl -s "http://${POD_IP}:9101/info"

# Prometheus targets and a quick query
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
curl -s 'http://127.0.0.1:9090/api/v1/query?query=mesh_ping_rtt_ms'
```

Common causes of failing tests:

- **Ping fails, throughput works**: raw ICMP is blocked by the network or the
  container could not get `NET_RAW`. Check the probe log and the
  `mesh_ping_*` series.
- **Throughput fails, ping works**: the peer's iperf3 server is not running,
  or port 5201 is blocked between nodes. Check the probe log for
  `iperf3 server listening` / `failed to start iperf3 server`.
- **All metrics missing**: the PodMonitor is not selected. Check
  `kubectl -n monitoring get podmonitor network-mesh-probe -o yaml` and the
  Prometheus targets page.
- **Table cell is `-`**: the source has not completed a test against that
  destination yet, or the peer probe pod is not running.

The probe is intentionally not a privileged pod. It gets only `NET_RAW`,
drops all other capabilities, runs with a read-only root filesystem and
tolerates all taints so it also runs on the control-plane nodes.
