# Media stack

Jellyfin, Sonarr, Radarr, Prowlarr, Profilarr, ytdl-sub, Seerr and
qBittorrent run in the `media` namespace. They share one CephFS-backed
`ReadWriteMany` volume at `/data`:

```
/data/downloads              qBittorrent downloads (incomplete/ below it)
/data/media/movies           Radarr root folder
/data/media/tv               Sonarr root folder
/data/ytdl-sub               ytdl-sub NRK library (Season NN + sidecars)
```

ytdl-sub mounts the same claim at `/media`, so its paths are
`/media/ytdl-sub` and `/media/.ytdl-sub-working`; Jellyfin sees the finished
library as `/data/ytdl-sub`.

Sonarr/Radarr import from `/data/downloads` into `/data/media` on the same
filesystem, so the normal move/rename path works and hardlinks are supported by
CephFS. The *arr apps can keep "Use Hardlinks instead of Copy" enabled.

| App | URL | Auth |
|---|---|---|
| Jellyfin | https://jellyfin.glem.men | Native OIDC (SSO-Auth plugin) |
| Sonarr | https://sonarr.glem.men | Authentik proxy, app auth `External` |
| Radarr | https://radarr.glem.men | Authentik proxy, app auth `External` |
| Prowlarr | https://prowlarr.glem.men | Authentik proxy, app auth `External` |
| Profilarr | https://profilarr.glem.men | Authentik proxy, app auth off |
| ytdl-sub | — (cron worker) | None; no web UI |
| Seerr | https://seerr.glem.men | Seerr login (built-in auth) |
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

ytdl-sub needs no Secret: the NRK subscriptions are public and live in the
`ytdl-sub-config` ConfigMap.

## Databases

The database-backed media applications use SQLite on their Ceph `nvme-rbd`
config volumes:

- Sonarr/Radarr/Prowlarr: `<app>.db` under `/config`
- Jellyfin: `/config/data/jellyfin.db`
- Seerr: `/config/db.sqlite3`
- Profilarr: `/config/data/profilarr.db`, with cloned profile repositories
  under `/config/data/databases`
- qBittorrent: its own settings database under `/config`

ytdl-sub has no database. It keeps a yt-dlp download archive named
`.ytdl-sub-<series>-download-archive.json` in each series folder under
`/data/ytdl-sub`; deleting it makes the next run treat every episode as new.

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

## ytdl-sub

ytdl-sub archives NRK series that are not available through Sonarr's
indexers. It is a yt-dlp wrapper: it downloads each season page, renames the
episodes into a Jellyfin layout and writes the metadata and artwork as sidecar
files. It has no web UI, Service, Ingress or Authentik provider.

| Series | NRK page | Seasons |
|---|---|---|
| Førstegangstjenesten | https://tv.nrk.no/serie/foerstegangstjenesten | 1-2 + Ekstramateriale |
| Monsen og nasjonalparkene | https://tv.nrk.no/serie/monsen-og-nasjonalparkene | 1-2 + Ekstramateriale |
| Med Monsen på villspor | https://tv.nrk.no/serie/monsen-paa-villspor | 1-4 + Ekstramateriale |
| Nytt på nytt | https://tv.nrk.no/serie/nytt-paa-nytt | 1999, 2011-2026 |
| Rådebank | https://tv.nrk.no/serie/raadebank | 1-3 + Ekstramateriale |
| Uti vår hage | https://tv.nrk.no/serie/uti-vaar-hage | 1-2 |
| Sámi Grand Prix | https://tv.nrk.no/serie/sami-grand-prix-tv | 2019, 2021-2026 + Ekstramateriale |
| Norske grønnsaker – sommersnutter | https://tv.nrk.no/serie/norske-groennsaker-sommersnutter | 1-4 |
| Canada på tvers | https://tv.nrk.no/serie/canada-paa-tvers | 1 |
| Distriktspolitiet | https://tv.nrk.no/serie/distriktspolitiet | 1 |
| Manndag | https://tv.nrk.no/serie/mann-dag | 1-2 |
| Flex | https://tv.nrk.no/serie/flex | 1 + Ekstramateriale |
| Verdens minste kommentatorboks | https://tv.nrk.no/serie/verdens-minste-kommentatorboks | 1-4 |

