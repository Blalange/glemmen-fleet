# Talos secrets (gitignored)
#
# Files in this directory are ignored by git (see .gitignore), except for this
# README. Keep credentials here, never in the patches/nodes files.

## netbird.env

The Netbird setup key used for the `ExtensionServiceConfig` rendered by
`gen-config.sh`. Create a *reusable* setup key in the Netbird dashboard
(https://netbird.blalange.org) and put it in `netbird.env`:

```sh
NB_SETUP_KEY=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
```

A setup key is a credential: anyone holding it can register peers in your
Netbird network. If it leaks, revoke it in the dashboard and write a new one
here, then re-run `./gen-config.sh` and re-apply the machine configs.

## generated/

Everything the scripts derive from these secrets (secrets bundle, kubeconfig,
machine configs) lands in `talos/generated/`, which is also gitignored.
Back that directory up: it contains the cluster PKI (`secrets.yaml`).
Losing it means losing the ability to manage the Talos API without a reinstall.
