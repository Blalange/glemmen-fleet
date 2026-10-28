#!/usr/bin/env python3
"""Create or update Grafana dashboard JSON files from upstream sources.

Downloads dashboard JSON files into the Grafana dashboards directory. The
Kustomization's configMapGenerator turns them into ConfigMaps labelled
grafana_dashboard: "1", so no YAML is generated here. Dashboards that are
maintained by hand (cluster-specs, disk-health, the enhanced Longhorn
dashboard and the Flux dashboards) are not listed in DASHBOARDS and are left
untouched.

Each entry in DASHBOARDS points at a raw dashboard JSON and names the output
file (and optionally a subdirectory). When adding a dashboard here, also add
its file to configMapGenerator in the matching kustomization.yaml. Grafana.com
dashboards use the __inputs export format, which Grafana's file provisioning
ignores; those inputs are rewritten to datasource template variables so the
dashboards work when loaded from a ConfigMap.

Stdlib only. Run:  python3 scripts/update-grafana-dashboards.py
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DASHBOARDS = [
    {
        "file": "node-exporter-full.json",
        "dir": "node-exporter",
        "url": "https://raw.githubusercontent.com/rfmoz/grafana-dashboards/master/prometheus/node-exporter-full.json",
    },
    {
        "file": "smartctl-exporter.json",
        "url": "https://grafana.com/api/dashboards/24922/revisions/latest/download",
    },
    {
        "file": "idrac-exporter.json",
        "url": "https://raw.githubusercontent.com/mrlhansen/idrac_exporter/master/grafana/idrac.json",
    },
    {
        "file": "idrac-overview.json",
        "url": "https://raw.githubusercontent.com/mrlhansen/idrac_exporter/master/grafana/idrac_overview.json",
    },
    {
        "file": "client-dpi.json",
        "dir": "unifi",
        "url": "https://grafana.com/api/dashboards/11310/revisions/latest/download",
    },
    {
        "file": "network-sites.json",
        "dir": "unifi",
        "url": "https://grafana.com/api/dashboards/11311/revisions/latest/download",
    },
    {
        "file": "usw-insights.json",
        "dir": "unifi",
        "url": "https://grafana.com/api/dashboards/11312/revisions/latest/download",
    },
    {
        "file": "usg-insights.json",
        "dir": "unifi",
        "url": "https://grafana.com/api/dashboards/11313/revisions/latest/download",
    },
    {
        "file": "uap-insights.json",
        "dir": "unifi",
        "url": "https://grafana.com/api/dashboards/11314/revisions/latest/download",
    },
    {
        "file": "client-insights.json",
        "dir": "unifi",
        "url": "https://grafana.com/api/dashboards/11315/revisions/latest/download",
    },
    {
        "file": "overview.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/overview.json",
    },
    {
        "file": "compact.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/compact.json",
    },
    {
        "file": "query.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/query.json",
    },
    {
        "file": "query-frontend.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/query-frontend.json",
    },
    {
        "file": "store.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/store.json",
    },
    {
        "file": "receive.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/receive.json",
    },
    {
        "file": "sidecar.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/sidecar.json",
    },
    {
        "file": "rule.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/rule.json",
    },
    {
        "file": "bucket-replicate.json",
        "dir": "thanos",
        "url": "https://raw.githubusercontent.com/thanos-io/thanos/main/examples/dashboards/bucket-replicate.json",
    },
]

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / \
    "flux" / "monitoring" / "resources" / "dashboards"


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
    if "title" not in payload or (
            "panels" not in payload and "rows" not in payload):
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

    Some exports also contain their own datasource variable whose default value
    is still the import placeholder (for example the Longhorn dashboard's
    ${datasource}). Clear that stale current value so Grafana selects the
    provisioned Prometheus datasource when the dashboard loads.

    Returns True when the payload was modified.
    """
    inputs = payload.get("__inputs") or []
    datasources = [item for item in inputs if item.get("type") == "datasource"]
    if not datasources:
        return False

    templating = payload.setdefault("templating", {}).setdefault("list", [])
    names = {variable.get("name") for variable in templating}
    added = []
    for item in datasources:
        if item["name"] in names:
            continue
        variable = dict(
            DATASOURCE_VARIABLE,
            name=item["name"],
            query=item.get("pluginId", "prometheus"),
        )
        templating.append(variable)
        added.append(variable)

    import_placeholders = {"${%s}" % item["name"] for item in datasources}
    for variable in templating:
        if variable.get("type") != "datasource":
            continue
        current = variable.get("current")
        if isinstance(current, dict) and current.get("value") in import_placeholders:
            variable["current"] = {}

    remaining = [item for item in inputs if item.get("type") != "datasource"]
    if remaining:
        payload["__inputs"] = remaining
    else:
        payload.pop("__inputs")

    # Drop import variables that no panel actually references. The Longhorn
    # export, for example, keeps its own 'datasource' variable and no longer
    # needs the DS_PROMETHEUS placeholder once its stale current value is gone.
    serialized = json.dumps(payload)
    for variable in added:
        if "${%s}" % variable["name"] not in serialized:
            templating.remove(variable)
    return True


def clear_datasource_current(payload):
    """Remove pinned datasource choices from template variables.

    Some exports (for example the iDRAC BMC overview) remember a datasource
    that does not exist in this cluster. Leaving the current value set makes
    the panels show "Datasource not found" until someone picks one manually.
    An empty current value lets Grafana use the default data source of the
    requested type.

    Returns True when the payload was modified.
    """
    changed = False
    for variable in payload.get("templating", {}).get("list", []):
        if variable.get("type") == "datasource" and variable.get("current"):
            variable["current"] = {}
            changed = True
    return changed


def render_json(payload):
    """Render a dashboard payload as a stable, pretty-printed JSON document."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory to write dashboard JSON files into (default: %(default)s)",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    failed = False
    for dashboard in DASHBOARDS:
        filename = dashboard["file"]
        url = dashboard["url"]
        path = args.output_dir / dashboard.get("dir", "") / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = fetch(url)
            payload = validate_json(text, url)
        except RuntimeError as exc:
            print("ERROR {}: {}".format(filename, exc), file=sys.stderr)
            failed = True
            continue

        convert_inputs_to_variables(payload)
        clear_datasource_current(payload)
        text = render_json(payload)
        changed = not path.exists() or path.read_text() != text
        if changed:
            path.write_text(text)
        status = "updated" if changed else "unchanged"
        print("{}: {} ({:,} bytes)".format(filename, status, len(text)))

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