Everything is in the `ytdl-sub-config` ConfigMap (`ytdl-sub-config.yaml`),
mounted read-only at `/configs`:

- `config.yaml` - the custom Jellyfin TV collection preset. It is the upstream
  preset with `_episode_video_tags` removed and chapter embedding disabled, so
  ffmpeg never rewrites the finished file. It also sets the episode filename,
  subtitle naming and the NFO tags.
- `subscriptions.yaml` - the series, their seasons and the per-series genre,
  plot and rating.
- `cron` - the command the LinuxServer cron wrapper runs:
  `ytdl-sub --config /configs/config.yaml sub /configs/subscriptions.yaml`.

The container runs the cron schedule `15 4 * * *` (Europe/Oslo).
`CRON_RUN_ON_START=true` starts the first archive as soon as the pod is up, so
a fresh install does not wait for 04:15. A Git change to `config.yaml` or
`subscriptions.yaml` is picked up by the next run; a change to the `cron`
script itself needs a pod restart because an init container copies it to
`/config`, where the LinuxServer cron wrapper expects an executable file.

### Layout and metadata

The media PVC is mounted at `/media` and the library is `/media/ytdl-sub`.
Jellyfin mounts the same claim at `/data`, so add a **Shows** library for
`/data/ytdl-sub`. A series looks like:

```
Førstegangstjenesten/
  tvshow.nfo
  poster/season art when NRK provides it
  Season 01/
    Førstegangstjenesten - S01E01 - 1. Pilot - del 1 [MUHH35000119].mp4
    Førstegangstjenesten - S01E01 - 1. Pilot - del 1 [MUHH35000119].info.json
    Førstegangstjenesten - S01E01 - 1. Pilot - del 1 [MUHH35000119].nfo
    Førstegangstjenesten - S01E01 - 1. Pilot - del 1 [MUHH35000119]-thumb.jpg
    Førstegangstjenesten - S01E01 - 1. Pilot - del 1 [MUHH35000119].nb.vtt
  Season 00/                       Ekstramateriale (trailers and bonus clips)
```

- The `.nfo` files carry the title, plot, air date, season/episode, genre,
  content rating, year, studio (`NRK`), country and language.
- The `.info.json` files are yt-dlp's full metadata (description, thumbnail
  URLs, subtitles, formats). The filename matches the video basename and
  includes the yt-dlp ID in square brackets, which is the layout the
  `jellyfin-youtube-metadata-plugin` looks for. If that plugin is enabled for
  this library, put the NFO reader first in Jellyfin's local metadata reader
  order; see Troubleshooting.
- The `-thumb.jpg` files are episode thumbnails that Jellyfin reads natively.
  NRK has no channel avatar, so ytdl-sub may write a season poster but not a
  show `poster.jpg`; add `poster.jpg`/`fanart.jpg` to the series folder (or
  upload them in Jellyfin) if you want custom show art.
- Norwegian subtitles (`nb-ttv` from NRK) are written as `<video>.nb.vtt`.
- Episode numbers come from the NRK playlist order for the full-scan shows
  and Sámi Grand Prix. `Nytt på nytt` uses date-based numbers (`MMDD` plus a
  two-digit same-day index) because its year-seasons are listed newest first;
  a playlist index would renumber every episode when a new one airs. Season 0
  (Ekstramateriale) uses the upload date because those entries are individual
  program URLs with no playlist index.

### No transcoding or remuxing

ytdl-sub does not convert, remux or re-encode the media. No `file_convert`
plugin is configured, video tags and chapter embedding are disabled, and the
only ffmpeg step is yt-dlp merging NRK's separate HLS video and audio streams
into an `.mp4` (stream copy, same H.264/AAC codecs). Metadata lives in sidecar
files, not in the container.

