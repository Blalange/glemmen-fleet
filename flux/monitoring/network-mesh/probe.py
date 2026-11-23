#!/usr/bin/env python3
"""Pod-network mesh probe for the glemmen cluster.

Runs as a DaemonSet with pod networking, so exactly one instance runs on each
node. Every instance periodically:

  * pings every other probe pod with ICMP echo requests every minute,
  * runs a short iperf3 TCP test with 16 parallel streams to every other
    probe pod in both directions every 30 minutes,

and exposes the results as Prometheus metrics on /metrics. Peer probe pods are
discovered from the Kubernetes API (or from STATIC_PEERS when running outside
the cluster). Running on the pod network makes the tests traverse the Calico
pod-CIDR routes.

The HTTP server also implements endpoints for metrics, health and manual
transfer checks:

  GET  /download?bytes=N  returns N zero bytes
  POST /upload            reads and discards the request body
  GET  /metrics           Prometheus text exposition
  GET  /healthz           liveness/readiness endpoint
  GET  /info              probe configuration as JSON

Only the Python standard library is used, plus the iperf3 binary for the
throughput tests. The probe runs in the nicolaka/netshoot image, which provides
Python 3 and iperf3.
"""

import json
import math
import os
import random
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.4.0"

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
SA_TOKEN = os.path.join(SA_DIR, "token")
SA_CA = os.path.join(SA_DIR, "ca.crt")

ZERO_BLOCK = b"\0" * (256 * 1024)
ZERO_FILE = "/tmp/network-mesh-zero"
ZERO_FILE_SIZE = 4 * 1024 * 1024
ZERO_FILE_LOCK = threading.Lock()
DEFAULT_PORT = 9101


