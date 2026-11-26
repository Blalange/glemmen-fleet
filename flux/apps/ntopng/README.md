# ntopng

[ntopng](https://www.ntop.org/products/traffic-analysis/ntopng/) is the
cluster's traffic probe. It runs on `glemmen80` and captures two 1 Gbit/s
mirror feeds:

| Interface | Source | Purpose |
| --- | --- | --- |
| `eno1` | UDM Pro port 7 (mirror of WAN port 9) | Outbound/inbound internet traffic |
| `eno2` | USW Pro 24 PoE port 3 (mirror of uplink port 1) | Client LAN traffic, including internal traffic through the UDM |

The switch/UDM configuration, cabling and limitations are documented in
[`network/unifi/README.md`](../../../network/unifi/README.md).

The web UI is at <https://ntopng.glem.men>. Traefik terminates TLS with the
`*.glem.men` wildcard certificate and forwards to the pod on
`192.168.6.80:3000`. ntopng still enforces its own login.

## Login

The admin password is not stored in Git. The deployment reads only the MD5
hash from the hand-created `ntopng-admin` Secret in the `ntopng` namespace;
the startup script writes it to Redis before ntopng starts. The plaintext is
kept in the same Secret under `password` so it can be retrieved when needed:

```sh
kubectl -n ntopng get secret ntopng-admin \
  -o jsonpath='{.data.password}' | base64 -d
```

Create or rotate it with:

```sh
kubectl -n ntopng create secret generic ntopng-admin \
  --from-literal=password='<new-password>' \
  --from-literal=password-md5="$(printf '%s' '<new-password>' | md5sum | cut -d' ' -f1)" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n ntopng rollout restart deployment/ntopng
```

The startup script re-applies the hash on every start, so a password changed
in the ntopng UI is reset to the Secret value after the next pod restart.
Passwords with special characters are rejected by ntopng; use letters and
digits.

## Placement and volumes

The pod is pinned to `glemmen80` with `kubernetes.io/hostname` because that
node has the two spare 1 Gbit/s NICs (`eno1`, `eno2`) and a 10G link to the
Nexus. It uses `hostNetwork: true`, `NET_ADMIN` and `NET_RAW` so libpcap can
open the mirror interfaces and put them in promiscuous mode. The Talos node
config in `talos/nodes/glemmen80.yaml` also brings `eno1`/`eno2` up at boot;
the busybox `link-up` init container does the same for hot-plugged cables.

Two `nvme-rbd` PVCs keep state across restarts:

- `ntopng-data` (20 GiB) – `/var/lib/ntopng`, including RRD and host history.
- `ntopng-redis` (2 GiB) – `/var/lib/redis`, ntopng's configuration, users
  and DNS/flow caches.

The image's local Redis listens on `127.0.0.1:6379` only, so it is not exposed
on the host network.

## First start and verification

```sh
kubectl -n ntopng get pods -o wide
kubectl -n ntopng logs deployment/ntopng --tail=100
```

Important log lines:

- `Reading packets from eno1` and `Reading packets from eno2`
- `HTTP server listening on 3000`
- no `Unable to open interface` or `Permission denied` errors.

From the cluster network, the UI should answer on both the Service and the
node address:

```sh
kubectl -n ntopng port-forward service/ntopng 3000:3000
# or
curl -I http://192.168.6.80:3000/
```

The ntopng interface tabs should show packet counters for both `eno1` and
`eno2`. If one stays at zero, check the corresponding mirror source and cable;
see [`network/unifi/README.md`](../../../network/unifi/README.md).

## Updating

The `ntop/ntopng` registry repository only publishes the `latest` tag. The
manifest pins the current image by digest for reproducibility. To update,
pull `latest`, read the current digest and replace the two occurrences in
`deployment.yaml` (the permission init container and the ntopng container):

```sh
TOKEN=$(curl -fsSL 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:ntop/ntopng:pull' | jq -r .token)
curl -fsSI -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  https://registry-1.docker.io/v2/ntop/ntopng/manifests/latest |
  tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
```

Then run `flux reconcile kustomization ntopng --with-source` or let Flux pick
up the commit.