### Adding series and seasons

Add a subscription under the `nrk_tv` or `nrk_tv_full_scan` key in
`subscriptions.yaml`. Each season is an `sNN_name`/`sNN_url` pair; the
collection preset supports up to 40 seasons. For example, when NRK starts the
2027 season of Nytt på nytt, add:

```yaml
s18_name: "2027"
s18_url: "https://tv.nrk.no/serie/nytt-paa-nytt/sesong/2027"
```

Use `nrk_tv` for newest-first season pages and `nrk_tv_full_scan` for
oldest-first pages. The full-scan preset disables `break_on_existing`: on an
oldest-first page yt-dlp would otherwise stop at episode 1 on every run and
never see newly published episodes at the end. The full-scan series are small
enough to rescan completely each day; Nytt på nytt and Sámi Grand Prix are
newest-first and keep the fast break-on-existing path (expired seasons are
still enumerated, but each season is a separate, small playlist).

NRK only serves this content from Norwegian addresses, and a season page can
list episodes that are not published yet ("Kommer om 14 dager") or have been
removed ("Ikke tilgjengelig lenger"). yt-dlp skips those; they are not errors.
Nytt på nytt's 1999 and 2011-2022 seasons are currently expired at NRK, so the
usable archive is 2023-2026 (about 115 episodes). The older seasons stay in
the config in case NRK republishes them.

The first archive is still sizeable: about 115 Nytt på nytt episodes plus
about 190 episodes of the other series. Expect roughly 250-450 GB at 1080p
(the 50 fps episodes are the larger ones). The media claim was grown by
3000Gi (about 3TB) for this library; see the capacity section below.

### Operations

The cron output goes to the container log (the LinuxServer stdout-logs mod
links `.cron.log` to stdout):

```sh
kubectl -n media logs deploy/ytdl-sub
```

To validate a change without downloading, run a dry run inside the pod:

```sh
kubectl -n media exec deploy/ytdl-sub -- \
  ytdl-sub --dry-run --config /configs/config.yaml sub /configs/subscriptions.yaml
```

Do not run a second real `ytdl-sub` process while cron is running: it can
download the same episode twice. The download archive is written after each
episode, so an interrupted run resumes where it stopped.

The image is pinned to `ghcr.io/jmbannon/ytdl-sub:2026.11.19`; update it with
the other media images. The container uses uid/gid 1000, writes to the media
volume as uid 1000, and keeps only cron state, logs and the LinuxServer
defaults on the `ytdl-sub-config-ceph` volume. `/tmp` is an emptyDir for
ytdl-sub's file locks.

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
Bind the Jellyfin application to `media-users`, and the proxy applications to
`media-admins`. Do not grant the admin-only apps to `media-users`: the *arr
apps and Profilarr run with their own authentication disabled and trust the
proxy. Seerr is not an Authentik application; it keeps its own login.

Create an outpost named `glemmen-media` of type Proxy with these providers:

| Application | Provider type | External host | Internal host |
|---|---|---|---|
| Sonarr | Proxy | https://sonarr.glem.men | http://sonarr.media.svc.cluster.local:8989 |
| Radarr | Proxy | https://radarr.glem.men | http://radarr.media.svc.cluster.local:7878 |
| Prowlarr | Proxy | https://prowlarr.glem.men | http://prowlarr.media.svc.cluster.local:9696 |
| Profilarr | Proxy | https://profilarr.glem.men | http://profilarr.media.svc.cluster.local:6868 |
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

Seerr is exposed directly rather than through the outpost because it has its
own login page. The stable image has no native OIDC, so users sign in with
their Jellyfin or local Seerr account. The `preview-new-oidc` image adds OIDC
but is not recommended for production.

## CephFS capacity and backups

