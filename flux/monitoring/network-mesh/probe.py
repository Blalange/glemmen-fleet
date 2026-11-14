#!/usr/bin/env python3
"""Node-to-node network probe for the glemmen cluster.

Runs as a DaemonSet with host networking, so exactly one instance runs on each
node. Every instance periodically:

  * pings every other node with ICMP echo requests,
  * uploads a block of zeroes to every other node over HTTP,
  * downloads a block of zeroes from every other node over HTTP,

and exposes the results as Prometheus metrics on /metrics. Peers are discovered
from the Kubernetes API (or from STATIC_PEERS when running outside the
cluster).

The HTTP server also implements the two transfer endpoints used by the tests:

  GET  /download?bytes=N  returns N zero bytes
  POST /upload            reads and discards the request body
  GET  /metrics           Prometheus text exposition
  GET  /healthz           liveness/readiness endpoint
  GET  /info              probe configuration as JSON

Only the Python standard library is used, so the container image can be
python:3.13-slim with the script mounted from a ConfigMap.
"""

import concurrent.futures
import http.client
import json
import math
import os
import random
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
SA_TOKEN = os.path.join(SA_DIR, "token")
SA_CA = os.path.join(SA_DIR, "ca.crt")

ZERO_BLOCK = b"\0" * (256 * 1024)
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
PORT = env_int("PORT", DEFAULT_PORT)
INTERVAL = max(30.0, env_float("INTERVAL_SECONDS", 300.0))
PING_COUNT = max(1, env_int("PING_COUNT", 4))
PING_INTERVAL = max(0.05, env_float("PING_INTERVAL_MS", 200.0) / 1000.0)
PING_TIMEOUT = max(0.1, env_float("PING_TIMEOUT_SECONDS", 1.0))
TRANSFER_BYTES = max(1024, env_int("TRANSFER_BYTES", 32 * 1024 * 1024))
TRANSFER_TIMEOUT = max(1.0, env_float("TRANSFER_TIMEOUT_SECONDS", 15.0))
TRANSFER_FLOWS = max(1, min(64, env_int("TRANSFER_FLOWS", 8)))
MAX_TRANSFER_BYTES = 1024 * 1024 * 1024
STATIC_PEERS_RAW = os.environ.get("STATIC_PEERS", "")

PROBE_LABEL = "node"


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


def ping(host, count=PING_COUNT, interval=PING_INTERVAL,
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
        result["success"] = len(rtts) == count
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


class ZeroStream(object):
    """Iterable body that produces exactly `size` zero bytes."""

    def __init__(self, size):
        self.size = size

    def __iter__(self):
        remaining = self.size
        while remaining > 0:
            chunk = ZERO_BLOCK if remaining >= len(ZERO_BLOCK) \
                else ZERO_BLOCK[:remaining]
            remaining -= len(chunk)
            yield chunk

    def __len__(self):
        return self.size


def _transfer_one(host, port, direction, size, timeout):
    """Run one HTTP upload or download flow."""
    result = {
        "direction": direction,
        "bytes": size,
        "success": False,
        "error": None,
    }
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    started = time.perf_counter()
    try:
        if direction == "download":
            path = "/download?bytes={}".format(size)
            conn.request("GET", path, headers={"Connection": "close"})
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError("HTTP {}".format(response.status))
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
            result["bytes"] = received
            result["success"] = received == size
            if not result["success"]:
                result["error"] = "received {} of {} bytes".format(
                    received, size)
        else:
            headers = {
                "Content-Length": str(size),
                "Content-Type": "application/octet-stream",
                "Connection": "close",
            }
            conn.request("POST", "/upload", body=ZeroStream(size),
                         headers=headers)
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                raise RuntimeError("HTTP {}: {}".format(
                    response.status, body[:200].decode(errors="replace")))
            result["success"] = True
    except (OSError, http.client.HTTPException, RuntimeError) as exc:
        result["error"] = str(exc)
    finally:
        conn.close()

    elapsed = time.perf_counter() - started
    result["seconds"] = elapsed
    if result["success"]:
        result["mbps"] = (result["bytes"] * 8.0) / (elapsed * 1_000_000.0)
    return result


def transfer(host, port, direction, size=TRANSFER_BYTES,
             timeout=TRANSFER_TIMEOUT):
    """Run parallel HTTP flows and return aggregate throughput."""
    flows = max(1, min(TRANSFER_FLOWS, size)) if size > 0 else 1
    base, extra = divmod(size, flows)
    chunks = [base + (1 if index < extra else 0) for index in range(flows)]

    started = time.perf_counter()
    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=flows) as pool:
            futures = [
                pool.submit(_transfer_one, host, port, direction, chunk,
                            timeout)
                for chunk in chunks
            ]
            for future in futures:
                results.append(future.result())
    except Exception as exc:
        results.append({"bytes": 0, "success": False, "error": str(exc)})

    elapsed = time.perf_counter() - started
    total_bytes = sum(result.get("bytes", 0) for result in results)
    errors = [result.get("error") for result in results
              if result.get("error")]
    success = (
        len(results) == len(chunks)
        and all(result.get("success") for result in results)
        and total_bytes == size
    )
    result = {
        "direction": direction,
        "bytes": total_bytes,
        "success": success,
        "error": errors[0] if errors
        else (None if success else "incomplete transfer"),
        "seconds": elapsed,
        "flows": flows,
    }
    if success:
        result["mbps"] = (total_bytes * 8.0) / (elapsed * 1_000_000.0)
    return result


