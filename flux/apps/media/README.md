# Media stack

Jellyfin, Sonarr, Radarr, Prowlarr, Profilarr, Seerr and qBittorrent run in the
`media` namespace. They share one SeaweedFS-backed `ReadWriteMany` volume at
`/data`:

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
| Profilarr | https://profilarr.glem.men | Authentik proxy, app auth off |
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
- Profilarr: `/config/data/profilarr.db`, with cloned profile repositories
  under `/config/data/databases`
- qBittorrent: its own settings database under `/config`

YugabyteDB was evaluated for Sonarr/Radarr/Prowlarr, but their
`DatabaseVersionParser` calls `.NET Version.Parse()` on YSQL's
`server_version` string (`15.12-YB-2026.1.3.0-b100`), which fails. They therefore
fall back to the supported SQLite backend. `yugabyte-init-v6` no longer creates
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

## Profilarr

Profilarr syncs quality profiles, custom formats, delay profiles and media
management settings from a profile database (TRaSH Guides, Dictionarry, ...)
into Sonarr and Radarr. It is an admin-only app and its own login is disabled
(`AUTH=off`); access is only through the Authentik proxy outpost. Do not create
an Ingress that points at the Profilarr Service directly, or the UI will be
reachable without authentication.

`ORIGIN` is set to `https://profilarr.glem.men` because SvelteKit validates the
request origin behind the reverse proxy. If the public hostname changes, update
it in `profilarr.yaml` as well as the Authentik provider.

The parser (`profilarr-parser`) backs the custom-format and quality-profile
testing features. It is internal-only at
`http://profilarr-parser.media.svc.cluster.local:5000`; syncing works without
it, but the testers return errors if the Deployment is down. Both images are
pinned to `2.3.1`; keep them on the same tag when updating.

Profilarr stores its SQLite database, cloned profile repositories, logs and
backups on the `profilarr-config` volume. The Arr API keys are stored in that
database, so the volume must be retained (the `media-config` StorageClass uses
`reclaimPolicy: Retain`).

To connect the Arr apps, add them under Settings > Instances:

| App | Internal URL |
|---|---|
| Sonarr | `http://sonarr.media.svc.cluster.local:8989` |
| Radarr | `http://radarr.media.svc.cluster.local:7878` |

Use the API key from each app's `config.xml`, for example:

```sh
kubectl -n media exec deploy/sonarr -- sh -c \
  'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -d">" -f2'
```

Then link a database and sync the entities you want. The first sync can create
a lot of custom formats; review the changes in Profilarr before pushing them.

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
`media-admins` (Sonarr/Radarr/Prowlarr/Profilarr/qBittorrent administrators).
Bind the Jellyfin and Seerr applications to `media-users`, and the other proxy
applications to `media-admins`. Do not grant the admin-only apps to
`media-users`: the *arr apps and Profilarr run with their own authentication
disabled and trust the proxy.

Create an outpost named `glemmen-media` of type Proxy with these providers:

| Application | Provider type | External host | Internal host |
|---|---|---|---|
| Sonarr | Proxy | https://sonarr.glem.men | http://sonarr.media.svc.cluster.local:8989 |
| Radarr | Proxy | https://radarr.glem.men | http://radarr.media.svc.cluster.local:7878 |
| Prowlarr | Proxy | https://prowlarr.glem.men | http://prowlarr.media.svc.cluster.local:9696 |
| Profilarr | Proxy | https://profilarr.glem.men | http://profilarr.media.svc.cluster.local:6868 |
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

EC metadata failures (stale `.vif` stubs causing `different encode run` or
`size mismatch` errors) are diagnosed and repaired using the runbook in
[`flux/infrastructure/seaweedfs/README.md`](../../infrastructure/seaweedfs/README.md).

The media PVC is 2500Gi. It is stored in a SeaweedFS cluster with four
operator-managed volume servers, each with four 200Gi Longhorn disks
(`volumeServerDiskCount: 4`), for 3.2TiB of Longhorn-backed raw capacity. A
fifth volume server on `glemmen60` writes directly to
`/var/mnt/hdd1/seaweedfs` instead of Longhorn and adds 6TB (5.46TiB) of raw
volume slots (5588 x 1GiB). The CSI StorageClass requests `replication: "001"`
for new volumes, while the older media volumes are erasure-coded 10+4.
Conservatively, the combined 8.66TiB of raw capacity supports about 4.33TiB of
logical data if every volume were replicated 001; the 2500Gi (2.44TiB) media
PVC fits with room for the Thanos bucket and metadata.