The media library is `media-data-ceph`, a 5300Gi `ReadWriteMany` claim on the
`fast-fs` StorageClass. It was grown from 2300Gi by 3000Gi for the ytdl-sub
NRK archive. The CephFS data pool is `cephfs-fast` on the CMR OSDs in
glemmen130/glemmen150, replicated twice with `failureDomain: host` and
`min_size: 1`. The original library still uses about 2.0TiB; the added 3000Gi is
headroom for ytdl-sub. A PVC resize only raises the CephFS quota, so the
`fast-fs` pool must have the physical space. If it does not, move the claim to
`bulk-fs` as soon as the glemmen80 disks are available instead of filling the
CMR OSDs.

The migration copied the qBittorrent downloads and the existing Hærry Pot
movie with rsync, verified the file sizes and skipped unreadable source files.
The media Deployments were then switched to `media-data-ceph`; the old
SeaweedFS cluster and its volumes were removed once the copy was verified.
Media is re-downloadable and is not backed up off cluster; Ceph RF2 protects
against a single OSD or host failure.

### Library import and hardlinks

The rest of the library was re-imported from `/data/downloads` with Radarr and
Sonarr using `DownloadedMoviesScan` / `DownloadedEpisodesScan` and
`importMode: copy`. Both apps have `copyUsingHardlinks` enabled, so the import
created hardlinks in `/data/media` rather than copies. The media files share
inodes with the qBittorrent downloads, which is why both directories can be
used at the same time without doubling Ceph usage:

```sh
# Hardlinks are counted once when both paths are passed to du.
# downloads ~2.0T + media ~50G (the unique Hærry Pot file)
du -sh /data/downloads /data/media
```

Two releases are still RAR-only and were not imported because extracting them
would create a second copy and break seeding:

- `Charterfeber.SE.S01.SWEDiSH.1080p.WEB.h264-OLLONBORRE` (Sonarr 0/206)
- `Key.And.Peele.S04.NORDiC.1080p.WEB.x264-PE2PE`

The higher-quality `Foundation (2021) ... HONE` releases were imported; the
smaller duplicate `Vyndros` sets remain in downloads. The English `Once Upon a
Time... Man` Bluray set was imported; the multi-series French `Il Etait Une
Fois` pack remains in downloads. Radarr still reports missing movies only where
there is no complete download (Kung Fu Panda 2/3, Porco Rosso, The Hunger
Games: Mockingjay Parts 1/2 and Sunrise on the Reaping) or the torrent is
incomplete (`Alibi.com`, `Alibi.com 2`).

Some old SeaweedFS EC volumes had corrupt metadata and could not be read; the
rsync job logs those files and skips them. Re-download them through qBittorrent
if the corresponding media is still wanted.

When the 24 × 12TB disks and SAS HBA are installed in glemmen80, add the bulk
CephFS pool and move this claim to `bulk-fs`. Check the pool's free space
before growing the fast-tier claim again; the 3000Gi growth for ytdl-sub is the
last planned increase on `fast-fs`.

## Errored downloads

qBittorrent can leave torrents in the `error` state after gluetun reconnects or
a transient tracker/disk failure. The `qbittorrent-resume-errored` CronJob runs
every 15 minutes, asks `/api/v2/torrents/info?filter=errored` for the torrent
hashes and resumes them with `/api/v2/torrents/start`. It reaches the
ClusterIP Service from a pod IP in `10.42.0.0/16`, which is in qBittorrent's
Web UI authentication bypass subnet, so it needs no credentials.

The script builds the URL from `QBITTORRENT_SERVICE_HOST` and
`QBITTORRENT_SERVICE_PORT`, which kubelet injects for the same-namespace
Service, so the first hop does not depend on CoreDNS. `QBITTORRENT_URL` is
still accepted as an override for manual runs. Curl uses `--retry-all-errors`
so transient DNS/connection failures are retried instead of being masked, and
`set -e` plus `--fail-with-body` make a failed API call exit nonzero.

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
   in the `jellyfin-admin` Secret. Add libraries for `/data/media/movies`,
   `/data/media/tv` and `/data/ytdl-sub` (Shows). The SSO-Auth 5.1.1 plugin is
   already installed under `/config/plugins/SSO-Auth` and configured with the
   `authentik` OIDC provider (issuer
   `https://auth.blalange.org/application/o/jellyfin/`, redirect
   `https://jellyfin.glem.men/sso/OID/redirect/authentik`). If you rebuild the
   config volume, reinstall the plugin from the K0lin manifest and re-add the
   provider with `POST /SSO/OID/Add/authentik`.