# ---------------------------------------------------------------------------
# Peer discovery


def kubernetes_nodes():
    with open(SA_TOKEN, "r") as handle:
        token = handle.read().strip()
    # KUBERNETES_SERVICE_HOST/PORT are injected by the kubelet for hostNetwork
    # pods too. The in-cluster DNS name is a fallback for unusual setups.
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")

    selector = urllib.parse.urlencode(
        {"labelSelector": "kubernetes.io/os=linux"})
    url = "https://{}:{}/api/v1/nodes?{}".format(host, port, selector)
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

    peers = []
    for item in payload.get("items", []):
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        name = metadata.get("name")
        if not name:
            continue
        addresses = [
            address.get("address")
            for address in status.get("addresses") or []
            if address.get("type") == "InternalIP" and address.get("address")
        ]
        ipv4 = [address for address in addresses if ":" not in address]
        address = ipv4[0] if ipv4 else (addresses[0] if addresses else None)
        if not address:
            continue
        ready = any(
            condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
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
    return kubernetes_nodes()


# ---------------------------------------------------------------------------
# Metrics state


METRIC_HELP = {
    "mesh_probe_info": "Static information about this mesh probe instance.",
    "mesh_probe_peers": "Number of peer nodes configured for this probe.",
    "mesh_probe_start_time_seconds": "Unix time at which the probe started.",
    "mesh_probe_last_cycle_timestamp_seconds":
        "Unix time at which the last test cycle finished.",
    "mesh_probe_cycle_duration_seconds":
        "Duration of the last test cycle in seconds.",
    "mesh_peer_ready": "Whether the peer node reports Ready in Kubernetes.",
    "mesh_peer_info": "Peer node address information.",
    "mesh_ping_sent_packets": "ICMP echo requests sent to the peer.",
    "mesh_ping_received_packets": "ICMP echo replies received from the peer.",
    "mesh_ping_loss_percent": "ICMP packet loss towards the peer in percent.",
    "mesh_ping_success": "1 when every ICMP echo request was answered.",
    "mesh_ping_rtt_ms": "Average ICMP round-trip time in milliseconds.",
    "mesh_ping_rtt_min_ms": "Minimum ICMP round-trip time in milliseconds.",
    "mesh_ping_rtt_max_ms": "Maximum ICMP round-trip time in milliseconds.",
    "mesh_ping_rtt_mdev_ms":
        "Mean deviation of the ICMP round-trip times in milliseconds.",
    "mesh_transfer_success": "1 when the HTTP transfer completed.",
    "mesh_transfer_mbps": "Measured HTTP transfer throughput in Mbit/s.",
    "mesh_transfer_bytes": "Bytes transferred in the last HTTP test.",
    "mesh_transfer_seconds": "Duration of the last HTTP transfer in seconds.",
    "mesh_transfer_flows": "Parallel HTTP flows used for the last transfer.",
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

    def update_peers(self, peers):
        with self.lock:
            self.peers = peers
            valid = {
                peer["name"] for peer in peers
                if peer["name"] != self.node
            }
            for destination in list(self.results):
                if destination not in valid:
                    del self.results[destination]

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

    def finish_cycle(self, duration):
        with self.lock:
            self.cycle_finished = time.time()
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

        targets = [peer for peer in peers if peer["name"] != self.node]
        add("mesh_probe_info",
            {PROBE_LABEL: self.node, "version": VERSION}, 1)
        add("mesh_probe_peers", {PROBE_LABEL: self.node}, len(targets))
        add("mesh_probe_start_time_seconds",
            {PROBE_LABEL: self.node}, self.started_at)
        if cycle_finished is not None:
            add("mesh_probe_last_cycle_timestamp_seconds",
                {PROBE_LABEL: self.node}, cycle_finished)
        if cycle_duration is not None:
            add("mesh_probe_cycle_duration_seconds",
                {PROBE_LABEL: self.node}, cycle_duration)

        for peer in peers:
            if peer["name"] == self.node:
                continue
            add("mesh_peer_ready",
                {"source": self.node, "peer": peer["name"]},
                1 if peer.get("ready") else 0)
            add("mesh_peer_info",
                {"source": self.node, "peer": peer["name"],
                 "address": peer["host"], "port": peer["port"]}, 1)

        for destination in sorted(results):
            pair = results[destination]
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
            for direction in ("upload", "download"):
                test = (pair.get("transfer") or {}).get(direction)
                if not test:
                    continue
                direction_labels = dict(labels, direction=direction)
                add("mesh_transfer_success", direction_labels,
                    1 if test.get("success") else 0)
                if test.get("success"):
                    add("mesh_transfer_mbps", direction_labels,
                        test.get("mbps", 0.0))
                add("mesh_transfer_bytes", direction_labels,
                    test.get("bytes", 0))
                add("mesh_transfer_seconds", direction_labels,
                    test.get("seconds", 0.0))
                add("mesh_transfer_flows", direction_labels,
                    test.get("flows", 0))
                add("mesh_last_test_timestamp_seconds",
                    dict(labels, test=direction),
                    pair.get(direction + "_time", 0))

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
            self._send(200, "ok\n")
        elif parsed.path == "/info":
            peers = self.state.snapshot_peers()
            payload = {
                "node": NODE_NAME,
                "node_ip": NODE_IP,
                "version": VERSION,
                "interval_seconds": INTERVAL,
                "ping_count": PING_COUNT,
                "ping_interval_ms": PING_INTERVAL * 1000.0,
                "ping_timeout_seconds": PING_TIMEOUT,
                "transfer_bytes": TRANSFER_BYTES,
                "transfer_flows": TRANSFER_FLOWS,
                "transfer_timeout_seconds": TRANSFER_TIMEOUT,
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
                while remaining > 0:
                    chunk = ZERO_BLOCK if remaining >= len(ZERO_BLOCK) \
                        else ZERO_BLOCK[:remaining]
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
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

    def phase_offset(self, peers):
        names = {peer["name"] for peer in peers}
        names.add(NODE_NAME)
        ordered = sorted(names)
        index = ordered.index(NODE_NAME)
        return INTERVAL * index / len(ordered)

    def next_run(self, now, offset):
        slot = math.floor((now - offset) / INTERVAL) + 1
        return offset + slot * INTERVAL

    def run_cycle(self, peers):
        started = time.time()
        targets = [peer for peer in peers if peer["name"] != NODE_NAME]
        ping_ok = 0
        transfers_ok = 0
        transfers_total = len(targets) * 2
        for peer in targets:
            if self.stop.is_set():
                break
            destination = peer["name"]
            ping_result = ping(peer["host"])
            self.state.set_ping(destination, ping_result)
            if ping_result["success"]:
                ping_ok += 1
            elif ping_result.get("error"):
                log("ping {} failed: {}".format(
                    destination, ping_result["error"]))
            for direction in ("upload", "download"):
                if self.stop.is_set():
                    break
                test = transfer(peer["host"], peer["port"], direction)
                self.state.set_transfer(destination, direction, test)
                if test["success"]:
                    transfers_ok += 1
                elif test.get("error"):
                    log("{} {} failed: {}".format(
                        direction, destination, test["error"]))
        duration = time.time() - started
        self.state.finish_cycle(duration)
        log("cycle finished: {}/{} ping, {}/{} transfers, {:.1f}s".format(
            ping_ok, len(targets), transfers_ok, transfers_total, duration))

    def run(self):
        peers = None
        next_run = None
        while not self.stop.is_set():
            if not peers:
                peers = self.refresh(peers)
                if not peers:
                    self.stop.wait(10)
                    continue

            now = time.time()
            if next_run is None:
                # Give the other probes time to start before the first
                # cluster-wide test burst.
                delay = 5 + stable_jitter(NODE_NAME, 25)
                next_run = now + delay
                log("first test cycle in {:.0f}s".format(delay))

            wait = next_run - now
            if wait > 0 and self.stop.wait(wait):
                break

            refreshed = self.refresh(peers)
            if not refreshed:
                next_run = time.time() + 10
                continue
            peers = refreshed
            self.run_cycle(peers)
            now = time.time()
            next_run = self.next_run(now, self.phase_offset(peers))
            # The first cycle starts at an arbitrary phase; avoid following
            # it with a second cycle only a second or two later. Also skip
            # whole slots if a cycle overran its interval.
            while next_run < now + 10:
                next_run += INTERVAL


def main():
    log("starting mesh-probe {} on {} (port {}, interval {:.0f}s, "
        "{} bytes/transfer, {} flows)".format(
            VERSION, NODE_NAME, PORT, INTERVAL, TRANSFER_BYTES,
            TRANSFER_FLOWS))
    if not STATIC_PEERS and not os.path.exists(SA_TOKEN):
        log("warning: {} is missing; waiting for Kubernetes API".format(
            SA_TOKEN))

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
