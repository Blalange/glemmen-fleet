# Network mesh probe

A cluster-wide pod-network mesh test. One `network-mesh-probe` pod runs on
every Linux node on the pod network, so each test traverses the Calico
pod-CIDR routes. Peers are the other probe pods'
IPs on port `9101`; the throughput tests use iperf3 on port `5201`. The node
and VLAN underlay uses MTU 9000 and Calico pods use 8980 bytes, so the probe
measures the same jumbo-frame path as application pods.

Each probe has two independent schedules:

- every **60 seconds** it sends 10 ICMP echo requests to every peer probe pod
  (200 ms apart, 1 s timeout); this is the health signal,
- every **30 minutes** it runs a 3-second iperf3 TCP test (2 measured seconds
  after a 1-second warm-up) with 16 parallel streams in each direction and
  records the receiver-side throughput.

The results are exposed on `/metrics` and scraped by the `network-mesh-probe`
PodMonitor. The **Network Mesh** Grafana dashboard (`glemmen-network-mesh`)
renders four source/destination matrices: ping RTT, packet loss, upload
throughput and download throughput. Each matrix has an `avg` and a `max` row
per source, aggregated over the dashboard time range.

## Why pod networking and iperf3?

The probe deliberately runs on the pod network: the tests then traverse the
same Calico pod-CIDR routes as application pods. Running with `hostNetwork`
would measure only the directly connected server-VLAN link and would never exceed
1 Gbit/s on a 1xx node.

The throughput tests use iperf3 with 16 parallel TCP streams. iperf3 runs in
C and measures the receiver-side rate; `-R` reverses the direction so each
probe records both upload and download. The probe image is
`nicolaka/netshoot`, which already provides Python 3 and iperf3.

Each throughput test lasts 2 measured seconds per direction. Tests are
phase-shifted across nodes so only one node runs a throughput cycle at a time;
a full cycle moves roughly `N * (N - 1) * 2 * rate * seconds`. At eight nodes
and 0.9 Gbit/s, that is about 20 GB every 30 minutes, or roughly 0.09 Gbit/s of
aggregate average background traffic. The probe exposes `mesh_test_active`
while a throughput cycle is running so switch-drop alerts can be suppressed
during the intentional bursts. Increase `TRANSFER_INTERVAL_SECONDS` if the
storage/media traffic needs more headroom. The HTTP `/download` and `/upload`
endpoints remain available for manual checks; `TRANSFER_BYTES` only controls
those endpoints.

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
| `PING_INTERVAL_SECONDS` | `60` | Time between ping cycles |
| `TRANSFER_INTERVAL_SECONDS` | `1800` | Time between throughput cycles |
| `PING_COUNT` | `10` | ICMP echo requests per peer |
| `PING_INTERVAL_MS` | `200` | Delay between ICMP requests |
| `PING_TIMEOUT_SECONDS` | `1` | Per-request reply timeout |
| `EXPECTED_NODES` | current 8 nodes | Peers that must appear in the mesh; missing nodes emit `mesh_peer_unreachable=1` |
| `NODE_CAPACITIES` | per-node map | Expected NIC capacity in Mbit/s for utilization panels |
| `TRANSFER_BYTES` | `33554432` | Bytes per manual HTTP `/download`/`/upload` test |
| `TRANSFER_FLOWS` | `16` | Parallel iperf3 streams (and HTTP flows) |
| `TRANSFER_TIMEOUT_SECONDS` | `20` | iperf3 subprocess timeout |
| `IPERF_PORT` | `5201` | Peer iperf3 server port |
| `IPERF_SECONDS` | `3` | Total iperf3 run duration |
| `IPERF_OMIT_SECONDS` | `1` | Initial warm-up second excluded from the interval statistics |
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
| `mesh_ping_success` | source, destination | 1 when at least one reply was received (reachability) |
| `mesh_peer_unreachable` | source, peer | 1 when an expected peer is missing or not Ready |
| `mesh_transfer_mbps` | source, destination, direction | iperf3 receiver-side throughput |
| `mesh_transfer_utilization_percent` | source, destination, direction | Throughput relative to the slower endpoint's capacity |
| `mesh_transfer_error` | source, destination, direction, reason | Last iperf3 failure reason |
| `mesh_transfer_last_success_timestamp_seconds` | source, destination, direction | Last successful test time |
| `mesh_transfer_bytes` | source, destination, direction | Bytes received in the measured interval |
| `mesh_transfer_seconds` | source, destination, direction | Measured interval duration |
| `mesh_transfer_flows` | source, destination, direction | Parallel iperf3 streams |
| `mesh_transfer_success` | source, destination, direction | 1 when the test completed |
| `mesh_iperf_server_up` | node | 1 when the local iperf3 server is listening |
| `mesh_test_active` | node | 1 during a throughput cycle (alert inhibition) |
| `mesh_probe_pod_mtu` | node | MTU of the probe pod interface |
| `mesh_node_capacity_mbps` | node | Expected NIC capacity |
| `mesh_probe_last_ping_timestamp_seconds` | node | Last completed ping cycle |
| `mesh_probe_last_transfer_timestamp_seconds` | node | Last completed throughput cycle |
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
