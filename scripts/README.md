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

```sh
uv run --with requests --with PyYAML \
  python scripts/netbox/generate_inventory.py --from-cluster \
  --kubectl ./scripts/bin/kubectl --kubeconfig talos/generated/kubeconfig
```

Regenerates `flux/apps/netbox/inventory/inventory.yaml` from the live UniFi
controller and Kubernetes/Talos nodes. `--from-cluster` reads the UniFi
credentials from the `unpoller-unifi` Secret in `monitoring`; the credentials
are never written to the inventory. Review the diff and commit the result.

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
