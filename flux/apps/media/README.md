# Media stack

Jellyfin, Sonarr, Radarr, Prowlarr, Seerr and qBittorrent run in the `media`
namespace. They share one SeaweedFS-backed `ReadWriteMany` volume at `/data`:

```
/data/downloads              qBittorrent downloads (incomplete/ below it)
/data/media/movies           Radarr root folder
/data/media/tv               Sonarr root folder
```

Sonarr/Radarr import from `/data/downloads` into `/data/media` on the same
filesystem, so the normal move/rename path works without a hardlink. Hardlinks
are not guaranteed on the FUSE mount; disable "Use Hardlinks instead of Copy"
in the *arr media management settings if import errors mention hardlinks.

| App | URL | Auth |
|---|---|---|
| Jellyfin | https://jellyfin.glem.men | Native OIDC (SSO-Auth plugin) |
| Sonarr | https://sonarr.glem.men | Authentik proxy, app auth `External` |
| Radarr | https://radarr.glem.men | Authentik proxy, app auth `External` |
| Prowlarr | https://prowlarr.glem.men | Authentik proxy, app auth `External` |
| Seerr | https://seerr.glem.men | Authentik proxy + Seerr login |
| qBittorrent | https://torrent.glem.men | Authentik proxy; qBittorrent auth bypassed for the pod network |

## Secrets

Everything secret is created by hand and never committed:

| Secret | Purpose |
|---|---|
| `qbittorrent-wireguard` | Windscribe `wg0.conf` with an IPv4 endpoint |
| `qbittorrent-webui` | qBittorrent break-glass username/password (keys `username`, `password`) |
| `jellyfin-admin` | Jellyfin local administrator (keys `username`, `password`) |
| `authentik-outpost` | Authentik outpost token (key `token`) |

The local admin passwords are only for break-glass access; normal Jellyfin and
qBittorrent access goes through Authentik. Read them with `kubectl -n media get
secret <name> -o jsonpath=...` when needed.

## Databases

All media applications use SQLite on their Longhorn config volumes:

- Sonarr/Radarr/Prowlarr: `<app>.db` under `/config`
- Jellyfin: `/config/data/jellyfin.db`
- Seerr: `/config/db.sqlite3`
- qBittorrent: its own settings database under `/config`

YugabyteDB was evaluated for Sonarr/Radarr/Prowlarr, but their
`DatabaseVersionParser` calls `.NET Version.Parse()` on YSQL's
`server_version` string (`15.12-YB-2026.1.2.0-b0`), which fails. They therefore
fall back to the supported SQLite backend. `yugabyte-init-v5` no longer creates
media roles or databases, and there is no `media-db` Secret.

## FlareSolverr

FlareSolverr runs as an internal-only service at
`http://flaresolverr.media.svc.cluster.local:8191`. It is used by Prowlarr as an
indexer proxy for Cloudflare-protected trackers. It has no Ingress and no SSO;
only cluster workloads can reach it.

Prowlarr's indexer proxy is configuration, not a Kubernetes resource, so it is
created through Prowlarr (Settings > Indexers > Indexer Proxies) or its API:

```sh
api=$(kubectl -n media exec deploy/prowlarr -- sh -c \
  'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -d">" -f2')

kubectl -n media exec deploy/prowlarr -- sh -c "curl -sS -X POST \
  -H 'X-Api-Key: $api' -H 'Content-Type: application/json' \
  http://127.0.0.1:9696/api/v1/indexerproxy \
  --data '{"name":"FlareSolverr","implementation":"FlareSolverr","configContract":"FlareSolverrSettings","fields":[{"name":"host","value":"http://flaresolverr.media.svc.cluster.local:8191/"},{"name":"requestTimeout","value":60}],"tags":[]}'"
```

The image is pinned to FlareSolverr `v3.5.2`; update it with the other media
images when a newer release is available.

## WireGuard

The WireGuard configuration is never committed. Create the Secret from the
Windscribe profile. Remove the IPv6 `Address` and `::/0` allowed IP first if
the profile contains them; the cluster and pod network are IPv4-only. Keep the
private key, peer public key, preshared key, endpoint and IPv4 address.