The fifth server is a separate Flux HelmRelease using the official SeaweedFS
chart (`flux/infrastructure/seaweedfs/volume-glemmen60.yaml`). It is not part
of the operator's `seaweedfs-volume` StatefulSet, so the StatefulSet recreate
procedure below does not touch it. The chart's `volume.id` is intentionally
left unset: some SeaweedFS maintenance tasks dial the node ID directly, so the
ID must be the pod DNS address with a port rather than a bare `glemmen60`.
The Longhorn disk on `glemmen60` reserves 6TB (`storageReserved`), so Longhorn
will not schedule replicas into the space promised to the SeaweedFS server.

`volumeServerDiskCount` is a top-level Seaweed field, not a field under
`volume`. Changing it, `volume.replicas` or `volume.requests.storage` changes
the StatefulSet's `volumeClaimTemplates`, which Kubernetes forbids updating in
place. The operator logs `VolumeClaimTemplatesMismatch` and a rollout can stall
with `persistentvolumeclaim "mount1" not found`. Recreate the StatefulSet and
the remaining old pods:

```sh
kubectl -n seaweedfs delete statefulset seaweedfs-volume --cascade=orphan
# Let the operator recreate the StatefulSet with all claim templates, then
# remove any pods still using the old template:
kubectl -n seaweedfs delete pod -l app.kubernetes.io/component=volume
kubectl -n seaweedfs rollout status statefulset/seaweedfs-volume --timeout=15m

# Four-disk volume servers have mount0-* through mount3-* PVCs; expect 16:
kubectl -n seaweedfs get pvc | grep -E 'mount[0-3]-seaweedfs-volume'
```

Existing PVCs are retained. New PVCs pick up the recurring-job labels from
`volume.storageLabels` automatically; the original `mount0-*` PVCs were
labeled by hand during the first rollout.

Verify that each Longhorn volume loses `recurring-job-group.longhorn.io/default`
and gains `recurring-job-group.longhorn.io/daily-trim`:

```sh
kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns=NAME:.metadata.name,GROUPS:.metadata.labels
```

The `seaweedfs-master` PVC stays in the `default` backup group. Because the
data volume servers are excluded from Longhorn backup, and the fifth server is
not Longhorn-managed at all, the Thanos bucket on the same servers is not
backed up either. This is intentional: the media library is treated as
re-downloadable and the long-term metrics are not duplicated off cluster.

## Errored downloads

qBittorrent can leave torrents in the `error` state after gluetun reconnects or
a transient tracker/disk failure. The `qbittorrent-resume-errored` CronJob runs
every 15 minutes, asks `/api/v2/torrents/info?filter=errored` for the torrent
hashes and resumes them with `/api/v2/torrents/start`. It reaches the
ClusterIP Service from a pod IP in `10.42.0.0/16`, which is in qBittorrent's
Web UI authentication bypass subnet, so it needs no credentials.

qBittorrent 5.x renamed the old `/api/v2/torrents/resume` endpoint to
`/api/v2/torrents/start`; the old endpoint returns `404 Endpoint does not
exist` on 5.3.0. The CronJob is pinned to the 5.x image, so it uses `start`.
If the image is ever downgraded below 5.0, switch the script back to `resume`.

To trigger it without waiting for the next run:

```sh
kubectl -n media create job --from=cronjob/qbittorrent-resume-errored resume-now
kubectl -n media logs -f job/resume-now
```

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
6. **Profilarr**: add Sonarr and Radarr under Settings > Instances with the
   internal URLs above and their API keys, then link a profile database and
   sync. Do this after Prowlarr has added the indexers and the Arr apps are
   otherwise configured.

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
- qBittorrent keeps restarting with `OOMKilled`: do not use a build with
  libtorrent 2.1.x. qBittorrent 5.3.0 with libtorrent 2.1.2 leaks memory while
  downloading ([qbittorrent/qBittorrent#25202](https://github.com/qbittorrent/qBittorrent/issues/25202)).
  The image is pinned to the 2.0.15 build; bump it only after the upstream
  issue is fixed and verified.
- Sonarr/Radarr/Prowlarr report `Version string portion was too short or too
  long` or `Failed to load system status`: they are connecting to YSQL. They
  must use SQLite; there is no `media-db` Secret. If an old Deployment still
  has `*__POSTGRES__*` env vars, remove them and recreate the Deployment
  (`kubectl -n media delete deploy <app>` then re-apply), because a normal
  `kubectl apply` can retain env entries owned by another manager.
- Longhorn backup job still includes a SeaweedFS data volume: check the PVC and
  Longhorn volume labels; the PVC must be the recurring-job source.
