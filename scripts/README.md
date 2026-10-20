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