5. **Seerr**: add Jellyfin at `http://jellyfin.media.svc.cluster.local:8096`,
   then add Sonarr and Radarr with their API keys.
6. **Profilarr**: add Sonarr and Radarr under Settings > Instances with the
   internal URLs above and their API keys, then link a profile database and
   sync. Do this after Prowlarr has added the indexers and the Arr apps are
   otherwise configured.
7. **ytdl-sub**: nothing to create. The subscriptions are in Git and the cron
   job runs on pod start. Add the Jellyfin Shows library for `/data/ytdl-sub`
   (step 4), enable the built-in NFO metadata reader, and check the first run
   with `kubectl -n media logs -f deploy/ytdl-sub`. The NRK archive is large;
   let the first run finish before adding more series.

## Troubleshooting

- `media-data-ceph` Pending: check `kubectl -n rook-ceph get cephcluster` and
  the CephFS CSI controller/node pods. The media Kustomization waits for the
  claim.
- gluetun restarting: check its log for `/dev/net/tun`, endpoint DNS or
  WireGuard handshake errors. The `media` namespace is privileged, so
  `privileged: true` is a valid fallback.
- gluetun's startup probe fails with `context deadline exceeded` and the
  container is killed about every five minutes: its firewall defaults to
  `INPUT DROP`, and `FIREWALL_INPUT_PORTS` must include **both** `8080` (Web UI)
  and `9999` (health server) or kubelet cannot reach the probes.
- gluetun loops on `adding IPv4/IPv6 rule ... file exists`: a previous container
  in the shared pod network namespace left the `table 51820` rules behind. Keep
  `strategy: Recreate` and the `postStart` hook that deletes the IPv4 and IPv6
  rules. Gluetun's own OOM kill (a 256Mi limit is too small during blocklist
  download) also leaves the rules behind, so keep the 512Mi limit.
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
- ytdl-sub downloads at 480p or stops with `downloaded at a low resolution ...
  you've probably been throttled`: NRK rate-limited the run. The resolution
  assert stops the archive rather than filling it with bad copies; wait a few
  hours and run again. Set `enable_resolution_assert: False` only if low
  resolution is acceptable.
- ytdl-sub logs `Ikke tilgjengelig lenger` or `Kommer om ... dager`: the season
  page lists an episode that has been removed or is not published yet. yt-dlp
  skips it; no action is needed.
- ytdl-sub reports `No files changed` for every series: the yt-dlp download
  archive already contains those episodes. Delete
  `.ytdl-sub-<series>-download-archive.json` in the series folder to force a
  re-check, or confirm the ConfigMap still has the expected `sNN_url` entries.
- Jellyfin shows every ytdl-sub episode in Season 1: the
  `jellyfin-youtube-metadata-plugin` local episode provider always reports
  season 1. Put the built-in NFO reader (or `Local Metadata`) before
  `YoutubeMetadata` in the `/data/ytdl-sub` library's local metadata reader
  order, or disable YoutubeMetadata for that library; the generated `.nfo`
  files contain the correct season and episode. The `.info.json` sidecars are
  still written for the plugin.
- A new Nytt på nytt or Sámi Grand Prix year is missing: add the next
  `sNN_name`/`sNN_url` pair under that subscription and commit. The season page
  is not discovered automatically, and the collection preset supports up to 40
  seasons.
- A ConfigMap edit is not visible yet: the file is mounted read-only at
  `/configs` and Kubernetes can take about a minute to update it. The next
  cron run reads the new file; a manual `kubectl -n media rollout restart
  deploy/ytdl-sub` is the reliable option.
