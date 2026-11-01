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

## Databases

Sonarr/Radarr/Prowlarr use two YSQL databases each (`<app>-main` and
`<app>-log`). The roles/databases are created by
`yugabyte-init-v4` in `flux/infrastructure/yugabyte/`. Jellyfin and Seerr keep
SQLite on their Longhorn config volumes (Jellyfin has no supported PostgreSQL
backend; Seerr's TypeORM migrations do not complete reliably on YSQL).
qBittorrent stores its settings in its config volume.

The init Job reads passwords from `yugabyte-auth` in the `yugabyte` namespace.
Create the three keys there and the matching `media-db` Secret in `media` before
reconciling:

```sh
kubectl -n yugabyte patch secret yugabyte-auth --type merge -p "$(cat <<EOF
{"data":{
  "sonarr-password":"$(printf %s "$(openssl rand -hex 24)" | base64 -w0)",
  "radarr-password":"$(printf %s "$(openssl rand -hex 24)" | base64 -w0)",
  "prowlarr-password":"$(printf %s "$(openssl rand -hex 24)" | base64 -w0)"
}}
EOF
)"
```

Then create `media-db` with the same values. The simplest way is to read the
three keys back from `yugabyte-auth`:

```sh
kubectl -n media create secret generic media-db \
  --from-literal=sonarr-password="$(kubectl -n yugabyte get secret yugabyte-auth -o jsonpath='{.data.sonarr-password}' | base64 -d)" \
  --from-literal=radarr-password="$(kubectl -n yugabyte get secret yugabyte-auth -o jsonpath='{.data.radarr-password}' | base64 -d)" \
  --from-literal=prowlarr-password="$(kubectl -n yugabyte get secret yugabyte-auth -o jsonpath='{.data.prowlarr-password}' | base64 -d)" \
  --dry-run=client -o yaml | kubectl apply -f -
```

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

The media PVC is 350Gi on four 100Gi SeaweedFS volume servers (400Gi total; the
remainder is headroom for the Thanos bucket and metadata). The `Seaweed` CR
already requests four 100Gi volumes with required pod anti-affinity.

The current cluster has two 50Gi volume-server PVCs. On the first rollout, after
Flux applies the new CR:

```sh
# Recreate the StatefulSet so the immutable volumeClaimTemplate becomes 100Gi.
# Orphaned pods keep running and are adopted by the new StatefulSet.
kubectl -n seaweedfs delete statefulset seaweedfs-volume --cascade=orphan
kubectl -n seaweedfs rollout status statefulset/seaweedfs-volume --timeout=15m

# Expand the two existing PVCs to the new size.
for pvc in mount0-seaweedfs-volume-0 mount0-seaweedfs-volume-1; do
  kubectl -n seaweedfs patch pvc "$pvc" --type merge \
    -p '{"spec":{"resources":{"requests":{"storage":"100Gi"}}}}'
done

# Exclude the existing data PVCs from the Longhorn daily-backup job while
# keeping daily-trim. New PVCs get these labels from volume.storageLabels.
kubectl -n seaweedfs label pvc mount0-seaweedfs-volume-0 mount0-seaweedfs-volume-1 \
  recurring-job.longhorn.io/source=enabled \
  recurring-job-group.longhorn.io/daily-trim=enabled --overwrite
```

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

1. **qBittorrent**: get the temporary Web UI password from the container log if
   the UI asks for one, then set a permanent password. Set the default save path
   to `/data/downloads`, the incomplete path to `/data/downloads/incomplete`,
   and the network interface to `wg0`. Enable "Bypass authentication for clients
   in whitelisted IP subnets" with `10.42.0.0/16` so the Authentik outpost and
   the *arr apps can use the API without a second login. Disable UPnP/NAT-PMP.
2. **Prowlarr**: add indexers, then add Sonarr and Radarr as applications and
   sync. Use `http://sonarr.media.svc.cluster.local:8989` and
   `http://radarr.media.svc.cluster.local:7878` if you configure them manually.
3. **Sonarr/Radarr**: add qBittorrent at `http://qbittorrent.media.svc.cluster.local:8080`
   (any username/password works because of the subnet whitelist), set the root
   folders to `/data/media/tv` and `/data/media/movies`, and confirm downloads
   are imported from `/data/downloads`.
4. **Jellyfin**: add libraries for `/data/media/movies` and `/data/media/tv`;
   install and configure the SSO plugin.
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
- Sonarr/Radarr cannot connect to YSQL: check the app log and `media-db`
  Secret. If an app's schema is incompatible with YSQL, switch that one app to
  SQLite by removing its `*__POSTGRES__*` env vars; the config volume remains.
- Longhorn backup job still includes a SeaweedFS data volume: check the PVC and
  Longhorn volume labels; the PVC must be the recurring-job source.
