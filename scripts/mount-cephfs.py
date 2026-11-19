#!/usr/bin/env python3
"""Mount the cluster's CephFS on this machine.

Reads the monitor endpoints, FSID and CephFS CSI node key from the live
cluster, then mounts the filesystem with mount.ceph (kernel client) or
ceph-fuse (userspace client). The local machine must be able to reach the
Ceph mons and OSDs on the server VLAN (192.168.6.0/24).

The kernel client requires root and mount.ceph. The userspace client works as
a normal user and needs ceph-fuse, /dev/fuse and fuse3; it is selected
automatically when the script is not run as root. The CSI node key is written
to a mode-600 temporary file and removed after the mount. Install `ceph` on
Arch, or `ceph-common` and `ceph-fuse` on Debian/Ubuntu/Fedora.

Examples:
  sudo ./scripts/mount-cephfs.py /mnt/cephfs
  ./scripts/mount-cephfs.py --pvc media-data-ceph ~/media
  ./scripts/mount-cephfs.py --fuse --read-only --subdir /volumes ~/ceph
  sudo ./scripts/mount-cephfs.py --unmount /mnt/cephfs

Use --print to resolve the cluster details and show the mount command without
mounting anything.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KUBECTL = ROOT / "scripts" / "bin" / "kubectl"
DEFAULT_KUBECONFIG = ROOT / "talos" / "generated" / "kubeconfig"
DEFAULT_MOUNTPOINT = Path("/mnt/cephfs")
DEFAULT_FS = "cephfs"
ROOK_NAMESPACE = "rook-ceph"
CEPHFS_SECRET = "rook-csi-cephfs-node"
CEPHFS_DRIVER = "rook-ceph.cephfs.csi.ceph.com"
MOUNT_CEPH_CANDIDATES = (
    "mount.ceph",
    "/sbin/mount.ceph",
    "/usr/sbin/mount.ceph",
    "/usr/bin/mount.ceph",
)
CEPH_FUSE_CANDIDATES = (
    "ceph-fuse",
    "/usr/bin/ceph-fuse",
    "/usr/sbin/ceph-fuse",
)


def die(message: str) -> None:
    print("ERROR: {}".format(message), file=sys.stderr)
    raise SystemExit(1)


def kubectl(kubectl_path: str, kubeconfig: Path, args: list[str]) -> str:
    command = [kubectl_path, "--kubeconfig", str(kubeconfig), *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        die("kubectl {} failed: {}".format(" ".join(args), detail))
    return result.stdout


def kubectl_json(kubectl_path: str, kubeconfig: Path, args: list[str]) -> dict:
    return json.loads(kubectl(kubectl_path, kubeconfig, [*args, "-o", "json"]))


def find_kubectl(override: str | None) -> str:
    if override:
        path = Path(override)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        found = shutil.which(override)
        if found:
            return found
        die("kubectl not found: {}".format(override))

    if DEFAULT_KUBECTL.is_file() and os.access(DEFAULT_KUBECTL, os.X_OK):
        return str(DEFAULT_KUBECTL)
    found = shutil.which("kubectl")
    if found:
        return found
    die(
        "kubectl not found; run ./scripts/download-tools.py kubectl or pass "
        "--kubectl"
    )


def find_mount_ceph(required: bool = True) -> str | None:
    candidates: list[str] = []
    if os.environ.get("MOUNT_CEPH"):
        candidates.append(os.environ["MOUNT_CEPH"])
    candidates.extend(MOUNT_CEPH_CANDIDATES)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute():
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            continue
        found = shutil.which(candidate)
        if found:
            return found

    if required:
        die(
            "mount.ceph not found; install the Ceph client tools:\n"
            "  Arch:          sudo pacman -S ceph\n"
            "  Debian/Ubuntu: sudo apt install ceph-common\n"
            "  Fedora:        sudo dnf install ceph-common"
        )
    return None


def find_ceph_fuse(required: bool = True) -> str | None:
    candidates: list[str] = []
    if os.environ.get("CEPH_FUSE"):
        candidates.append(os.environ["CEPH_FUSE"])
    candidates.extend(CEPH_FUSE_CANDIDATES)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute():
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            continue
        found = shutil.which(candidate)
        if found:
            return found

    if required:
        die(
            "ceph-fuse not found; install the userspace Ceph client:\n"
            "  Arch:          sudo pacman -S ceph\n"
            "  Debian/Ubuntu: sudo apt install ceph-fuse\n"
            "  Fedora:        sudo dnf install ceph-fuse"
        )
    return None


def get_monitors(kubectl_path: str, kubeconfig: Path) -> list[str]:
    configmap = kubectl_json(
        kubectl_path,
        kubeconfig,
        ["-n", ROOK_NAMESPACE, "get", "configmap", "rook-ceph-mon-endpoints"],
    )
    raw = (configmap.get("data") or {}).get("data", "")
    monitors: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        _, separator, address = entry.partition("=")
        if not separator or not address:
            die("unexpected monitor entry: {!r}".format(entry))
        monitors.append(address)

    if not monitors:
        die("no Ceph monitors found in rook-ceph-mon-endpoints")
    return sorted(set(monitors))


def get_fsid(kubectl_path: str, kubeconfig: Path) -> str:
    cluster = kubectl_json(
        kubectl_path,
        kubeconfig,
        ["-n", ROOK_NAMESPACE, "get", "cephcluster", "rook-ceph"],
    )
    fsid = ((cluster.get("status") or {}).get("ceph") or {}).get("fsid")
    if fsid:
        return str(fsid)

    # Fall back to the mon secret, which Rook also populates with the FSID.
    secret = kubectl_json(
        kubectl_path,
        kubeconfig,
        ["-n", ROOK_NAMESPACE, "get", "secret", "rook-ceph-mon"],
    )
    encoded = (secret.get("data") or {}).get("fsid")
    if encoded:
        try:
            decoded = base64.b64decode(encoded).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError):
            decoded = ""
        if decoded:
            return decoded

    die("cannot determine the Ceph FSID from the CephCluster status or mon secret")


def get_credentials(kubectl_path: str, kubeconfig: Path) -> tuple[str, str]:
    secret = kubectl_json(
        kubectl_path,
        kubeconfig,
        ["-n", ROOK_NAMESPACE, "get", "secret", CEPHFS_SECRET],
    )
    data = secret.get("data") or {}
    try:
        user = base64.b64decode(data["userID"]).decode("utf-8").strip()
        key = base64.b64decode(data["userKey"]).decode("utf-8").strip()
    except (KeyError, binascii.Error, UnicodeDecodeError) as exc:
        die("{} is missing a valid userID/userKey: {}".format(CEPHFS_SECRET, exc))

    if not user or not key:
        die("{} has an empty userID or userKey".format(CEPHFS_SECRET))
    return user, key


def resolve_pvc(
    kubectl_path: str, kubeconfig: Path, namespace: str, name: str
) -> tuple[str, str]:
    pvc = kubectl_json(
        kubectl_path, kubeconfig, ["-n", namespace, "get", "pvc", name]
    )
    pv_name = (pvc.get("spec") or {}).get("volumeName")
    if not pv_name:
        die("PVC {}/{} is not bound".format(namespace, name))

    pv = kubectl_json(kubectl_path, kubeconfig, ["get", "pv", pv_name])
    csi = (pv.get("spec") or {}).get("csi") or {}
    if csi.get("driver") != CEPHFS_DRIVER:
        die(
            "PVC {}/{} is not backed by the Rook CephFS driver (got {!r})".format(
                namespace, name, csi.get("driver")
            )
        )

    attributes = csi.get("volumeAttributes") or {}
    fs_name = attributes.get("fsName")
    subdir = attributes.get("subvolumePath")
    if not fs_name or not subdir:
        die("PV {} has no CephFS fsName/subvolumePath".format(pv_name))
    return str(fs_name), str(subdir)


def normalize_subdir(path: str) -> str:
    path = path.strip()
    if not path or path == "/":
        return "/"
    return "/" + path.strip("/")


def check_monitors(monitors: list[str]) -> None:
    reachable: set[str] = set()
    for monitor in monitors:
        host, separator, port = monitor.rpartition(":")
        if not separator:
            host, port = monitor, "3300"
        host = host.strip("[]")
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                reachable.add(monitor)
        except (OSError, ValueError):
            continue

    if not reachable:
        die(
            "none of the Ceph monitors are reachable: {}".format(
                ", ".join(monitors)
            )
        )
    for monitor in monitors:
        if monitor not in reachable:
            print(
                "WARNING: monitor {} is not reachable".format(monitor),
                file=sys.stderr,
            )


def mountpoint_path(value: Path) -> Path:
    path = value.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def kernel_mount_options(monitors: list[str], keyfile: Path, read_only: bool,
                         extra: list[str]) -> str:
    options = [
        "mon_addr={}".format("/".join(monitors)),
        "secretfile={}".format(keyfile),
        "noatime",
    ]
    if read_only:
        options.append("ro")
    options.extend(extra)
    return ",".join(options)


def fuse_mount_options(read_only: bool, extra: list[str]) -> list[str]:
    options: list[str] = []
    if read_only:
        options.append("ro")
    options.extend(extra)
    return options


def ensure_mountpoint(mountpoint: Path) -> None:
    if mountpoint.exists() and not mountpoint.is_dir():
        die("{} exists and is not a directory".format(mountpoint))
    try:
        mountpoint.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        die("cannot create mountpoint {}: {}".format(mountpoint, exc))
    if os.path.ismount(mountpoint):
        die(
            "{} is already a mountpoint; use --unmount first".format(mountpoint)
        )


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def print_resolved(args: argparse.Namespace, mountpoint: Path, fs: str,
                   subdir: str, user: str, fsid: str,
                   monitors: list[str], mode: str) -> int:
    if mode == "fuse":
        found = find_ceph_fuse(required=False)
        ceph_fuse = found or "ceph-fuse"
        command = [
            ceph_fuse,
            "-n",
            "client.{}".format(user),
            "-c",
            "<ceph.conf>",
            "-k",
            "<keyring>",
            "--client_fs",
            fs,
        ]
        if subdir != "/":
            command.extend(["--client_mountpoint", subdir])
        options = fuse_mount_options(args.read_only, args.option)
        if options:
            command.extend(["-o", ",".join(options)])
        command.append(str(mountpoint))
        print("Mode:       ceph-fuse (userspace)")
        print("CephFS:     {} (fsid {})".format(fs, fsid))
        print("Monitors:   {}".format(", ".join(monitors)))
        print(
            "CephX user: {} (key from {}/{})".format(
                user, ROOK_NAMESPACE, CEPHFS_SECRET
            )
        )
        print("Source:     {}".format(subdir))
        print("Mountpoint: {}".format(mountpoint))
        print("Command:")
        print("  " + shlex.join(command))
        if not found:
            print(
                "\nceph-fuse is not installed locally; install the userspace "
                "Ceph client before running the command."
            )
        return 0

    found = find_mount_ceph(required=False)
    mount_ceph = found or "mount.ceph"
    device = "{}@{}.{}={}".format(user, fsid, fs, subdir)
    options = kernel_mount_options(
        monitors, Path("<key-file>"), args.read_only, args.option
    )
    print("Mode:       mount.ceph (kernel)")
    print("CephFS:     {} (fsid {})".format(fs, fsid))
    print("Monitors:   {}".format(", ".join(monitors)))
    print(
        "CephX user: {} (key from {}/{})".format(
            user, ROOK_NAMESPACE, CEPHFS_SECRET
        )
    )
    print("Source:     {}".format(subdir))
    print("Mountpoint: {}".format(mountpoint))
    print("Command:")
    command = [mount_ceph, device, str(mountpoint), "-o", options]
    print("  " + shlex.join(command))
    if not found:
        print(
            "\nmount.ceph is not installed locally; install the Ceph client "
            "tools before running the command."
        )
    return 0


def do_mount_kernel(args: argparse.Namespace, mountpoint: Path, fs: str,
                    subdir: str, user: str, key: str, fsid: str,
                    monitors: list[str]) -> int:
    if os.geteuid() != 0:
        command = shlex.join(
            ["sudo", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
        )
        die("mounting requires root; re-run with:\n  {}".format(command))

    mount_ceph = find_mount_ceph()
    ensure_mountpoint(mountpoint)
    device = "{}@{}.{}={}".format(user, fsid, fs, subdir)
    with tempfile.TemporaryDirectory(prefix="glemmen-cephfs-") as temporary:
        keyfile = Path(temporary) / "key"
        write_private(keyfile, key + "\n")
        options = kernel_mount_options(
            monitors, keyfile, args.read_only, args.option
        )
        command = [mount_ceph, device, str(mountpoint), "-o", options]
        print("Mounting {} at {} ...".format(fs, mountpoint), flush=True)
        result = subprocess.run(command, text=True)
        if result.returncode != 0:
            die("mount.ceph failed with exit code {}".format(result.returncode))

    if not os.path.ismount(mountpoint):
        die(
            "mount.ceph returned success but {} is not a mountpoint".format(
                mountpoint
            )
        )
    print("Mounted {} at {}".format(fs, mountpoint))
    return 0


def do_mount_fuse(args: argparse.Namespace, mountpoint: Path, fs: str,
                  subdir: str, user: str, key: str, fsid: str,
                  monitors: list[str]) -> int:
    ceph_fuse = find_ceph_fuse()
    fuse_device = Path("/dev/fuse")
    if not fuse_device.exists():
        die(
            "/dev/fuse not found; load the fuse module or install fuse3 "
            "before mounting with ceph-fuse"
        )
    if not os.access(fuse_device, os.R_OK | os.W_OK):
        die(
            "/dev/fuse is not accessible; add the user to the fuse group or "
            "adjust the device permissions"
        )
    if os.geteuid() != 0 and not (
        shutil.which("fusermount3") or shutil.which("fusermount")
    ):
        die(
            "fusermount3 not found; install fuse3 to mount as a non-root "
            "user, or run the script with sudo for the kernel client"
        )

    ensure_mountpoint(mountpoint)
    with tempfile.TemporaryDirectory(prefix="glemmen-cephfs-") as temporary:
        config = Path(temporary) / "ceph.conf"
        keyring = Path(temporary) / "keyring"
        write_private(
            keyring,
            "[client.{}]\n    key = {}\n".format(user, key),
        )
        write_private(
            config,
            "[global]\n"
            "    fsid = {}\n"
            "    mon_host = {}\n"
            "    auth_client_required = cephx\n"
            "\n"
            "[client.{}]\n"
            "    keyring = {}\n".format(
                fsid, ",".join(monitors), user, keyring
            ),
        )
        command = [
            ceph_fuse,
            "-n",
            "client.{}".format(user),
            "-c",
            str(config),
            "-k",
            str(keyring),
            "--client_fs",
            fs,
        ]
        if subdir != "/":
            command.extend(["--client_mountpoint", subdir])
        options = fuse_mount_options(args.read_only, args.option)
        if options:
            command.extend(["-o", ",".join(options)])
        command.append(str(mountpoint))

        print(
            "Mounting {} at {} with ceph-fuse ...".format(fs, mountpoint),
            flush=True,
        )
        result = subprocess.run(command, text=True)
        if result.returncode != 0:
            die("ceph-fuse failed with exit code {}".format(result.returncode))

        deadline = time.monotonic() + 10
        while not os.path.ismount(mountpoint) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not os.path.ismount(mountpoint):
            die(
                "ceph-fuse returned success but {} is not a mountpoint".format(
                    mountpoint
                )
            )

    print("Mounted {} at {} (ceph-fuse)".format(fs, mountpoint))
    return 0


def mount_fstype(mountpoint: Path) -> str:
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return ""
    result = subprocess.run(
        [findmnt, "-T", str(mountpoint), "-o", "FSTYPE", "-n"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def do_unmount(mountpoint: Path) -> int:
    if not mountpoint.exists():
        print("{} does not exist; nothing to unmount".format(mountpoint))
        return 0
    if not os.path.ismount(mountpoint):
        print("{} is not a mountpoint".format(mountpoint))
        return 0

    fstype = mount_fstype(mountpoint)
    if fstype.startswith("fuse"):
        fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
        if not fusermount:
            if os.geteuid() != 0:
                die(
                    "fusermount3 not found; install fuse3 to unmount this "
                    "FUSE mount, or run the script with sudo"
                )
            fusermount = ""
        if fusermount:
            result = subprocess.run(
                [fusermount, "-u", str(mountpoint)], text=True
            )
            if result.returncode != 0:
                die(
                    "fusermount failed with exit code {}".format(
                        result.returncode
                    )
                )
            print("Unmounted {}".format(mountpoint))
            return 0

    if os.geteuid() != 0:
        command = shlex.join(
            ["sudo", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
        )
        die("unmounting requires root; re-run with:\n  {}".format(command))

    umount = shutil.which("umount")
    if not umount:
        die("umount not found")
    result = subprocess.run([umount, str(mountpoint)], text=True)
    if result.returncode != 0:
        die("umount failed with exit code {}".format(result.returncode))
    print("Unmounted {}".format(mountpoint))
    return 0


def do_status(mountpoint: Path) -> int:
    if not mountpoint.exists() or not os.path.ismount(mountpoint):
        print("{} is not a mountpoint".format(mountpoint))
        return 1

    findmnt = shutil.which("findmnt")
    if findmnt:
        result = subprocess.run(
            [
                findmnt,
                "-T",
                str(mountpoint),
                "-o",
                "TARGET,SOURCE,FSTYPE,OPTIONS",
                "-n",
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip()
        if output:
            print(output)
            return 0
    print("{} is a mountpoint".format(mountpoint))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "mountpoint",
        nargs="?",
        type=Path,
        help=(
            "mountpoint (default: {} as root, ~/cephfs otherwise)".format(
                DEFAULT_MOUNTPOINT
            )
        ),
    )
    parser.add_argument(
        "--kubectl", help="kubectl binary (default: scripts/bin/kubectl or PATH)"
    )
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        default=DEFAULT_KUBECONFIG,
        help="kubeconfig (default: talos/generated/kubeconfig)",
    )
    parser.add_argument(
        "--fs",
        help="CephFS name (default: {}, or the PVC's filesystem)".format(
            DEFAULT_FS
        ),
    )
    parser.add_argument(
        "--pvc",
        help="mount the CephFS subvolume for this PVC instead of the filesystem root",
    )
    parser.add_argument(
        "-n",
        "--namespace",
        default="media",
        help="namespace for --pvc (default: media)",
    )
    parser.add_argument(
        "--subdir",
        help="mount a subdirectory of the CephFS instead of the root",
    )
    parser.add_argument(
        "--fuse",
        action="store_true",
        help=(
            "use ceph-fuse instead of the kernel client "
            "(automatic when not running as root)"
        ),
    )
    parser.add_argument(
        "-r",
        "--read-only",
        action="store_true",
        help="mount read-only",
    )
    parser.add_argument(
        "-o",
        "--option",
        action="append",
        default=[],
        help="extra mount option (repeatable)",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show the resolved mount command without mounting",
    )
    parser.add_argument(
        "--unmount",
        action="store_true",
        help="unmount the mountpoint",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show whether the mountpoint is mounted",
    )
    args = parser.parse_args()

    if args.unmount and args.status:
        parser.error("--unmount and --status are mutually exclusive")
    if args.print_only and (args.unmount or args.status):
        parser.error("--print cannot be combined with --unmount or --status")
    if args.pvc and args.subdir:
        parser.error("--pvc and --subdir are mutually exclusive")

    if args.mountpoint is None:
        mountpoint = (
            DEFAULT_MOUNTPOINT
            if os.geteuid() == 0
            else Path.home() / "cephfs"
        )
    else:
        mountpoint = mountpoint_path(args.mountpoint)
    if args.unmount:
        return do_unmount(mountpoint)
    if args.status:
        return do_status(mountpoint)

    kubeconfig = args.kubeconfig.expanduser()
    if not kubeconfig.is_file():
        die(
            "kubeconfig not found: {}; run ./scripts/talos/gen-config.py".format(
                kubeconfig
            )
        )

    kubectl_path = find_kubectl(args.kubectl)
    monitors = get_monitors(kubectl_path, kubeconfig)
    fsid = get_fsid(kubectl_path, kubeconfig)
    user, key = get_credentials(kubectl_path, kubeconfig)

    fs = args.fs or DEFAULT_FS
    subdir = normalize_subdir(args.subdir or "/")
    if args.pvc:
        pvc_fs, pvc_subdir = resolve_pvc(
            kubectl_path, kubeconfig, args.namespace, args.pvc
        )
        if args.fs and args.fs != pvc_fs:
            die(
                "--fs {} does not match the PVC's filesystem {}".format(
                    args.fs, pvc_fs
                )
            )
        fs = pvc_fs
        subdir = normalize_subdir(pvc_subdir)

    check_monitors(monitors)
    mode = "fuse" if args.fuse or os.geteuid() != 0 else "kernel"
    if args.print_only:
        return print_resolved(
            args, mountpoint, fs, subdir, user, fsid, monitors, mode
        )
    if mode == "fuse":
        return do_mount_fuse(
            args, mountpoint, fs, subdir, user, key, fsid, monitors
        )
    return do_mount_kernel(
        args, mountpoint, fs, subdir, user, key, fsid, monitors
    )


if __name__ == "__main__":
    sys.exit(main())
