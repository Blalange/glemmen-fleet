#!/usr/bin/env python3
"""Find outdated Talos, Kubernetes, Flux, Helm chart and container-image pins.

The script is read-only. It compares the versions in talos/settings.env and the
GitOps manifests with the latest upstream releases/tags.

Usage:
  ./scripts/check-updates.py
  ./scripts/check-updates.py --only talos --only kubernetes
  ./scripts/check-updates.py --no-images
  ./scripts/check-updates.py --json
  ./scripts/check-updates.py --fail-on-updates

Environment:
  GITHUB_TOKEN    optional, avoids GitHub API rate limits
  DOCKER_USERNAME / DOCKER_PASSWORD
                  optional, for private registries
  CHECK_UPDATES_TIMEOUT
                  optional HTTP timeout in seconds (default: 20)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

USER_AGENT = "glemmen-fleet-check-updates"
CATEGORIES = ("talos", "kubernetes", "flux", "helm", "images", "tuppr")
EXCLUDED_PREFIXES = (
    "talos/generated/",
    "talos/bin/",
    "scripts/bin/",
    "scripts/results/",
    "flux/clusters/glemmen/flux-system/",
)
EXCLUDED_DIRS = {".git", "__pycache__", ".venv", "results"}


@dataclass
class UpdateResult:
    category: str
    name: str
    current: str | None
    latest: str | None
    status: str  # update | ok | tracking | unknown
    source: str = ""
    detail: str = ""
    files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedVersion:
    raw: str
    prefix: str
    numbers: tuple[int, ...]
    suffix: str


@dataclass(frozen=True)
class ImageRef:
    raw: str
    registry: str
    repository: str
    tag: str | None
    digest: str | None


VERSION_RE = re.compile(
    r"^(?P<prefix>v?)(?P<numbers>\d+(?:\.\d+)*)(?P<suffix>[-+].*)?$"
)
PRERELEASE_RE = re.compile(r"(?:alpha|beta|rc|pre|dev|nightly)", re.IGNORECASE)
YAML_SUFFIXES = {".yaml", ".yml"}


def parse_version(raw: str | None) -> ParsedVersion | None:
    if not raw:
        return None
    match = VERSION_RE.match(raw.strip())
    if not match:
        return None
    return ParsedVersion(
        raw=raw.strip(),
        prefix=match.group("prefix"),
        numbers=tuple(int(part) for part in match.group("numbers").split(".")),
        suffix=match.group("suffix") or "",
    )


def compare_versions(left: ParsedVersion, right: ParsedVersion) -> int:
    length = max(len(left.numbers), len(right.numbers))
    left_numbers = left.numbers + (0,) * (length - len(left.numbers))
    right_numbers = right.numbers + (0,) * (length - len(right.numbers))
    if left_numbers != right_numbers:
        return 1 if left_numbers > right_numbers else -1
    if left.suffix == right.suffix:
        return 0
    # A final release is newer than a prerelease of the same number.
    if not left.suffix:
        return 1
    if not right.suffix:
        return -1
    return 1 if left.suffix > right.suffix else -1


def version_status(current: str | None, latest: str | None) -> str:
    if not current or not latest:
        return "unknown"
    if current == latest:
        return "ok"
    left, right = parse_version(current), parse_version(latest)
    if left is None or right is None:
        return "update" if latest != current else "ok"
    return "update" if compare_versions(right, left) > 0 else "ok"


def http_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def github_latest(repo: str, token: str, timeout: float) -> tuple[str | None, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_error = ""
    try:
        data = http_json(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers=headers,
            timeout=timeout,
        )
        tag = data.get("tag_name")
        if tag:
            return str(tag), "GitHub API"
    except Exception as exc:  # noqa: BLE001 - report the fallback reason
        api_error = str(exc)

    try:
        request = urllib.request.Request(
            f"https://github.com/{repo}/releases/latest",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl().rstrip("/")
        tag = final_url.rsplit("/", 1)[-1]
        if parse_version(tag):
            return tag, "GitHub releases"
    except Exception as exc:  # noqa: BLE001
        if not api_error:
            api_error = str(exc)

    return None, f"GitHub API/redirect failed: {api_error or 'unknown error'}"


def yaml_module():
    return common.require_yaml()


def load_documents(path: Path) -> list[dict[str, Any]]:
    yaml = yaml_module()
    try:
        documents = yaml.safe_load_all(path.read_text(encoding="utf-8"))
        return [document for document in documents if isinstance(document, dict)]
    except Exception:
        return []


def repo_yaml_files() -> list[Path]:
    files: list[Path] = []
    for root, directories, filenames in os.walk(common.REPO_ROOT):
        root_path = Path(root)
        relative_root = root_path.relative_to(common.REPO_ROOT)
        if (
            set(relative_root.parts) & EXCLUDED_DIRS
            or relative_root.as_posix().startswith(EXCLUDED_PREFIXES)
        ):
            directories[:] = []
            continue

        directories[:] = [
            name
            for name in directories
            if name not in EXCLUDED_DIRS
            and not (relative_root / name).as_posix().startswith(EXCLUDED_PREFIXES)
        ]
        for name in filenames:
            path = root_path / name
            if path.suffix.lower() in YAML_SUFFIXES:
                files.append(path)
    return sorted(files)


def relative(path: Path) -> str:
    return path.relative_to(common.REPO_ROOT).as_posix()


def prerelease(version: str) -> bool:
    parsed = parse_version(version)
    return bool(parsed and PRERELEASE_RE.search(parsed.suffix))


def best_version(versions: list[str]) -> str | None:
    candidates = [version for version in versions if parse_version(version)]
    if not candidates:
        return None
    stable = [version for version in candidates if not prerelease(version)]
    pool = stable or candidates
    parsed = [(version, parse_version(version)) for version in pool]
    parsed = [(version, value) for version, value in parsed if value is not None]
    if not parsed:
        return None
    best = max(
        parsed,
        key=functools.cmp_to_key(lambda left, right: compare_versions(left[1], right[1])),
    )
    return best[0]


def check_talos(settings: dict[str, str], token: str, timeout: float) -> UpdateResult:
    current = settings.get("TALOS_VERSION")
    latest, source = github_latest("siderolabs/talos", token, timeout)
    detail = ""
    if current and latest:
        left, right = parse_version(current), parse_version(latest)
        if left and right:
            major_jump = right.numbers[0] > left.numbers[0]
            minor_jump = (
                len(left.numbers) > 1
                and len(right.numbers) > 1
                and right.numbers[0] == left.numbers[0]
                and right.numbers[1] > left.numbers[1] + 1
            )
            if major_jump or minor_jump:
                detail = "Talos requires sequential minor upgrades"
    return UpdateResult(
        "talos", "Talos Linux", current, latest, version_status(current, latest),
        source=source, detail=detail,
    )


def check_kubernetes(settings: dict[str, str], timeout: float) -> UpdateResult:
    current = settings.get("KUBERNETES_VERSION")
    latest = None
    source = "https://dl.k8s.io/release/stable.txt"
    try:
        latest = http_text(source, timeout=timeout).strip()
    except Exception as exc:  # noqa: BLE001
        return UpdateResult(
            "kubernetes", "Kubernetes", current, None, "unknown",
            source=source, detail=f"failed to read stable.txt: {exc}",
        )

    detail = ""
    left, right = parse_version(current), parse_version(latest)
    if left and right and len(left.numbers) > 1 and len(right.numbers) > 1:
        if right.numbers[1] > left.numbers[1] + 1:
            detail = "Kubernetes requires one minor upgrade at a time"
    return UpdateResult(
        "kubernetes", "Kubernetes", current, latest,
        version_status(current, latest), source=source, detail=detail,
    )


def flux_current() -> tuple[str, str]:
    path = common.REPO_ROOT / "flux/clusters/glemmen/flux-system/gotk-components.yaml"
    if path.is_file():
        match = re.search(
            r"app\.kubernetes\.io/version:\s*(v?\d+\.\d+\.\d+)",
            path.read_text(encoding="utf-8"),
        )
        if match:
            return match.group(1), relative(path)
    return common.DEFAULT_FLUX_VERSION, "scripts/common.py"


def check_flux(token: str, timeout: float) -> UpdateResult:
    current, current_source = flux_current()
    latest, source = github_latest("fluxcd/flux2", token, timeout)
    return UpdateResult(
        "flux", "Flux", current, latest, version_status(current, latest),
        source=source or current_source,
    )


def parse_oci_url(url: str) -> tuple[str, str] | None:
    value = url.removeprefix("oci://")
    registry, separator, repository = value.partition("/")
    if not separator or not registry or not repository:
        return None
    return registry, repository


def parse_image_ref(raw: str) -> ImageRef | None:
    value = raw.strip()
    if not value or any(token in value for token in ("{{", "${", " ")):
        return None
    if value.startswith(("/", ".", "http://", "https://")):
        return None

    digest = None
    if "@" in value:
        value, digest = value.split("@", 1)

    tag = None
    last_slash = value.rfind("/")
    last_colon = value.rfind(":")
    if last_colon > last_slash:
        value, tag = value[:last_colon], value[last_colon + 1 :]

    parts = value.split("/")
    if len(parts) > 1 and (
        "." in parts[0] or ":" in parts[0] or parts[0] == "localhost"
    ):
        registry = parts[0]
        repository = "/".join(parts[1:])
    else:
        registry = "docker.io"
        repository = value

    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
        if "/" not in repository:
            repository = f"library/{repository}"

    if not registry or not repository:
        return None
    return ImageRef(raw.strip(), registry, repository, tag, digest)


def registry_token(challenge: str, repository: str, timeout: float) -> str | None:
    if not challenge.lower().startswith("bearer "):
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.get("realm")
    if not realm:
        return None
    query = {
        "service": params.get("service", ""),
        "scope": params.get("scope") or f"repository:{repository}:pull",
    }
    query = {key: value for key, value in query.items() if value}
    separator = "&" if urllib.parse.urlparse(realm).query else "?"
    url = f"{realm}{separator}{urllib.parse.urlencode(query)}"
    data = http_json(url, timeout=timeout)
    token = data.get("token") or data.get("access_token")
    return str(token) if token else None


def registry_get(
    url: str,
    registry: str,
    repository: str,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    username = os.environ.get("DOCKER_USERNAME")
    password = os.environ.get("DOCKER_PASSWORD")
    if username and password:
        import base64

        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response), dict(response.headers)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        challenge = exc.headers.get("WWW-Authenticate", "")
        token = registry_token(challenge, repository, timeout)
        if not token:
            raise
        request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response), dict(response.headers)


def registry_tags(image: ImageRef, timeout: float) -> list[str]:
    registry = "registry-1.docker.io" if image.registry == "docker.io" else image.registry
    url = f"https://{registry}/v2/{image.repository}/tags/list?n=1000"
    tags: list[str] = []
    for _ in range(20):
        data, headers = registry_get(url, image.registry, image.repository, timeout)
        tags.extend(str(tag) for tag in (data.get("tags") or []))
        link = headers.get("Link") or headers.get("link")
        if not link:
            break
        match = re.search(r'<([^>]+)>;\s*rel="?next"?', link)
        if not match:
            break
        url = urllib.parse.urljoin(url, match.group(1))
    return tags


def best_image_tag(current: str, tags: list[str]) -> str | None:
    parsed_current = parse_version(current)
    if parsed_current is None:
        return None

    candidates: list[tuple[str, ParsedVersion]] = []
    for tag in tags:
        parsed = parse_version(tag)
        if parsed is None or parsed.prefix != parsed_current.prefix:
            continue
        # Only compare tags with the same variant suffix (e.g. -alpine).
        # Otherwise a build ID or a different image flavour wins by accident.
        if parsed.suffix != parsed_current.suffix:
            continue
        # A date/build stamp such as "9950684" is not a version bump for an
        # image currently tagged 0.18.0.
        if len(parsed_current.numbers) >= 2 and len(parsed.numbers) < 2:
            continue
        if "." in parsed_current.raw and "." not in tag:
            continue
        candidates.append((tag, parsed))

    if not candidates:
        return None

    def sort_key(item: tuple[str, ParsedVersion]) -> tuple[tuple[int, ...], str]:
        _, parsed = item
        length = max(len(parsed.numbers), len(parsed_current.numbers))
        numbers = parsed.numbers + (0,) * (length - len(parsed.numbers))
        return numbers, parsed.suffix

    return max(candidates, key=sort_key)[0]


def check_image(ref: str, timeout: float, files: list[str]) -> UpdateResult:
    image = parse_image_ref(ref)
    if image is None:
        return UpdateResult(
            "images", ref, None, None, "unknown", detail="not a container image"
        )
    if image.digest:
        return UpdateResult(
            "images", ref, image.digest, None, "tracking",
            detail="pinned by digest", files=files,
        )
    if not image.tag:
        return UpdateResult(
            "images", ref, None, None, "tracking",
            detail="no tag (implicit latest)", files=files,
        )
    if image.tag == "latest":
        return UpdateResult(
            "images", ref, image.tag, None, "tracking",
            detail="tracks latest", files=files,
        )

    try:
        tags = registry_tags(image, timeout)
    except Exception as exc:  # noqa: BLE001
        return UpdateResult(
            "images", ref, image.tag, None, "unknown",
            source=image.registry, detail=f"tag lookup failed: {exc}", files=files,
        )

    latest = best_image_tag(image.tag, tags)
    if latest is None:
        return UpdateResult(
            "images", ref, image.tag, None, "unknown",
            source=image.registry, detail="no comparable tags found", files=files,
        )
    return UpdateResult(
        "images", ref, image.tag, latest, version_status(image.tag, latest),
        source=image.registry, files=files,
    )


def collect_images() -> dict[str, list[str]]:
    images: dict[str, list[str]] = {}

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "image" and isinstance(child, str):
                    if parse_image_ref(child):
                        images.setdefault(child.strip(), []).append(path)
                else:
                    walk(child, path)
        elif isinstance(value, list):
            for child in value:
                walk(child, path)

    for path in repo_yaml_files():
        source = relative(path)
        for document in load_documents(path):
            walk(document, source)
    return {ref: sorted(set(files)) for ref, files in images.items()}


def collect_helm() -> list[dict[str, Any]]:
    helm_repositories: dict[str, str] = {}
    oci_repositories: dict[str, dict[str, str]] = {}
    releases: list[dict[str, Any]] = []

    for path in repo_yaml_files():
        for document in load_documents(path):
            kind = document.get("kind")
            metadata = document.get("metadata") or {}
            name = metadata.get("name")
            if not name:
                continue
            if kind == "HelmRepository":
                url = (document.get("spec") or {}).get("url")
                if url:
                    helm_repositories[name] = str(url)
            elif kind == "OCIRepository":
                spec = document.get("spec") or {}
                ref = spec.get("ref") or {}
                oci_repositories[name] = {
                    "url": str(spec.get("url", "")),
                    "tag": str(ref.get("tag", "")),
                }
            elif kind == "HelmRelease":
                releases.append(
                    {
                        "path": relative(path),
                        "name": name,
                        "namespace": metadata.get("namespace", ""),
                        "spec": document.get("spec") or {},
                    }
                )

    targets: list[dict[str, Any]] = []
    for release in releases:
        spec = release["spec"]
        chart_spec = spec.get("chart") or {}
        chart_ref = spec.get("chartRef")
        if chart_ref:
            source_name = chart_ref.get("name")
            source = oci_repositories.get(source_name)
            if not source:
                continue
            targets.append(
                {
                    "name": release["name"],
                    "namespace": release["namespace"],
                    "path": release["path"],
                    "chart": source["url"].rsplit("/", 1)[-1],
                    "current": source["tag"],
                    "kind": "OCIRepository",
                    "source_name": source_name,
                    "source_url": source["url"],
                }
            )
            continue

        inner = chart_spec.get("spec") or {}
        chart = inner.get("chart")
        current = inner.get("version")
        source_ref = inner.get("sourceRef") or {}
        source_name = source_ref.get("name")
        source_kind = source_ref.get("kind")
        source_url = ""
        if source_kind == "HelmRepository":
            source_url = helm_repositories.get(source_name, "")
        elif source_kind == "OCIRepository":
            source_url = (oci_repositories.get(source_name) or {}).get("url", "")
        if chart and current and source_url:
            targets.append(
                {
                    "name": release["name"],
                    "namespace": release["namespace"],
                    "path": release["path"],
                    "chart": chart,
                    "current": current,
                    "kind": source_kind,
                    "source_name": source_name,
                    "source_url": source_url,
                }
            )
    return targets


def check_helm_targets(timeout: float) -> list[UpdateResult]:
    index_cache: dict[str, dict[str, Any]] = {}
    results: list[UpdateResult] = []

    for target in collect_helm():
        current = target["current"]
        latest = None
        detail = ""
        source = target["source_url"]
        try:
            if target["kind"] == "HelmRepository":
                index_url = source.rstrip("/") + "/index.yaml"
                if index_url not in index_cache:
                    yaml = yaml_module()
                    index_cache[index_url] = yaml.safe_load(
                        http_text(index_url, timeout=timeout)
                    )
                entries = (index_cache[index_url].get("entries") or {}).get(
                    target["chart"], []
                )
                versions = [
                    str(entry["version"])
                    for entry in entries
                    if entry.get("version") and not entry.get("deprecated")
                ]
                latest = best_version(versions)
            elif target["kind"] == "OCIRepository":
                parsed = parse_oci_url(source)
                if parsed:
                    registry, repository = parsed
                    image = ImageRef(source, registry, repository, None, None)
                    latest = best_version(registry_tags(image, timeout))
        except Exception as exc:  # noqa: BLE001
            detail = f"lookup failed: {exc}"

        name = target["name"]
        if target["namespace"]:
            name = f"{target['namespace']}/{name}"
        results.append(
            UpdateResult(
                "helm",
                name,
                current,
                latest,
                version_status(current, latest) if latest else "unknown",
                source=source,
                detail=detail,
                files=[target["path"]],
            )
        )
    return results


def check_tuppr(settings: dict[str, str]) -> UpdateResult:
    path = (
        common.REPO_ROOT
        / "flux/infrastructure/tuppr/resources/kustomization.yaml"
    )
    if not path.is_file():
        return UpdateResult(
            "tuppr", "upgrade plans", None, None, "unknown",
            detail="tuppr resources kustomization not found",
        )
    text = path.read_text(encoding="utf-8")
    configured = (
        "../../../../talos/settings.env" in text
        and "TALOS_VERSION" in text
        and "KUBERNETES_VERSION" in text
    )
    return UpdateResult(
        "tuppr",
        "upgrade plan versions",
        (
            f"talos {settings.get('TALOS_VERSION')}, "
            f"kubernetes {settings.get('KUBERNETES_VERSION')}"
        ),
        None,
        "ok" if configured else "unknown",
        source=relative(path),
        detail=(
            "TalosUpgrade/KubernetesUpgrade versions come from talos/settings.env"
            if configured
            else "kustomization does not read talos/settings.env"
        ),
    )


def print_results(results: list[UpdateResult], settings: dict[str, str]) -> None:
    labels = {
        "update": "UPDATE",
        "ok": "ok",
        "tracking": "tracking",
        "unknown": "unknown",
    }
    for category in CATEGORIES:
        items = [item for item in results if item.category == category]
        if not items:
            continue
        print(f"\n{category.upper()}")
        for item in items:
            status = labels.get(item.status, item.status)
            if item.status == "update":
                line = f"  {item.name}: {item.current} -> {item.latest}  [{status}]"
            elif item.current and item.latest:
                line = f"  {item.name}: {item.current} ({status})"
            elif item.current:
                line = f"  {item.name}: {item.current} ({status})"
            else:
                line = f"  {item.name}: {status}"
            if item.detail:
                line += f" - {item.detail}"
            print(line)
            if item.files:
                print(f"      {item.files[0]}")

    updates = [item for item in results if item.status == "update"]
    print()
    if updates:
        print(f"Summary: {len(updates)} update(s) available")
        for item in updates:
            print(f"  - {item.name}: {item.current} -> {item.latest}")
    else:
        print("Summary: no updates found")
    print(
        "Tuppr reads TALOS_VERSION and KUBERNETES_VERSION from "
        f"talos/settings.env (Talos {settings.get('TALOS_VERSION')}, "
        f"Kubernetes {settings.get('KUBERNETES_VERSION')})."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-updates.py",
        description="Check pinned versions against upstream releases.",
    )
    parser.add_argument(
        "--only", action="append", choices=CATEGORIES,
        help="limit checks to a category (repeatable)",
    )
    parser.add_argument("--no-images", action="store_true", help="skip image checks")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--timeout", type=float,
        default=float(os.environ.get("CHECK_UPDATES_TIMEOUT", "20")),
        help="HTTP timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--jobs", type=int, default=8, help="parallel image checks (default: 8)"
    )
    parser.add_argument(
        "--fail-on-updates", action="store_true",
        help="exit 1 when at least one update is available",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    args.jobs = max(1, args.jobs)

    settings = common.load_env_file(common.TALOS_SETTINGS)
    selected = set(args.only or CATEGORIES)
    if args.no_images:
        selected.discard("images")
    token = os.environ.get("GITHUB_TOKEN", "")

    results: list[UpdateResult] = []
    if "talos" in selected:
        results.append(check_talos(settings, token, args.timeout))
    if "kubernetes" in selected:
        results.append(check_kubernetes(settings, args.timeout))
    if "flux" in selected:
        results.append(check_flux(token, args.timeout))
    if "helm" in selected:
        results.extend(check_helm_targets(args.timeout))
    if "tuppr" in selected:
        results.append(check_tuppr(settings))

    if "images" in selected:
        images = collect_images()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(check_image, ref, args.timeout, files): ref
                for ref, files in images.items()
            }
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

    category_order = {category: index for index, category in enumerate(CATEGORIES)}
    results.sort(key=lambda item: (category_order[item.category], item.name))

    if args.json:
        print(
            json.dumps(
                {
                    "settings": {
                        "talos_version": settings.get("TALOS_VERSION"),
                        "kubernetes_version": settings.get("KUBERNETES_VERSION"),
                    },
                    "results": [asdict(item) for item in results],
                },
                indent=2,
            )
        )
    else:
        print_results(results, settings)

    if args.fail_on_updates and any(item.status == "update" for item in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
