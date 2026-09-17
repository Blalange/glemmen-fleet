#!/usr/bin/env python3
"""Measure latency, packet loss and throughput between every pair of cluster nodes.

Merged network test with three subcommands:

  ping   SSH into every node, ping every other node and collect RTT/loss.
  iperf  Run iperf3 between every ordered pair (TCP + UDP, both directions).
  all    Run both tests in sequence.

The tests log in to the nodes over SSH, so results reflect the Netbird mesh
paths between the nodes. ".blalange.intra" is stripped from host names in
console output and heatmaps. The underlay addresses Netbird selects for each
node pair (`netbird status --json`) are captured and written to the CSV files
as netbird_local / netbird_remote / netbird_conn, and rendered to an endpoint
table image. Results are written as CSV files plus heatmap "graph tables" with
the node IDs on both axes.

Requires SSH key auth (BatchMode) and iputils ping / iperf3 on the nodes;
matplotlib is only needed for the heatmaps.

Run:  python3 network-test.py all
      python3 network-test.py ping --count 20
      python3 network-test.py iperf --time 10 --resolve-locally --accept-host-keys
"""

import argparse
import csv
import json
import math
import re
import shlex
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

DEFAULT_HOSTS = [
    "glemmen130.blalange.intra",
    "glemmen150.blalange.intra",
    "glemmen160.blalange.intra",
    "glemmen170.blalange.intra",
    "glemmen180.blalange.intra",
    "glemmen80.blalange.intra",
]

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

DISPLAY_SUFFIX = ".blalange.intra"

NETBIRD_MARKER = "###NETBIRD"

SECTION_RE = re.compile(r"^###TARGET (\S+)\s*$")
TX_RX_RE = re.compile(r"(\d+) packets transmitted, (\d+) received")
LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
RTT_RE = re.compile(
    r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")

PROTOCOLS = ("tcp", "udp")
DIRECTIONS = ("src_to_dst", "dst_to_src")


@dataclass
class PingResult:
    source: str
    destination: str
    transmitted: Optional[int] = None
    received: Optional[int] = None
    loss_percent: Optional[float] = None
    min_ms: Optional[float] = None
    avg_ms: Optional[float] = None
    max_ms: Optional[float] = None
    mdev_ms: Optional[float] = None
    netbird_local: Optional[str] = None
    netbird_remote: Optional[str] = None
    netbird_conn: Optional[str] = None
    error: Optional[str] = None


@dataclass
class IperfResult:
    source: str
    destination: str
    protocol: str
    direction: str
    sender_mbps: Optional[float] = None
    receiver_mbps: Optional[float] = None
    throughput_mbps: Optional[float] = None
    jitter_ms: Optional[float] = None
    lost_packets: Optional[int] = None
    packets: Optional[int] = None
    lost_percent: Optional[float] = None
    retransmits: Optional[int] = None
    netbird_local: Optional[str] = None
    netbird_remote: Optional[str] = None
    netbird_conn: Optional[str] = None
    error: Optional[str] = None


def add_common_args(parser):
    parser.add_argument(
        "--hosts",
        default=",".join(DEFAULT_HOSTS),
        help="comma-separated node list (default: %(default)s)",
    )
    parser.add_argument(
        "--ssh-user",
        default="user",
        help="SSH user to log in as on every node (default: %(default)s)",
    )
    parser.add_argument(
        "--resolve-locally",
        action="store_true",
        help="resolve the host names to IP addresses on this machine and use "
             "those IPs as the test targets (SSH still uses the host names)",
    )
    parser.add_argument(
        "--accept-host-keys",
        action="store_true",
        help="automatically accept and trust unknown SSH host keys "
             "(StrictHostKeyChecking=accept-new)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
        help="directory for CSV and PNG output (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-plot",
        action="store_true",
        help="only write CSV files, do not render the heatmaps or the "
             "Netbird endpoint table images",
    )


def add_ping_args(parser):
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="ICMP echo requests per node pair (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="seconds between pings (default: %(default)s)",
    )
    parser.add_argument(
        "--ping-timeout",
        type=float,
        default=1.0,
        help="seconds to wait for each reply (default: %(default)s)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="source nodes to test in parallel, 0 = one job per node "
             "(default: %(default)s)",
    )


