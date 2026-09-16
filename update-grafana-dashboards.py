#!/usr/bin/env python3
"""Create or update Grafana dashboard ConfigMap manifests from upstream sources.

Downloads dashboard JSON files and writes them as Kubernetes ConfigMap YAML
into the kube-prometheus-stack/dashboards/ Fleet bundle, matching the format of
the other dashboards there (label grafana_dashboard: "1", namespace applied via
fleet.yaml).

Each entry in DASHBOARDS points at a raw dashboard JSON. The ConfigMap name and
the YAML file name come from the "configmap" field; the dashboard is stored
under a data key derived from the URL's file name (or an explicit "data_key").
Grafana.com dashboards use the __inputs export format, which Grafana's file
provisioning ignores; those inputs are rewritten to datasource template
variables so the dashboards work when loaded from a ConfigMap.

Stdlib only. Run:  python3 update-grafana-dashboards.py
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DASHBOARDS = [
    {
        "configmap": "grafana-dashboard-node-exporter",
        "url": "https://raw.githubusercontent.com/rfmoz/grafana-dashboards/master/prometheus/node-exporter-full.json",
    },
    {
        "configmap": "grafana-dashboard-unpoller-client-dpi",
        "url": "https://grafana.com/api/dashboards/11310/revisions/latest/download",
        "data_key": "client-dpi.json",
    },
    {
        "configmap": "grafana-dashboard-unpoller-network-sites",
        "url": "https://grafana.com/api/dashboards/11311/revisions/latest/download",
        "data_key": "network-sites.json",
    },
    {
        "configmap": "grafana-dashboard-unpoller-usw-insights",
        "url": "https://grafana.com/api/dashboards/11312/revisions/latest/download",
        "data_key": "usw-insights.json",
    },
    {
        "configmap": "grafana-dashboard-unpoller-usg-insights",
        "url": "https://grafana.com/api/dashboards/11313/revisions/latest/download",
        "data_key": "usg-insights.json",
    },
    {
        "configmap": "grafana-dashboard-unpoller-uap-insights",
        "url": "https://grafana.com/api/dashboards/11314/revisions/latest/download",
        "data_key": "uap-insights.json",
    },
    {
        "configmap": "grafana-dashboard-unpoller-client-insights",
        "url": "https://grafana.com/api/dashboards/11315/revisions/latest/download",
        "data_key": "client-insights.json",
    },
]

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / \
    "kube-prometheus-stack" / "dashboards"


def data_key(url):
    """Derive the ConfigMap data key from the URL's file name."""
    return Path(url.split("?", 1)[0]).name


def fetch(url):
    """Download url and return its text content, raising on errors."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "update-grafana-dashboards"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except Exception as exc:
        raise RuntimeError("failed to download {}".format(url)) from exc


def validate_json(text, url):
    """Ensure the downloaded content is a single valid dashboard JSON document."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(
            "downloaded content from {} is not valid JSON".format(url)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "downloaded JSON from {} is not a dashboard object".format(url))
    if "title" not in payload or "panels" not in payload:
        raise RuntimeError(
            "downloaded JSON from {} does not look like a Grafana dashboard".format(url))
    return payload


DATASOURCE_VARIABLE = {
    "current": {},
    "hide": 0,
    "includeAll": False,
    "multi": False,
    "options": [],
    "refresh": 1,
    "regex": "",
    "skipUrlSync": False,
    "type": "datasource",
}


def convert_inputs_to_variables(payload):
    """Rewrite __inputs datasources into templating datasource variables.

    Grafana.com dashboards are exported for the import wizard: panels reference
    ${DS_PROMETHEUS} and the datasource is declared in __inputs, which Grafana's
    file/ConfigMap provisioning ignores (the panels would fail with "Datasource
    ${DS_PROMETHEUS} was not found"). Giving the variable the same name makes
    Grafana resolve the provisioned Prometheus datasource at render time.
    Returns True when the payload was modified.
    """
    inputs = payload.get("__inputs") or []
    datasources = [item for item in inputs if item.get("type") == "datasource"]
    if not datasources:
        return False

    templating = payload.setdefault("templating", {}).setdefault("list", [])
    names = {variable.get("name") for variable in templating}
    for item in datasources:
        if item["name"] in names:
            continue
        templating.append(dict(
            DATASOURCE_VARIABLE,
            name=item["name"],
            query=item.get("pluginId", "prometheus"),
        ))

    remaining = [item for item in inputs if item.get("type") != "datasource"]
    if remaining:
        payload["__inputs"] = remaining
    else:
        payload.pop("__inputs")
    return True


def render_block_scalar(text, indent):
    """Render text as a YAML literal block scalar with each line prefixed by `indent` spaces."""
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(" " * indent + line if line else "" for line in lines)


def build_manifest(configmap_name, key, dashboard_text):
    """Build a ConfigMap manifest YAML document for one dashboard."""
    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: {}".format(configmap_name),
        "  labels:",
        '    grafana_dashboard: "1"',
        "data:",
        "  {}: |".format(key),
        render_block_scalar(dashboard_text, indent=4),
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory to write ConfigMap manifests into (default: %(default)s)",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    failed = False
    for dashboard in DASHBOARDS:
        configmap_name = dashboard["configmap"]
        url = dashboard["url"]
        key = dashboard.get("data_key", data_key(url))
        path = args.output_dir / "{}.yaml".format(configmap_name)
        try:
            text = fetch(url)
            payload = validate_json(text, url)
        except RuntimeError as exc:
            print("ERROR {}: {}".format(configmap_name, exc), file=sys.stderr)
            failed = True
            continue

        if convert_inputs_to_variables(payload):
            text = json.dumps(payload, indent=2, ensure_ascii=False)
        manifest = build_manifest(configmap_name, key, text)
        changed = not path.exists() or path.read_text() != manifest
        path.write_text(manifest)
        status = "updated" if changed else "unchanged"
        print("{}: {} ({:,} bytes)".format(path.name, status, len(text)))

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
