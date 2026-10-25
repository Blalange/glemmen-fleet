"""Subprocess and cluster-secret helpers."""

from __future__ import annotations

import base64
import json
import subprocess


def run(cmd: list[str], *, capture: bool = True) -> str:
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=capture,
        text=True,
        encoding="utf-8",
    )
    return result.stdout

def kubectl_command(kubectl: str, kubeconfig: str | None, *args: str) -> list[str]:
    cmd = [kubectl]
    if kubeconfig:
        cmd.extend(["--kubeconfig", kubeconfig])
    cmd.extend(args)
    return cmd

def read_cluster_secret(kubectl: str, kubeconfig: str | None) -> tuple[str, str]:
    raw = run(
        kubectl_command(
            kubectl,
            kubeconfig,
            "-n",
            "monitoring",
            "get",
            "secret",
            "unpoller-unifi",
            "-o",
            "json",
        )
    )
    secret = json.loads(raw)
    data = secret.get("data") or {}
    try:
        username = base64.b64decode(data["username"]).decode()
        password = base64.b64decode(data["password"]).decode()
    except KeyError as exc:
        raise SystemExit(f"unpoller-unifi Secret is missing {exc}") from exc
    return username, password