def env_int(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit("invalid {}: {!r}".format(name, value))


def env_float(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise SystemExit("invalid {}: {!r}".format(name, value))


NODE_NAME = os.environ.get("NODE_NAME") or socket.gethostname()
NODE_IP = os.environ.get("NODE_IP", "")
POD_NAME = os.environ.get("POD_NAME", "")
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "monitoring")
PORT = env_int("PORT", DEFAULT_PORT)
# Pings are the health signal and must stay frequent; throughput tests are
# deliberately infrequent so they do not saturate the 1 Gbit/s access ports.
PING_INTERVAL = max(10.0, env_float("PING_INTERVAL_SECONDS", 60.0))
TRANSFER_INTERVAL = max(
    60.0, env_float("TRANSFER_INTERVAL_SECONDS", 1800.0))
PING_COUNT = max(1, env_int("PING_COUNT", 10))
PING_INTERVAL_MS = max(0.05, env_float("PING_INTERVAL_MS", 200.0) / 1000.0)
PING_TIMEOUT = max(0.1, env_float("PING_TIMEOUT_SECONDS", 1.0))
TRANSFER_BYTES = max(1024, env_int("TRANSFER_BYTES", 32 * 1024 * 1024))
TRANSFER_TIMEOUT = max(1.0, env_float("TRANSFER_TIMEOUT_SECONDS", 20.0))
TRANSFER_FLOWS = max(1, min(64, env_int("TRANSFER_FLOWS", 16)))
IPERF_PORT = env_int("IPERF_PORT", 5201)
IPERF_SECONDS = max(2, env_int("IPERF_SECONDS", 3))
IPERF_OMIT = max(
    0, min(IPERF_SECONDS - 1, env_int("IPERF_OMIT_SECONDS", 1)))
MAX_TRANSFER_BYTES = 1024 * 1024 * 1024
STATIC_PEERS_RAW = os.environ.get("STATIC_PEERS", "")
EXPECTED_NODES_RAW = os.environ.get("EXPECTED_NODES", "")
NODE_CAPACITIES_RAW = os.environ.get("NODE_CAPACITIES", "")

PROBE_LABEL = "node"


def parse_expected_nodes(raw):
    return sorted({item.strip() for item in raw.split(",") if item.strip()})


def parse_capacities(raw):
    capacities = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, value = item.split(":", 1)
        try:
            capacities[name.strip()] = float(value)
        except ValueError:
            continue
    return capacities


EXPECTED_NODES = parse_expected_nodes(EXPECTED_NODES_RAW)
NODE_CAPACITIES = parse_capacities(NODE_CAPACITIES_RAW)
IPERF_SERVER_UP = False


def log(message):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print("{} {}".format(stamp, message), flush=True)


def stable_jitter(name, maximum):
    """Deterministic per-node jitter in [0, maximum)."""
    value = 0
    for char in name:
        value = (value * 131 + ord(char)) & 0xFFFFFFFF
    return value % max(1, int(maximum))


def split_host_port(value, default_port):
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ValueError("invalid address: {}".format(value))
        host = value[1:end]
        rest = value[end + 1:]
        if rest.startswith(":"):
            return host, int(rest[1:])
        return host, default_port
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    return value, default_port


def parse_static_peers(raw):
    peers = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, address = item.split("=", 1)
            name = name.strip()
        else:
            name = address = item
        host, port = split_host_port(address.strip(), PORT)
        peers.append({
            "name": name,
            "host": host,
            "port": port,
            "ready": True,
        })
    return sorted(peers, key=lambda peer: peer["name"])


STATIC_PEERS = parse_static_peers(STATIC_PEERS_RAW)


# ---------------------------------------------------------------------------
# ICMP ping


def icmp_checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack("!{}H".format(len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def ping(host, count=PING_COUNT, interval=PING_INTERVAL_MS,
         timeout=PING_TIMEOUT):
    """Return RTT statistics for `count` ICMP echo requests to `host`."""
    result = {
        "sent": count,
        "received": 0,
        "loss": 100.0,
        "success": False,
        "error": None,
    }
    try:
        address = socket.gethostbyname(host)
    except OSError as exc:
        result["error"] = "resolve {}: {}".format(host, exc)
        return result

    sock = None
    raw = True
    try:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                                 socket.IPPROTO_ICMP)
        except OSError:
            # Ping sockets do not need CAP_NET_RAW, but are often disabled by
            # the kernel's net.ipv4.ping_group_range. Fall back to them when
            # raw sockets are not permitted.
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                 socket.IPPROTO_ICMP)
            raw = False

        ident = random.randint(0, 0xFFFF)
        payload = b"mesh-probe" + struct.pack("!Q", int(time.time() * 1000))
        rtts = []
        for seq in range(count):
            packet = struct.pack("!BBHHH", 8, 0, 0, ident, seq) + payload
            checksum = icmp_checksum(packet) if raw else 0
            packet = struct.pack(
                "!BBHHH", 8, 0, checksum, ident, seq) + payload
            started = time.perf_counter()
            try:
                sock.sendto(packet, (address, 0))
            except OSError as exc:
                result["error"] = "send: {}".format(exc)
                break

            deadline = started + timeout
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, sender = sock.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError:
                    break
                if sender[0] != address:
                    continue

                # Raw sockets include the IPv4 header; ping sockets do not.
                offset = 0
                if data and (data[0] >> 4) == 4:
                    offset = (data[0] & 0x0F) * 4
                icmp = data[offset:]
                if len(icmp) < 8:
                    continue
                kind, _code, _checksum, reply_id, reply_seq = struct.unpack(
                    "!BBHHH", icmp[:8])
                if kind != 0 or reply_seq != seq:
                    continue
                if raw and reply_id != ident:
                    continue
                rtts.append((time.perf_counter() - started) * 1000.0)
                break

            if seq < count - 1:
                elapsed = time.perf_counter() - started
                time.sleep(max(0.0, interval - elapsed))

        result["received"] = len(rtts)
        result["loss"] = round(
            100.0 * (count - len(rtts)) / count, 3) if count else 0.0
        # Success means "reachable", not "zero packets lost".  Packet loss is
        # reported separately; requiring 100% replies turned one lost packet
        # into a false outage.
        result["success"] = len(rtts) > 0
        if rtts:
            average = sum(rtts) / len(rtts)
            result["min"] = min(rtts)
            result["avg"] = average
            result["max"] = max(rtts)
            result["mdev"] = sum(abs(rtt - average) for rtt in rtts) / len(
                rtts)
        if not rtts and not result["error"]:
            result["error"] = "no ICMP reply"
    except OSError as exc:
        result["error"] = str(exc)
    finally:
        if sock is not None:
            sock.close()
    return result


# ---------------------------------------------------------------------------
# Throughput tests


def ensure_zero_file():
    """Create the file used by the sendfile-based download path."""
    with ZERO_FILE_LOCK:
        if not os.path.exists(ZERO_FILE):
            with open(ZERO_FILE, "wb") as handle:
                handle.write(
                    ZERO_BLOCK * (ZERO_FILE_SIZE // len(ZERO_BLOCK)))
    return ZERO_FILE


def transfer(host, direction, timeout=TRANSFER_TIMEOUT):
    """Run an iperf3 TCP test and return receiver-side throughput.

    iperf3's `end.sum_received` covers the whole run (including the omitted
    warm-up), so use the post-omit interval statistics instead.  This keeps
    `mesh_transfer_seconds` honest and makes throughput comparable across
    runs.
    """
    result = {
        "direction": direction,
        "bytes": 0,
        "success": False,
        "error": None,
        "error_reason": None,
        "flows": TRANSFER_FLOWS,
    }
    command = [
        "iperf3", "-c", host, "-p", str(IPERF_PORT),
        "-P", str(TRANSFER_FLOWS), "-t", str(IPERF_SECONDS),
        "-J", "--connect-timeout", "3000",
    ]
    if IPERF_OMIT:
        command += ["-O", str(IPERF_OMIT)]
    if direction == "download":
        command.append("-R")

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True,
            timeout=max(timeout, IPERF_SECONDS + 10))
        elapsed = time.perf_counter() - started
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            result["error_reason"] = "iperf_exit_{}".format(
                completed.returncode)
            result["error"] = detail[:1000]
            result["seconds"] = elapsed
            return result
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            result["error_reason"] = "iperf_json"
            result["error"] = "{}: {}".format(exc, completed.stdout[:500])
            result["seconds"] = elapsed
            return result

        intervals = payload.get("intervals") or []
        measured = [
            item["sum"] for item in intervals
            if isinstance(item, dict) and isinstance(item.get("sum"), dict)
            and float(item["sum"].get("start", 0.0)) >= IPERF_OMIT
        ]
        if measured:
            result["bytes"] = sum(int(item.get("bytes", 0))
                                  for item in measured)
            measured_seconds = sum(float(item.get("seconds", 0.0))
                                   for item in measured)
            result["seconds"] = measured_seconds or elapsed
            if measured_seconds > 0:
                result["mbps"] = (
                    result["bytes"] * 8.0 / measured_seconds / 1_000_000.0)
            else:
                result["mbps"] = 0.0
        else:
            stats = (payload.get("end") or {}).get("sum_received") or {}
            result["bytes"] = int(stats.get("bytes", 0))
            result["seconds"] = float(stats.get("seconds", elapsed))
            result["mbps"] = float(
                stats.get("bits_per_second", 0.0)) / 1_000_000.0
        result["success"] = result["bytes"] > 0 and result["mbps"] > 0
        if not result["success"]:
            result["error_reason"] = "iperf_no_stats"
            result["error"] = "iperf3 returned no receiver statistics"
    except subprocess.TimeoutExpired:
        result["error_reason"] = "timeout"
        result["error"] = "iperf3 timed out after {}s".format(
            max(timeout, IPERF_SECONDS + 10))
        result["seconds"] = time.perf_counter() - started
    except Exception as exc:
        # A single unreachable peer must not stop the probe loop.
        result["error_reason"] = "probe_exception"
        result["error"] = str(exc)
        result["seconds"] = time.perf_counter() - started
    return result


def iperf_server_alive():
    """Return True when the local iperf3 server accepts TCP connections."""
    if not IPERF_SERVER_UP:
        return False
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", IPERF_PORT), timeout=0.5)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            sock.close()


def start_iperf_server():
    """Start the peer-facing iperf3 server in the background."""
    global IPERF_SERVER_UP
    command = ["iperf3", "-s", "-D", "-p", str(IPERF_PORT)]
    try:
        subprocess.run(
            command, check=True, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        IPERF_SERVER_UP = True
        log("iperf3 server listening on port {}".format(IPERF_PORT))
    except (OSError, subprocess.SubprocessError) as exc:
        IPERF_SERVER_UP = False
        log("failed to start iperf3 server: {}".format(exc))

# ---------------------------------------------------------------------------
# Peer discovery


def kubernetes_probe_pods():
    with open(SA_TOKEN, "r") as handle:
        token = handle.read().strip()
    # KUBERNETES_SERVICE_HOST/PORT are injected into every pod. The in-cluster
    # DNS name is a fallback for unusual setups.
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")

    selector = urllib.parse.urlencode(
        {"labelSelector": "app.kubernetes.io/name=network-mesh-probe"})
    url = "https://{}:{}/api/v1/namespaces/{}/pods?{}".format(
        host, port, urllib.parse.quote(POD_NAMESPACE, safe=""), selector)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer {}".format(token),
            "Accept": "application/json",
        },
    )
    context = ssl.create_default_context(cafile=SA_CA)
    with urllib.request.urlopen(request, timeout=10, context=context) as resp:
        payload = json.load(resp)

    candidates = []
    for item in payload.get("items", []):
        metadata = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        name = spec.get("nodeName")
        address = status.get("podIP")
        if not name or not address or status.get("phase") != "Running":
            continue
        ready = any(
            condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
        candidates.append((
            ready,
            metadata.get("creationTimestamp", ""),
            name,
            address,
        ))

    # A rolling update can briefly leave two probe pods on one node. Prefer a
    # Ready pod, then the newest one, and keep one peer per node.
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    peers = []
    seen = set()
    for ready, _, name, address in candidates:
        if name in seen:
            continue
        seen.add(name)
        peers.append({
            "name": name,
            "host": address,
            "port": PORT,
            "ready": ready,
        })
    return sorted(peers, key=lambda peer: peer["name"])


def discover_peers():
    if STATIC_PEERS:
        return [dict(peer) for peer in STATIC_PEERS]
    return kubernetes_probe_pods()


# ---------------------------------------------------------------------------
# Metrics state


METRIC_HELP = {
    "mesh_probe_info": "Static information about this mesh probe instance.",
    "mesh_probe_peers": "Number of peer nodes configured for this probe.",
    "mesh_probe_expected_peers": "Number of peer nodes expected in the mesh.",
    "mesh_probe_start_time_seconds": "Unix time at which the probe started.",
    "mesh_probe_last_cycle_timestamp_seconds":
        "Unix time at which the last throughput cycle finished.",
    "mesh_probe_last_ping_timestamp_seconds":
        "Unix time at which the last ping cycle finished.",
    "mesh_probe_last_transfer_timestamp_seconds":
        "Unix time at which the last throughput cycle finished.",
    "mesh_probe_cycle_duration_seconds":
        "Duration of the last throughput cycle in seconds.",
    "mesh_probe_pod_mtu": "MTU of the probe pod interface.",
    "mesh_test_active":
        "1 while a throughput test cycle is running, for alert inhibition.",
    "mesh_peer_ready": "Whether the peer probe pod reports Ready in Kubernetes.",
    "mesh_peer_unreachable":
        "1 when an expected peer is missing or not Ready.",
    "mesh_peer_info": "Peer probe pod address information.",
    "mesh_node_capacity_mbps": "Expected node NIC capacity in Mbit/s.",
    "mesh_iperf_server_up": "Whether the local iperf3 server is listening.",
    "mesh_ping_sent_packets": "ICMP echo requests sent to the peer.",
    "mesh_ping_received_packets": "ICMP echo replies received from the peer.",
    "mesh_ping_loss_percent": "ICMP packet loss towards the peer in percent.",
    "mesh_ping_success": "1 when every ICMP echo request was answered.",
    "mesh_ping_rtt_ms": "Average ICMP round-trip time in milliseconds.",
    "mesh_ping_rtt_min_ms": "Minimum ICMP round-trip time in milliseconds.",
    "mesh_ping_rtt_max_ms": "Maximum ICMP round-trip time in milliseconds.",
    "mesh_ping_rtt_mdev_ms":
        "Mean deviation of the ICMP round-trip times in milliseconds.",
    "mesh_transfer_success": "1 when the iperf3 test completed.",
    "mesh_transfer_mbps": "Measured iperf3 TCP throughput in Mbit/s.",
    "mesh_transfer_utilization_percent":
        "Throughput as a percentage of the slower endpoint's NIC capacity.",
    "mesh_transfer_error": "Last iperf3 failure reason for a direction.",
    "mesh_transfer_last_success_timestamp_seconds":
        "Unix time of the last successful iperf3 test for a direction.",
    "mesh_transfer_bytes": "Bytes received in the measured iperf3 interval.",
    "mesh_transfer_seconds":
        "Duration of the measured iperf3 interval in seconds.",
    "mesh_transfer_flows": "Parallel iperf3 streams used for the last transfer.",
    "mesh_last_test_timestamp_seconds":
        "Unix time at which the last test of this type finished.",
}


def escape_label(value):
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace('"', '\\"'))


def format_value(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return "{:.9g}".format(value)


class ProbeState(object):
    def __init__(self, node):
        self.node = node
        self.started_at = time.time()
        self.lock = threading.Lock()
        self.peers = []
        self.results = {}
        self.cycle_finished = None
        self.cycle_duration = None
        self.last_ping = None
        self.last_transfer = None
        self.test_active = 0
        self.expected_nodes = list(EXPECTED_NODES)
        self.capacities = dict(NODE_CAPACITIES)
        self.last_transfer_success = {}

    def update_peers(self, peers):
        # Keep results for peers that disappear from discovery.  Deleting them
        # made a node outage look like a smaller mesh with no red cells; the
        # renderer now emits an explicit unreachable series instead.
        with self.lock:
            self.peers = peers

    def set_test_active(self, active):
        with self.lock:
            self.test_active = 1 if active else 0

    def snapshot_peers(self):
        with self.lock:
            return list(self.peers)

    def set_ping(self, destination, result):
        with self.lock:
            pair = self.results.setdefault(destination, {})
            pair["ping"] = result
            pair["ping_time"] = time.time()

    def set_transfer(self, destination, direction, result):
        with self.lock:
            pair = self.results.setdefault(destination, {})
            pair.setdefault("transfer", {})[direction] = result
            pair[direction + "_time"] = time.time()
            if result.get("success"):
                self.last_transfer_success[
                    (destination, direction)] = time.time()

    def finish_ping(self):
        with self.lock:
            self.last_ping = time.time()

    def finish_cycle(self, duration):
        with self.lock:
            self.cycle_finished = time.time()
            self.last_transfer = self.cycle_finished
            self.cycle_duration = duration

    def render(self):
        samples = {}

        def add(name, labels, value):
            samples.setdefault(name, []).append((labels, value))

        with self.lock:
            peers = list(self.peers)
            results = {}
            for destination, pair in self.results.items():
                copied = {}
                for key, value in pair.items():
                    if isinstance(value, dict):
                        copied[key] = {
                            name: dict(item) if isinstance(item, dict)
                            else item
                            for name, item in value.items()
                        }
                    else:
                        copied[key] = value
                results[destination] = copied
            cycle_finished = self.cycle_finished
            cycle_duration = self.cycle_duration
            last_ping = self.last_ping
            last_transfer = self.last_transfer
            test_active = self.test_active
            expected_nodes = list(self.expected_nodes)
            capacities = dict(self.capacities)
            last_transfer_success = dict(self.last_transfer_success)

        peer_by_name = {
            peer["name"]: peer for peer in peers
            if peer["name"] != self.node
        }
        expected_targets = [
            name for name in expected_nodes if name != self.node
        ]
        if not expected_targets:
            expected_targets = sorted(peer_by_name)

        node_labels = {PROBE_LABEL: self.node}
        add("mesh_probe_info",
            dict(node_labels, version=VERSION, pod=POD_NAME), 1)
        add("mesh_probe_peers", node_labels, len(peer_by_name))
        add("mesh_probe_expected_peers", node_labels, len(expected_targets))
        add("mesh_probe_start_time_seconds", node_labels, self.started_at)
        add("mesh_test_active", node_labels, test_active)
        add("mesh_iperf_server_up", node_labels,
            1 if iperf_server_alive() else 0)
        try:
            with open("/sys/class/net/eth0/mtu", "r") as handle:
                pod_mtu = int(handle.read().strip())
        except (OSError, ValueError):
            pod_mtu = 0
        add("mesh_probe_pod_mtu", node_labels, pod_mtu)
        if cycle_finished is not None:
            add("mesh_probe_last_cycle_timestamp_seconds",
                node_labels, cycle_finished)
        if last_ping is not None:
            add("mesh_probe_last_ping_timestamp_seconds",
                node_labels, last_ping)
        if last_transfer is not None:
            add("mesh_probe_last_transfer_timestamp_seconds",
                node_labels, last_transfer)
        if cycle_duration is not None:
            add("mesh_probe_cycle_duration_seconds",
                node_labels, cycle_duration)

        for name in sorted(set(expected_targets) | set(peer_by_name)):
            peer = peer_by_name.get(name, {"name": name, "ready": False})
            ready = 1 if peer.get("ready") else 0
            labels = {"source": self.node, "peer": name}
            add("mesh_peer_ready", labels, ready)
            add("mesh_peer_unreachable", labels, 0 if ready else 1)
            if peer.get("host"):
                add("mesh_peer_info",
                    dict(labels, address=peer["host"],
                         port=peer.get("port", "")), 1)
            if name in capacities:
                add("mesh_node_capacity_mbps", {PROBE_LABEL: name},
                    capacities[name])
        if self.node in capacities:
            add("mesh_node_capacity_mbps", {PROBE_LABEL: self.node},
                capacities[self.node])

        destinations = sorted(set(results) | set(expected_targets))
        for destination in destinations:
            pair = results.get(destination, {})
            peer = peer_by_name.get(destination, {})
            ready = bool(peer.get("ready"))
            labels = {"source": self.node, "destination": destination}
            ping = pair.get("ping")
            if ping:
                add("mesh_ping_sent_packets", labels, ping.get("sent", 0))
                add("mesh_ping_received_packets", labels,
                    ping.get("received", 0))
                add("mesh_ping_loss_percent", labels, ping.get("loss", 100.0))
                add("mesh_ping_success", labels,
                    1 if ping.get("success") else 0)
                if "avg" in ping:
                    add("mesh_ping_rtt_ms", labels, ping["avg"])
                    add("mesh_ping_rtt_min_ms", labels, ping["min"])
                    add("mesh_ping_rtt_max_ms", labels, ping["max"])
                    add("mesh_ping_rtt_mdev_ms", labels, ping["mdev"])
                add("mesh_last_test_timestamp_seconds",
                    dict(labels, test="ping"), pair.get("ping_time", 0))
            elif not ready:
                add("mesh_ping_sent_packets", labels, 0)
                add("mesh_ping_received_packets", labels, 0)
                add("mesh_ping_loss_percent", labels, 100.0)
                add("mesh_ping_success", labels, 0)
                add("mesh_last_test_timestamp_seconds",
                    dict(labels, test="ping"), 0)

            for direction in ("upload", "download"):
                test = (pair.get("transfer") or {}).get(direction)
                direction_labels = dict(labels, direction=direction)
                last_success = last_transfer_success.get(
                    (destination, direction), 0)
                if test:
                    add("mesh_transfer_success", direction_labels,
                        1 if test.get("success") else 0)
                    if test.get("success"):
                        add("mesh_transfer_mbps", direction_labels,
                            test.get("mbps", 0.0))
                        source_capacity = capacities.get(self.node)
                        dest_capacity = capacities.get(destination)
                        if source_capacity and dest_capacity:
                            capacity = min(source_capacity, dest_capacity)
                            add("mesh_transfer_utilization_percent",
                                direction_labels,
                                100.0 * test.get("mbps", 0.0) / capacity)
                    elif test.get("error_reason"):
                        add("mesh_transfer_error",
                            dict(direction_labels,
                                 reason=test["error_reason"]), 1)
                    add("mesh_transfer_bytes", direction_labels,
                        test.get("bytes", 0))
                    add("mesh_transfer_seconds", direction_labels,
                        test.get("seconds", 0.0))
                    add("mesh_transfer_flows", direction_labels,
                        test.get("flows", 0))
                    add("mesh_last_test_timestamp_seconds",
                        dict(labels, test=direction),
                        pair.get(direction + "_time", 0))
                elif not ready:
                    add("mesh_transfer_success", direction_labels, 0)
                    add("mesh_last_test_timestamp_seconds",
                        dict(labels, test=direction), 0)
                if test or not ready:
                    add("mesh_transfer_last_success_timestamp_seconds",
                        direction_labels, last_success)

        lines = []
        for name in sorted(samples):
            lines.append("# HELP {} {}".format(name, METRIC_HELP[name]))
            lines.append("# TYPE {} gauge".format(name))
            for labels, value in sorted(
                    samples[name], key=lambda item: sorted(item[0].items())):
                label_text = ",".join(
                    '{}="{}"'.format(key, escape_label(labels[key]))
                    for key in sorted(labels)
                )
                lines.append("{}{{{}}} {}".format(
                    name, label_text, format_value(value)))
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTTP server


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mesh-probe/" + VERSION
    state = None

    def log_message(self, fmt, *args):
        return

    def _send(self, status, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/metrics":
            self._send(200, self.state.render(),
                       "text/plain; version=0.0.4; charset=utf-8")
        elif parsed.path == "/healthz":
            if iperf_server_alive():
                self._send(200, "ok\n")
            else:
                self._send(503, "iperf3 server is not listening\n")
        elif parsed.path == "/info":
            peers = self.state.snapshot_peers()
            payload = {
                "node": NODE_NAME,
                "node_ip": NODE_IP,
                "pod": POD_NAME,
                "version": VERSION,
                "ping_interval_seconds": PING_INTERVAL,
                "transfer_interval_seconds": TRANSFER_INTERVAL,
                "ping_count": PING_COUNT,
                "ping_interval_ms": PING_INTERVAL_MS * 1000.0,
                "ping_timeout_seconds": PING_TIMEOUT,
                "transfer_bytes": TRANSFER_BYTES,
                "transfer_flows": TRANSFER_FLOWS,
                "transfer_timeout_seconds": TRANSFER_TIMEOUT,
                "iperf_server_up": iperf_server_alive(),
                "peers": [
                    {"name": peer["name"], "host": peer["host"],
                     "port": peer["port"]}
                    for peer in peers if peer["name"] != NODE_NAME
                ],
            }
            self._send(200, json.dumps(payload, indent=2) + "\n",
                       "application/json")
        elif parsed.path == "/download":
            try:
                size = int(query.get("bytes", [TRANSFER_BYTES])[0])
            except (TypeError, ValueError):
                self._send(400, "invalid bytes parameter\n")
                return
            if size < 0 or size > MAX_TRANSFER_BYTES:
                self._send(400, "bytes must be between 0 and {}\n".format(
                    MAX_TRANSFER_BYTES))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Connection", "close")
            self.end_headers()
            remaining = size
            try:
                self.wfile.flush()
                if hasattr(self.connection, "sendfile"):
                    zero_file = ensure_zero_file()
                    with open(zero_file, "rb") as source:
                        while remaining > 0:
                            count = min(remaining, ZERO_FILE_SIZE)
                            sent = self.connection.sendfile(
                                source, 0, count)
                            if not sent:
                                break
                            remaining -= sent
                else:
                    while remaining > 0:
                        chunk = ZERO_BLOCK if remaining >= len(ZERO_BLOCK) \
                            else ZERO_BLOCK[:remaining]
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True
        else:
            self._send(404, "not found\n")

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/upload":
            self._send(404, "not found\n")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411, "Content-Length required\n")
            return
        if length < 0 or length > MAX_TRANSFER_BYTES:
            self._send(413, "request body too large\n")
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ConnectionError("request body ended early")
                remaining -= len(chunk)
        except (ConnectionError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        self._send(200, "received {}\n".format(length))


# ---------------------------------------------------------------------------
# Scheduler


class Scheduler(object):
    def __init__(self, state, stop):
        self.state = state
        self.stop = stop

    def refresh(self, previous):
        try:
            peers = discover_peers()
        except Exception as exc:
            log("peer discovery failed: {}".format(exc))
            return previous
        if not peers:
            log("peer discovery returned no nodes")
            return previous
        self.state.update_peers(peers)
        return peers

    def phase_offset(self, peers, interval):
        names = {peer["name"] for peer in peers}
        names.add(NODE_NAME)
        ordered = sorted(names)
        index = ordered.index(NODE_NAME)
        return interval * index / len(ordered)

    def next_run(self, now, offset, interval):
        slot = math.floor((now - offset) / interval) + 1
        return offset + slot * interval

    def run_ping_cycle(self, peers):
        started = time.time()
        targets = [peer for peer in peers if peer["name"] != NODE_NAME]
        ping_ok = 0
        for peer in targets:
            if self.stop.is_set():
                break
            result = ping(peer["host"])
            self.state.set_ping(peer["name"], result)
            if result["success"]:
                ping_ok += 1
            elif result.get("error"):
                log("ping {} failed: {}".format(
                    peer["name"], result["error"]))
        self.state.finish_ping()
        log("ping cycle finished: {}/{} reachable, {:.1f}s".format(
            ping_ok, len(targets), time.time() - started))

    def run_transfer_cycle(self, peers):
        started = time.time()
        targets = [peer for peer in peers if peer["name"] != NODE_NAME]
        transfers_ok = 0
        transfers_total = len(targets) * 2
        self.state.set_test_active(True)
        try:
            for peer in targets:
                if self.stop.is_set():
                    break
                destination = peer["name"]
                for direction in ("upload", "download"):
                    if self.stop.is_set():
                        break
                    test = transfer(peer["host"], direction)
                    self.state.set_transfer(destination, direction, test)
                    if test["success"]:
                        transfers_ok += 1
                    elif test.get("error"):
                        log("{} {} failed ({}): {}".format(
                            direction, destination,
                            test.get("error_reason", "unknown"),
                            test["error"]))
        finally:
            self.state.set_test_active(False)
        duration = time.time() - started
        self.state.finish_cycle(duration)
        log("throughput cycle finished: {}/{} transfers, {:.1f}s".format(
            transfers_ok, transfers_total, duration))

    def run(self):
        peers = None
        next_ping = None
        next_transfer = None
        while not self.stop.is_set():
            if not peers:
                peers = self.refresh(peers)
                if not peers:
                    self.stop.wait(10)
                    continue

            now = time.time()
            if next_ping is None:
                # Give the other probes time to start, then phase-shift the
                # first ping and throughput cycles independently.
                delay = 5 + stable_jitter(NODE_NAME, 15)
                next_ping = now + delay + self.phase_offset(
                    peers, PING_INTERVAL)
                next_transfer = now + delay + self.phase_offset(
                    peers, TRANSFER_INTERVAL)
                log("first ping in {:.0f}s, first transfer in {:.0f}s".format(
                    next_ping - now, next_transfer - now))

            wake = min(next_ping, next_transfer)
            wait = wake - now
            if wait > 0 and self.stop.wait(wait):
                break

            refreshed = self.refresh(peers)
            if not refreshed:
                next_ping = time.time() + 10
                next_transfer = time.time() + 10
                continue
            peers = refreshed

            now = time.time()
            if now >= next_ping:
                self.run_ping_cycle(peers)
                next_ping = self.next_run(
                    time.time(), self.phase_offset(peers, PING_INTERVAL),
                    PING_INTERVAL)
                while next_ping < time.time() + 5:
                    next_ping += PING_INTERVAL

            now = time.time()
            if now >= next_transfer:
                self.run_transfer_cycle(peers)
                next_transfer = self.next_run(
                    time.time(),
                    self.phase_offset(peers, TRANSFER_INTERVAL),
                    TRANSFER_INTERVAL)
                while next_transfer < time.time() + 30:
                    next_transfer += TRANSFER_INTERVAL


def main():
    log("starting mesh-probe {} on {} (port {}, ping {:.0f}s, "
        "transfer {:.0f}s, iperf3 {}s x {} flows)".format(
            VERSION, NODE_NAME, PORT, PING_INTERVAL, TRANSFER_INTERVAL,
            IPERF_SECONDS, TRANSFER_FLOWS))
    if not STATIC_PEERS and not os.path.exists(SA_TOKEN):
        log("warning: {} is missing; waiting for Kubernetes API".format(
            SA_TOKEN))

    start_iperf_server()
    state = ProbeState(NODE_NAME)
    Handler.state = state
    server = ThreadingHTTPServer(("", PORT), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, name="http", daemon=True)
    thread.start()

    stop = threading.Event()

    def shutdown(signum, _frame):
        log("received signal {}, shutting down".format(signum))
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        Scheduler(state, stop).run()
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