def add_iperf_args(parser):
    parser.add_argument(
        "--port",
        type=int,
        default=5201,
        help="iperf3 server port (default: %(default)s)",
    )
    parser.add_argument(
        "--time",
        type=float,
        default=5.0,
        help="seconds per iperf3 test (default: %(default)s)",
    )
    parser.add_argument(
        "--udp-bandwidth",
        default="1G",
        help="target bandwidth for the UDP loss tests, e.g. 100M or 1G "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=2,
        help="parallel iperf3 streams per test (default: %(default)s)",
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    add_common_args(common)
    ping_options = argparse.ArgumentParser(add_help=False)
    add_ping_args(ping_options)
    iperf_options = argparse.ArgumentParser(add_help=False)
    add_iperf_args(iperf_options)

    subparsers.add_parser(
        "ping", parents=[common, ping_options], help="latency and packet loss test")
    subparsers.add_parser(
        "iperf", parents=[common, iperf_options], help="TCP/UDP throughput and loss test")
    subparsers.add_parser(
        "all", parents=[common, ping_options, iperf_options],
        help="run the ping test followed by the iperf test")
    return parser.parse_args(argv)


def ssh_options(args):
    options = list(SSH_OPTIONS)
    if args.accept_host_keys:
        options += ["-o", "StrictHostKeyChecking=accept-new"]
    return options


def ssh_run(host, script, args, timeout=None):
    target = "{}@{}".format(args.ssh_user, host) if args.ssh_user else host
    command = ["ssh", *ssh_options(args), target, script]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def display_name(host):
    if host.endswith(DISPLAY_SUFFIX):
        return host[: -len(DISPLAY_SUFFIX)]
    return host


def resolve_addresses(hosts, resolve_locally):
    addresses = {}
    for host in hosts:
        if not resolve_locally:
            addresses[host] = host
            continue
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            print("warning: could not resolve {} locally ({}), "
                  "using the host name".format(host, exc), file=sys.stderr)
            addresses[host] = host
            continue
        addresses[host] = infos[0][4][0]
    return addresses


def parse_netbird_status(output):
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != NETBIRD_MARKER:
            continue
        payload = []
        for line in lines[index + 1:]:
            if line.startswith("###TARGET"):
                break
            payload.append(line)
        try:
            return json.loads("\n".join(payload))
        except ValueError:
            return None
    return None


def fetch_netbird_status(host, args):
    try:
        process = ssh_run(host, "netbird status --json", args, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    try:
        return json.loads(process.stdout)
    except ValueError:
        return None


def peer_endpoint(status, host):
    if not status:
        return None, None, None
    short = host.split(".")[0]
    for peer in (status.get("peers") or {}).get("details") or []:
        fqdn = peer.get("fqdn") or ""
        if fqdn == host or fqdn.split(".")[0] == short:
            endpoint = peer.get("iceCandidateEndpoint") or {}
            return (endpoint.get("local"), endpoint.get("remote"),
                    peer.get("connectionType"))
    return None, None, None


def endpoint_rows(results):
    seen = []
    for result in results:
        key = (result.source, result.destination, result.netbird_local,
               result.netbird_remote, result.netbird_conn)
        if key not in seen:
            seen.append(key)
    return [
        (display_name(source), display_name(destination),
         local or "?", remote or "?", conn or "")
        for source, destination, local, remote, conn in seen
    ]


def print_netbird_endpoints(rows):
    if not any(local != "?" or remote != "?" for _, _, local, remote, _ in rows):
        return
    print()
    print("Netbird underlay endpoints selected per pair:")
    for source, destination, local, remote, conn in rows:
        print("  {} -> {}: {} -> {}{}".format(
            source, destination, local, remote,
            " ({})".format(conn) if conn else ""))


def render_endpoint_table(rows, path, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("warning: matplotlib is not installed, skipping endpoint table "
              "(install with: pip install matplotlib)", file=sys.stderr)
        return False
    if not rows:
        return False

    columns = ["source", "destination", "netbird local", "netbird remote", "type"]
    figure, ax = plt.subplots(figsize=(11.5, 0.33 * len(rows) + 1.8))
    ax.axis("off")
    table = ax.table(cellText=[list(row) for row in rows], colLabels=columns,
                     loc="center", cellLoc="left", colLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.auto_set_column_width(col=list(range(len(columns))))
    table.scale(1, 1.45)
    for (row, _), cell in table.get_celld().items():
        if row == 0:
            cell.set_text_props(weight="bold")
            cell.set_facecolor("#e8e8e8")
    ax.set_title(title, pad=14)
    figure.tight_layout()
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)
    return True


def build_remote_script(targets, addresses, count, interval, ping_timeout):
    lines = [
        "echo '{}'".format(NETBIRD_MARKER),
        "netbird status --json 2>/dev/null || true",
        "echo",
    ]
    for target in targets:
        lines.append("echo '###TARGET {}'".format(target))
        lines.append(
            "ping -n -c {count} -i {interval:g} -W {timeout:g} {target} 2>&1 || true".format(
                count=count,
                interval=interval,
                timeout=ping_timeout,
                target=shlex.quote(addresses[target]),
            )
        )
    return "\n".join(lines)


def split_sections(output):
    sections = {}
    current = None
    for line in output.splitlines():
        match = SECTION_RE.match(line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def parse_ping(source, destination, text):
    result = PingResult(source=source, destination=destination)
    tx_rx = TX_RX_RE.search(text)
    if tx_rx:
        result.transmitted = int(tx_rx.group(1))
        result.received = int(tx_rx.group(2))
    loss = LOSS_RE.search(text)
    if loss:
        result.loss_percent = float(loss.group(1))
    rtt = RTT_RE.search(text)
    if rtt:
        result.min_ms, result.avg_ms, result.max_ms, result.mdev_ms = (
            float(group) for group in rtt.groups()
        )
    if result.loss_percent is None:
        lines = [line for line in text.splitlines() if line.strip()]
        result.error = lines[-1][:200] if lines else "no ping output"
    return result


def ping_from_source(source, targets, args, addresses):
    script = build_remote_script(
        targets, addresses, args.count, args.interval, args.ping_timeout)
    timeout = len(targets) * (args.count *
                              (args.interval + args.ping_timeout) + 5) + 30
    try:
        process = ssh_run(source, script, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [PingResult(source, target, error="ssh command timed out")
                for target in targets]
    if process.returncode == 255 or "###TARGET" not in process.stdout:
        message = process.stderr.strip().splitlines()
        message = message[-1][:200] if message else "ssh failed (exit {})".format(
            process.returncode)
        return [PingResult(source, target, error=message) for target in targets]

    sections = split_sections(process.stdout)
    status = parse_netbird_status(process.stdout)
    results = []
    for target in targets:
        if target not in sections:
            result = PingResult(source, target, error="no ping output")
        else:
            result = parse_ping(
                source, target, "\n".join(sections[target]))
        result.netbird_local, result.netbird_remote, result.netbird_conn = \
            peer_endpoint(status, target)
        results.append(result)
    return results


def run_ping_test(hosts, addresses, args):
    jobs = args.jobs if args.jobs > 0 else len(hosts)
    pairs = len(hosts) * (len(hosts) - 1)
    print("ping test: {} nodes, {} ordered pairs, {} parallel source node(s)".format(
        len(hosts), pairs, jobs))

    results = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {}
        for source in hosts:
            targets = [host for host in hosts if host != source]
            futures[pool.submit(ping_from_source, source,
                                targets, args, addresses)] = source
        for future in as_completed(futures):
            source = futures[future]
            try:
                source_results = future.result()
            except Exception as exc:
                source_results = [
                    PingResult(source, target,
                               error="unexpected error: {}".format(exc))
                    for target in hosts if target != source
                ]
            failed = [result for result in source_results if result.error]
            if failed:
                print("  {}: {} target(s) failed ({})".format(
                    display_name(source), len(failed), failed[0].error))
            else:
                print("  {}: ok".format(display_name(source)))
            results.extend(source_results)

    order = {host: position for position, host in enumerate(hosts)}
    results.sort(key=lambda result: (
        order[result.source], order[result.destination]))

    latency_matrix = build_matrix(hosts, results, "avg_ms")
    loss_matrix = build_matrix(hosts, results, "loss_percent")

    output_dir = args.output_dir
    write_ping_raw_csv(output_dir / "ping_raw.csv", results)
    write_matrix_csv(output_dir / "ping_latency_matrix.csv",
                     hosts, latency_matrix)
    write_matrix_csv(output_dir / "ping_loss_matrix.csv", hosts, loss_matrix)

    print()
    print("ping: wrote {} results to {}".format(len(results), output_dir))
    labels = [display_name(host) for host in hosts]
    print_matrix(labels, latency_matrix, "Average RTT (ms)", "{:.2f}")
    print_matrix(labels, loss_matrix, "Packet loss (%)", "{:.2f}")
    rows = endpoint_rows(results)
    print_netbird_endpoints(rows)

    if not args.skip_plot and rows:
        path = output_dir / "ping-netbird-endpoints.png"
        title = "Netbird underlay endpoints - ping test - {}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M"))
        if render_endpoint_table(rows, path, title):
            print("endpoint table: {}".format(path))

    if not args.skip_plot:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        panels = [
            {
                "title": "Average RTT (ms)",
                "matrix": latency_matrix,
                "cmap": "YlOrRd",
                "label": "ms",
                "fmt": "{:.2f}",
                "vmin": 0.0,
            },
            {
                "title": "Packet loss (%)",
                "matrix": loss_matrix,
                "cmap": "Reds",
                "label": "%",
                "fmt": "{:.2f}",
                "vmin": 0.0,
            },
        ]
        path = output_dir / "ping-heatmap.png"
        title = "Cluster ping test - {} - {} pings/pair".format(
            timestamp, args.count)
        if render_heatmaps(labels, panels, path, title):
            print("heatmap: {}".format(path))

    return 1 if any(result.error for result in results) else 0


def server_pidfile(args):
    return "/tmp/iperf3-test-{}.pid".format(args.port)


def server_logfile(args):
    return "/tmp/iperf3-test-{}.log".format(args.port)


def start_server(host, args):
    script = (
        "if [ -f {pid} ]; then kill $(cat {pid}) 2>/dev/null; rm -f {pid}; fi; "
        "rm -f {log}; "
        "iperf3 -s -D -p {port} --pidfile {pid} --logfile {log}; "
        "sleep 0.5; "
        "if [ -s {pid} ] && kill -0 $(cat {pid}) 2>/dev/null; then echo SERVER_STARTED; "
        "else echo SERVER_FAILED; cat {log} 2>/dev/null; fi"
    ).format(
        pid=shlex.quote(server_pidfile(args)),
        log=shlex.quote(server_logfile(args)),
        port=args.port,
    )
    try:
        process = ssh_run(host, script, args, timeout=30)
    except subprocess.TimeoutExpired:
        print("  warning: timed out starting iperf3 server on {}".format(
            display_name(host)), file=sys.stderr)
        return False
    if "SERVER_STARTED" in process.stdout:
        print("  iperf3 server started on {}".format(display_name(host)))
        return True
    output = (process.stdout + process.stderr).strip()
    print("  warning: could not start iperf3 server on {}: {}".format(
        display_name(host), output[-300:]), file=sys.stderr)
    return False


def stop_servers(hosts, args):
    script = (
        "if [ -f {pid} ]; then kill $(cat {pid}) 2>/dev/null; rm -f {pid}; fi; "
        "echo SERVER_STOPPED"
    ).format(pid=shlex.quote(server_pidfile(args)))
    for host in hosts:
        try:
            ssh_run(host, script, args, timeout=20)
        except (subprocess.TimeoutExpired, OSError):
            pass


def to_mbps(bits_per_second):
    if bits_per_second is None:
        return None
    return float(bits_per_second) / 1_000_000.0


def parse_iperf_json(source, destination, protocol, direction, payload):
    result = IperfResult(source=source, destination=destination,
                         protocol=protocol, direction=direction)
    end = payload.get("end") or {}
    sent = end.get("sum_sent") or {}
    received = end.get("sum_received") or {}
    summary = end.get("sum") or {}
    result.sender_mbps = to_mbps(sent.get("bits_per_second"))
    result.receiver_mbps = to_mbps(received.get("bits_per_second"))
    if protocol == "udp":
        result.throughput_mbps = (
            result.receiver_mbps if result.receiver_mbps is not None
            else result.sender_mbps
        )
        result.jitter_ms = summary.get("jitter_ms")
        result.lost_packets = summary.get("lost_packets")
        result.packets = summary.get("packets")
        result.lost_percent = summary.get("lost_percent")
    else:
        result.throughput_mbps = (
            result.sender_mbps if direction == "src_to_dst"
            else result.receiver_mbps
        )
        result.retransmits = sent.get("retransmits")
    if result.throughput_mbps is None and result.error is None:
        result.error = "iperf3 output did not contain throughput"
    return result


def run_client_test(source, destination, args, protocol, direction, addresses):
    command = ["iperf3", "-c", addresses[destination], "-p", str(args.port),
               "-t", "{:g}".format(args.time), "-J"]
    if args.parallel > 1:
        command += ["-P", str(args.parallel)]
    if protocol == "udp":
        command += ["-u", "-b", str(args.udp_bandwidth)]
    if direction == "dst_to_src":
        command.append("-R")
    remote = " ".join(shlex.quote(part) for part in command)
    try:
        process = ssh_run(source, remote, args, timeout=args.time + 60)
    except subprocess.TimeoutExpired:
        return IperfResult(source, destination, protocol, direction,
                           error="ssh command timed out")
    if process.returncode != 0:
        message = (process.stderr.strip() or process.stdout.strip()
                   or "iperf3 exited with {}".format(process.returncode))
        return IperfResult(source, destination, protocol, direction,
                           error=message.splitlines()[-1][:200])
    try:
        payload = json.loads(process.stdout)
    except ValueError:
        return IperfResult(source, destination, protocol, direction,
                           error="could not parse iperf3 JSON output")
    return parse_iperf_json(source, destination, protocol, direction, payload)


def run_tests(hosts, args, servers, addresses):
    total = len(hosts) * (len(hosts) - 1) * len(PROTOCOLS) * len(DIRECTIONS)
    results = []
    index = 0
    for source in hosts:
        for destination in hosts:
            if source == destination:
                continue
            status = fetch_netbird_status(source, args)
            netbird_local, netbird_remote, netbird_conn = peer_endpoint(
                status, destination)
            if netbird_local or netbird_remote:
                print("  netbird {} -> {}: {} -> {}{}".format(
                    display_name(source), display_name(destination),
                    netbird_local or "?", netbird_remote or "?",
                    " ({})".format(netbird_conn) if netbird_conn else ""),
                    flush=True)
            for protocol in PROTOCOLS:
                for direction in DIRECTIONS:
                    index += 1
                    if direction == "src_to_dst":
                        label = "{} -> {}".format(
                            display_name(source), display_name(destination))
                    else:
                        label = "{} -> {}".format(
                            display_name(destination), display_name(source))
                    print("[{}/{}] {} {}".format(index,
                          total, label, protocol), flush=True)
                    if source not in servers or destination not in servers:
                        result = IperfResult(source, destination, protocol,
                                             direction,
                                             error="iperf3 server not running")
                    else:
                        result = run_client_test(
                            source, destination, args, protocol, direction,
                            addresses)
                    result.netbird_local = netbird_local
                    result.netbird_remote = netbird_remote
                    result.netbird_conn = netbird_conn
                    if result.error:
                        print("    error: {}".format(result.error),
                              file=sys.stderr, flush=True)
                    else:
                        detail = "{:.1f} Mbit/s".format(result.throughput_mbps)
                        if protocol == "udp" and result.lost_percent is not None:
                            detail += ", {:.2f}% loss".format(result.lost_percent)
                        print("    {}".format(detail), flush=True)
                    results.append(result)
    return results


def run_iperf_test(hosts, addresses, args):
    tests = len(hosts) * (len(hosts) - 1) * len(PROTOCOLS) * len(DIRECTIONS)
    print("iperf test: {} nodes, {} ordered pairs, {} tests of {:g}s each".format(
        len(hosts), len(hosts) * (len(hosts) - 1), tests, args.time))

    servers = [host for host in hosts if start_server(host, args)]
    if not servers:
        print("error: no iperf3 servers could be started", file=sys.stderr)
        return 1

    try:
        results = run_tests(hosts, args, servers, addresses)
    finally:
        stop_servers(hosts, args)

    tcp_up = build_matrix(hosts, results, "throughput_mbps",
                          "tcp", "src_to_dst")
    tcp_down = build_matrix(hosts, results, "throughput_mbps",
                            "tcp", "dst_to_src")
    udp_up_loss = build_matrix(
        hosts, results, "lost_percent", "udp", "src_to_dst")
    udp_down_loss = build_matrix(
        hosts, results, "lost_percent", "udp", "dst_to_src")

    output_dir = args.output_dir
    write_iperf_raw_csv(output_dir / "iperf_raw.csv", results)
    write_matrix_csv(
        output_dir / "iperf_tcp_src_to_dst_mbps.csv", hosts, tcp_up)
    write_matrix_csv(
        output_dir / "iperf_tcp_dst_to_src_mbps.csv", hosts, tcp_down)
    write_matrix_csv(
        output_dir / "iperf_udp_src_to_dst_loss_percent.csv", hosts, udp_up_loss)
    write_matrix_csv(
        output_dir / "iperf_udp_dst_to_src_loss_percent.csv", hosts, udp_down_loss)

    print()
    print("iperf: wrote {} results to {}".format(len(results), output_dir))
    labels = [display_name(host) for host in hosts]
    print_matrix(labels, tcp_up, "TCP throughput src -> dst (Mbit/s)", "{:.0f}")
    print_matrix(labels, udp_up_loss,
                 "UDP packet loss src -> dst (%)", "{:.2f}")
    rows = endpoint_rows(results)
    print_netbird_endpoints(rows)

    if not args.skip_plot and rows:
        path = output_dir / "iperf-netbird-endpoints.png"
        title = "Netbird underlay endpoints - iperf test - {}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M"))
        if render_endpoint_table(rows, path, title):
            print("endpoint table: {}".format(path))

    if not args.skip_plot:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        panels = [
            {
                "title": "TCP throughput src -> dst",
                "matrix": tcp_up,
                "cmap": "viridis",
                "label": "Mbit/s",
                "fmt": "{:.0f}",
                "vmin": 0.0,
            },
            {
                "title": "TCP throughput dst -> src",
                "matrix": tcp_down,
                "cmap": "viridis",
                "label": "Mbit/s",
                "fmt": "{:.0f}",
                "vmin": 0.0,
            },
            {
                "title": "UDP packet loss src -> dst",
                "matrix": udp_up_loss,
                "cmap": "Reds",
                "label": "%",
                "fmt": "{:.2f}",
                "vmin": 0.0,
            },
            {
                "title": "UDP packet loss dst -> src",
                "matrix": udp_down_loss,
                "cmap": "Reds",
                "label": "%",
                "fmt": "{:.2f}",
                "vmin": 0.0,
            },
        ]
        path = output_dir / "iperf-heatmap.png"
        title = "Cluster iperf3 test - {} - {:g}s/test, UDP {}".format(
            timestamp, args.time, args.udp_bandwidth)
        if render_heatmaps(labels, panels, path, title):
            print("heatmap: {}".format(path))

    return 1 if any(result.error for result in results) else 0


def build_matrix(hosts, results, attribute, protocol=None, direction=None):
    index = {host: position for position, host in enumerate(hosts)}
    matrix = [[None] * len(hosts) for _ in hosts]
    for result in results:
        if protocol is not None and result.protocol != protocol:
            continue
        if direction is not None and result.direction != direction:
            continue
        matrix[index[result.source]][index[result.destination]
                                     ] = getattr(result, attribute)
    return matrix


def format_value(value, spec="{:.3f}"):
    return "" if value is None else spec.format(value)


def write_ping_raw_csv(path, results):
    fieldnames = [
        "source", "destination", "transmitted", "received", "loss_percent",
        "min_ms", "avg_ms", "max_ms", "mdev_ms", "netbird_local",
        "netbird_remote", "netbird_conn", "error",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fieldnames)
        for result in results:
            writer.writerow([
                result.source,
                result.destination,
                "" if result.transmitted is None else result.transmitted,
                "" if result.received is None else result.received,
                format_value(result.loss_percent),
                format_value(result.min_ms),
                format_value(result.avg_ms),
                format_value(result.max_ms),
                format_value(result.mdev_ms),
                result.netbird_local or "",
                result.netbird_remote or "",
                result.netbird_conn or "",
                result.error or "",
            ])


def write_iperf_raw_csv(path, results):
    fieldnames = [
        "source", "destination", "protocol", "direction", "sender_mbps",
        "receiver_mbps", "throughput_mbps", "jitter_ms", "lost_packets",
        "packets", "lost_percent", "retransmits", "netbird_local",
        "netbird_remote", "netbird_conn", "error",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fieldnames)
        for result in results:
            writer.writerow([
                result.source,
                result.destination,
                result.protocol,
                result.direction,
                format_value(result.sender_mbps),
                format_value(result.receiver_mbps),
                format_value(result.throughput_mbps),
                format_value(result.jitter_ms),
                "" if result.lost_packets is None else result.lost_packets,
                "" if result.packets is None else result.packets,
                format_value(result.lost_percent),
                "" if result.retransmits is None else result.retransmits,
                result.netbird_local or "",
                result.netbird_remote or "",
                result.netbird_conn or "",
                result.error or "",
            ])


def write_matrix_csv(path, hosts, matrix):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source\\destination", *hosts])
        for host, row in zip(hosts, matrix):
            writer.writerow([host, *[format_value(value) for value in row]])


def print_matrix(hosts, matrix, title, spec):
    width = max(max(len(host) for host in hosts), len("src\\dst")) + 2
    print()
    print(title)
    print("src\\dst".ljust(width) + "".join(host.rjust(width) for host in hosts))
    for host, row in zip(hosts, matrix):
        cells = ["-" if value is None else spec.format(value) for value in row]
        print(host.ljust(width) + "".join(cell.rjust(width) for cell in cells))


def render_heatmaps(hosts, panels, path, title, columns=2):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("warning: matplotlib is not installed, skipping heatmaps "
              "(install with: pip install matplotlib)", file=sys.stderr)
        return False

    count = len(hosts)
    cell = 1.4
    rows = math.ceil(len(panels) / columns)
    fig, axes = plt.subplots(
        rows,
        columns,
        figsize=((count * cell + 2.0) * columns, (count * cell + 2.4) * rows),
        squeeze=False,
    )
    for index, panel in enumerate(panels):
        ax = axes[index // columns][index % columns]
        values = np.array(
            [[math.nan if value is None else float(value) for value in row]
             for row in panel["matrix"]],
            dtype=float,
        )
        vmin = panel.get("vmin")
        vmax = panel.get("vmax")
        if vmax is None:
            finite = values[~np.isnan(values)]
            vmax = float(finite.max()) if finite.size else 1.0
            if vmax <= (vmin or 0.0):
                vmax = (vmin or 0.0) + 1.0
        image = ax.imshow(
            np.ma.masked_invalid(values),
            cmap=panel.get("cmap", "viridis"),
            vmin=vmin,
            vmax=vmax,
            aspect="equal",
        )
        for row in range(count):
            for column in range(count):
                value = values[row, column]
                if math.isnan(value):
                    ax.text(column, row, "-", ha="center", va="center",
                            color="0.45", fontsize=9)
                    continue
                rgba = image.cmap(image.norm(value))
                luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
                color = "black" if luminance > 0.5 else "white"
                ax.text(column, row, panel["fmt"].format(value), ha="center",
                        va="center", color=color, fontsize=9)
        ax.set_xticks(range(count))
        ax.set_xticklabels(hosts, rotation=45, ha="right", fontsize=9)
        ax.set_yticks(range(count))
        ax.set_yticklabels(hosts, fontsize=9)
        ax.set_xlabel("destination node")
        ax.set_ylabel("source node")
        ax.set_title(panel["title"])
        colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
        colorbar.set_label(panel["label"])

    for index in range(len(panels), rows * columns):
        axes[index // columns][index % columns].axis("off")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


def main(argv=None):
    args = parse_args(argv)
    hosts = [host.strip() for host in args.hosts.split(",") if host.strip()]
    if len(hosts) < 2:
        print("error: at least two hosts are required", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    addresses = resolve_addresses(hosts, args.resolve_locally)
    if args.resolve_locally:
        for host in hosts:
            print("  {} -> {}".format(display_name(host), addresses[host]))

    exit_code = 0
    if args.command in ("ping", "all"):
        exit_code |= run_ping_test(hosts, addresses, args)
    if args.command in ("iperf", "all"):
        exit_code |= run_iperf_test(hosts, addresses, args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
