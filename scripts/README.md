# Scripts

All operational scripts live here. Configuration, machine patches and generated
state stay in `talos/` and `flux/`; downloaded CLI tools live in `scripts/bin/`
(gitignored).

## Tools

```sh
./scripts/download-tools.py                 # talosctl + kubectl + flux
./scripts/download-tools.py talosctl        # one tool
./scripts/download-tools.py flux --force    # re-download
```

Versions come from `talos/settings.env` (`TALOS_VERSION`,
`KUBERNETES_VERSION`) and the built-in Flux default. They can be overridden with
`TALOS_VERSION`, `KUBECTL_VERSION` and `FLUX_VERSION`. The other scripts never
download tools; if one is missing they print the command above.

## Talos

```sh
./scripts/talos/gen-iso.py [--download]      # Image Factory schematic + ISO URLs
./scripts/talos/setup-images.py [--print]    # per-node maintenance-mode ISOs
./scripts/talos/gen-config.py                # secrets + machine configs + talosconfig
./scripts/talos/manage.py update <node>      # apply regenerated config to a running node
./scripts/talos/manage.py <command>          # other day-2 operations
```

`gen-config.py`, `gen-iso.py` and `setup-images.py` need PyYAML
(`python3 -m pip install pyyaml`).

## Flux

```sh
export GITHUB_TOKEN=github_pat_...
./scripts/flux/setup.py                      # bootstrap Flux
./scripts/flux/manage.py status              # status / sync / suspend / resume / logs ...
```

Both use `talos/generated/kubeconfig` by default; override with `KUBECONFIG`.

## NetBox

The NetBox inventory, collector, sync code and usage instructions live in
[`inventory/`](../inventory/README.md).

```sh
# collect live sources into inventory/generated
env -u IDRAC_USER -u IDRAC_PASS \
  uv run --with pysnmp==7.1.22 --with requests --with PyYAML \
  python inventory/scripts/collect.py --from-cluster \
  --kubectl ./scripts/bin/kubectl --kubeconfig talos/generated/kubeconfig \
  --talosctl ./scripts/bin/talosctl --talosconfig talos/generated/talosconfig

# validate the merged inventory
uv run --with pynetbox==7.9.0 --with PyYAML==6.0.3 --with requests==2.32.5 \
  python inventory/scripts/sync.py --inventory inventory --check
```

The collector also polls SNMP v2c network devices (default community
`public`) for interfaces, VLANs and LLDP/FDB diagnostics. Use
`--snmp-community`, `--no-snmp` or `--snmp-report PATH` as needed.

The sync enriches device and module types from the Apache-2.0 NetBox Device
Type Library (`--dtl`, enabled by default). Definitions and elevation images
are fetched at runtime and cached outside Git; manual inventory values and
components always win. The CronJob enables this with `DTL_ENABLED=true` and
caches under the NetBox media volume.

## Updates

```sh
./scripts/check-updates.py                  # Talos, Kubernetes, Flux, Helm, images
./scripts/check-updates.py --no-images      # skip registry lookups
./scripts/check-updates.py --json           # machine-readable output
./scripts/check-updates.py --fail-on-updates
```

The checker compares `TALOS_VERSION` and `KUBERNETES_VERSION` from
`talos/settings.env`, reads HelmRelease/OCIRepository versions from the Flux
manifests, and scans YAML for container image tags. `GITHUB_TOKEN` is optional
but avoids GitHub API rate limits. Tuppr's `TalosUpgrade` and
`KubernetesUpgrade` versions are generated from the same settings file via
Kustomize replacements.