```sh
kubectl -n media create secret generic qbittorrent-wireguard \
  --from-file=wg0.conf=./Windscribe-Oslo-Rakfisk-WG.conf \
  --dry-run=client -o yaml | kubectl apply -f -
```

Gluetun reads the file at `/gluetun/wireguard/wg0.conf`. Its custom provider
requires the `Endpoint` line to contain an IPv4 address, not a hostname. Resolve
`osl-433-wg.whiskergalaxy.com` once and put the address in the Secret:

```sh
getent ahostsv4 osl-433-wg.whiskergalaxy.com
# Endpoint = 79.127.150.99:443
```

The IP at the time of writing is `79.127.150.99`. If Windscribe changes the
server address or you regenerate the profile, update the Secret. If
`/dev/net/tun` is denied, set `privileged: true` on the gluetun container; the
namespace already permits it.

## Authentik

Create two groups in Authentik: `media-users` (normal viewers/requesters) and
`media-admins` (Sonarr/Radarr/Prowlarr/qBittorrent administrators). Bind the
Jellyfin and Seerr applications to `media-users`, and the other proxy
applications to `media-admins`. Do not grant the admin-only apps to
`media-users`: the *arr apps run with authentication disabled and trust the
proxy.

Create an outpost named `glemmen-media` of type Proxy with these providers:

| Application | Provider type | External host | Internal host |
|---|---|---|---|
| Sonarr | Proxy | https://sonarr.glem.men | http://sonarr.media.svc.cluster.local:8989 |
| Radarr | Proxy | https://radarr.glem.men | http://radarr.media.svc.cluster.local:7878 |
| Prowlarr | Proxy | https://prowlarr.glem.men | http://prowlarr.media.svc.cluster.local:9696 |
| Seerr | Proxy | https://seerr.glem.men | http://seerr.media.svc.cluster.local:5055 |
| qBittorrent | Proxy | https://torrent.glem.men | http://qbittorrent.media.svc.cluster.local:8080 |

The outpost is deployed by this Kustomization, not by an Authentik service
connection. Copy its token into the `authentik-outpost` Secret and restart the
outpost:

```sh
kubectl -n media create secret generic authentik-outpost \
  --from-literal=token='<outpost token>'
kubectl -n media rollout restart deploy/authentik-outpost
```

For Jellyfin, reuse the existing `jellyfin` OAuth2/OpenID provider (or create a
new confidential provider) and add the redirect URI:

```
https://jellyfin.glem.men/sso/OID/redirect/authentik
```

Install the SSO-Auth plugin from
`https://raw.githubusercontent.com/k0lin/jellyfin-plugin-sso/manifest-release/manifest.json`
(plugin >= 5.1 for Jellyfin >= 12), add an OIDC provider with issuer
`https://auth.blalange.org/application/o/jellyfin/`, the client ID/secret, and
the redirect URI above. Map `media-admins` to Jellyfin administrators and
`media-users` to normal users. Keep a local Jellyfin administrator as a
break-glass account.

The Stable Seerr image has no native OIDC, so users authenticate at Authentik
and then log in to Seerr with their Jellyfin/local Seerr account. Switching to
the `preview-new-oidc` image removes the second login but is not recommended for
production.

## SeaweedFS capacity and backups

The media PVC is 600Gi on four SeaweedFS volume servers, each with two 100Gi
disks (`volumeServerDiskCount: 2`). That is eight 100Gi data volumes, 800Gi
total; the remainder is headroom for the Thanos bucket and metadata. Required
pod anti-affinity keeps one volume server per node.

`volumeServerDiskCount` is a top-level Seaweed field, not a field under
`volume`. Changing it, `volume.replicas` or `volume.requests.storage` changes
the StatefulSet's `volumeClaimTemplates`, which Kubernetes forbids updating in
place. The operator logs `VolumeClaimTemplatesMismatch` and a rollout can stall
with `persistentvolumeclaim "mount1" not found`. Recreate the StatefulSet and
the remaining old pods:

```sh
kubectl -n seaweedfs delete statefulset seaweedfs-volume --cascade=orphan
# Let the operator recreate the StatefulSet with both claim templates, then
# remove any pods still using the old single-disk template:
kubectl -n seaweedfs delete pod -l app.kubernetes.io/component=volume
kubectl -n seaweedfs rollout status statefulset/seaweedfs-volume --timeout=15m

# A two-disk volume server has mount0-* and mount1-* PVCs; expect 8 in total:
kubectl -n seaweedfs get pvc | grep -E 'mount[01]-seaweedfs-volume'
```

Existing PVCs are retained. New `mount1-*` PVCs pick up the recurring-job labels
from `volume.storageLabels` automatically; existing `mount0-*` PVCs were
labeled by hand during the original rollout.

Verify that each Longhorn volume loses `recurring-job-group.longhorn.io/default`
and gains `recurring-job-group.longhorn.io/daily-trim`:

```sh
kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns=NAME:.metadata.name,GROUPS:.metadata.labels
```

The `seaweedfs-master` PVC stays in the `default` backup group. Because the
data volume servers are excluded from Longhorn backup, the Thanos bucket on the
same servers is not backed up either. This is intentional: the media library is
treated as re-downloadable and the long-term metrics are not duplicated off
cluster.

## First-run configuration

1. **qBittorrent**: the template already configures `/data/downloads`,
   `/data/downloads/incomplete`, interface `tun0` (gluetun names its VPN interface tun0), host-header validation off,
   and the `10.42.0.0/16` authentication bypass. Verify these in
   Options > Downloads/Web UI. The permanent Web UI password is in the
   `qbittorrent-webui` Secret (`username`/`password`); use it if you need the
   direct-login break-glass page. UPnP/NAT-PMP is disabled.
2. **Prowlarr**: add indexers, then add Sonarr and Radarr as applications and
   sync. Use `http://sonarr.media.svc.cluster.local:8989` and
   `http://radarr.media.svc.cluster.local:7878` if you configure them manually.
3. **Sonarr/Radarr**: add qBittorrent at `http://qbittorrent.media.svc.cluster.local:8080`
   (any username/password works because of the subnet whitelist), set the root
   folders to `/data/media/tv` and `/data/media/movies`, and confirm downloads
   are imported from `/data/downloads`.
4. **Jellyfin**: the local `admin` account is already created; its password is
   in the `jellyfin-admin` Secret. Add libraries for `/data/media/movies` and
   `/data/media/tv`. The SSO-Auth 5.1.1 plugin is already installed under
   `/config/plugins/SSO-Auth` and configured with the `authentik` OIDC provider
   (issuer `https://auth.blalange.org/application/o/jellyfin/`, redirect
   `https://jellyfin.glem.men/sso/OID/redirect/authentik`). If you rebuild the
   config volume, reinstall the plugin from the K0lin manifest and re-add the
   provider with `POST /SSO/OID/Add/authentik`.
5. **Seerr**: add Jellyfin at `http://jellyfin.media.svc.cluster.local:8096`,
   then add Sonarr and Radarr with their API keys.

## Troubleshooting

- `media-data` Pending: check `kubectl -n seaweedfs get seaweedcsidriver` and
  the CSI controller/node pods. The media Kustomization waits for the driver.
- gluetun restarting: check its log for `/dev/net/tun`, endpoint DNS or
  WireGuard handshake errors. The `media` namespace is privileged, so
  `privileged: true` is a valid fallback.
- qBittorrent shows the Authentik page but then its own login: add
  `10.42.0.0/16` to the Web UI subnet whitelist.
- qBittorrent says `The configured network interface is invalid`: use `tun0`,
  not `wg0`. Gluetun creates the WireGuard tunnel as `tun0` inside the pod.
- Sonarr/Radarr/Prowlarr report `Version string portion was too short or too
  long` or `Failed to load system status`: they are connecting to YSQL. They
  must use SQLite; there is no `media-db` Secret. If an old Deployment still
  has `*__POSTGRES__*` env vars, remove them and recreate the Deployment
  (`kubectl -n media delete deploy <app>` then re-apply), because a normal
  `kubectl apply` can retain env entries owned by another manager.
- Longhorn backup job still includes a SeaweedFS data volume: check the PVC and
  Longhorn volume labels; the PVC must be the recurring-job source.
