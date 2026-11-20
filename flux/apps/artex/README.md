# ARTEX

[ARTEX](https://github.com/BaaRai/ARTEX) is an LLM-driven autonomous
penetration-testing platform. The Go backend embeds the Next.js UI and stores
its data in PostgreSQL. It is exposed at <https://artex.glem.men> through the
`glemmen-artex` Authentik proxy outpost.

## Components

| Resource | Purpose |
|---|---|
| `postgres.yaml` | `artex-postgres` StatefulSet, 8 GiB `nvme-rbd` volume |
| `artex.yaml` | ARTEX Deployment, 20 GiB data volume and 1 GiB keys volume on `nvme-rbd` |
| `authentik-outpost.yaml` | Dedicated Authentik proxy outpost for `artex.glem.men` |
| `ingress.yaml` | Traefik Ingress -> outpost `:9000` -> ARTEX `:8787` |

ARTEX is pinned to `glemmen80` (192 GiB RAM, 1 TB system disk) because the
image bundles Chromium, bbot and a scanner toolchain, and three concurrent
workers can be memory-hungry.

## Database

ARTEX uses a dedicated PostgreSQL 18.6 instance, not the shared YugabyteDB
cluster. Its baseline schema uses GiST indexes on `inet`/`cidr`, a GIN index on
`JSONB[]`, and a `UNIQUE` constraint on a `CIDR`, which YSQL does not
implement. Patching ARTEX was explicitly avoided; the dedicated database keeps
the upstream image unmodified.

The database is internal-only. `artex-db` holds the password and the complete
DSN:

```sh
PG_PW="$(openssl rand -hex 24)"

kubectl -n artex create secret generic artex-db \
  --from-literal=password="$PG_PW" \
  --from-literal=dsn="postgres://artex:${PG_PW}@artex-postgres.artex.svc.cluster.local:5432/artex?sslmode=disable"
```

The official PostgreSQL image only applies `POSTGRES_PASSWORD` while the data
directory is empty. Rotating the Secret later does not change the role's
password; use `ALTER ROLE artex PASSWORD ...` as well and update both Secret
keys.

## Image

ARTEX does not publish a prebuilt image. Build the pinned upstream release
(unmodified source) and push it to the registry used by the Deployment:

```sh
docker login ghcr.io
./scripts/artex/build-image.sh
# defaults: v0.6.0 -> ghcr.io/blalange/artex:v0.6.0
```

The script clones the tag, builds the upstream Dockerfile for `linux/amd64`
and pushes it. The GHCR package must be public for the cluster to pull it
without an image pull secret; if it is private, create one in the `artex`
namespace and add `imagePullSecrets` to the Deployment.

To upgrade, bump `ARTEX_VERSION` in the script and the image tag in
`artex.yaml`, rebuild, and commit. ARTEX applies its embedded schema and
pending migrations at startup; back up PostgreSQL, `/app/data` and
`/app/keys` before upgrading.

## Authentik

The following objects exist in Authentik (`https://auth.blalange.org`):

| Object | Value |
|---|---|
| Proxy provider | `ARTEX` |
| Application | `ARTEX` (slug `artex`) |
| Group | `artex-admins`, bound to the application |
| Outpost | `glemmen-artex`, type Proxy, assigned to the provider |

Provider settings:

- External host: `https://artex.glem.men`
- Internal host: `http://artex.artex.svc.cluster.local:8787`
- Mode: Proxy
- Authorization flow: `default-provider-authorization-explicit-consent`
- Invalidation flow: `default-provider-invalidation-flow`
- Intercept header authentication: **off**. ARTEX sends its own
  `Authorization: Bearer <JWT>` on API calls; with interception enabled the
  outpost would treat that JWT as an Authentik token and return 401. Disabling
  it lets the outpost use the Authentik session cookie and pass the ARTEX
  header through unchanged.

The outpost is deployed by this Kustomization, not by an Authentik service
connection. Store its token in the hand-created Secret:

```sh
kubectl -n artex create secret generic artex-authentik-outpost \
  --from-literal=token='<outpost token>'
```

The token is available from the Authentik API as
`GET /api/v3/core/tokens/<outpost token identifier>/view_key/`. Add users to
`artex-admins` to grant access; users outside the group are rejected by the
outpost.

The outpost proxies SSE connections from ARTEX's activity stream. If a future
proxy change breaks streaming, the alternative is Traefik forward-auth, with
Traefik proxying directly to the ARTEX Service and only authentication checks
going to the outpost.

## First run

A fresh ARTEX installation is not initialised until you complete `/setup`. The
first visit requires the one-time setup token printed at startup:

```sh
kubectl -n artex logs deploy/artex | grep "Setup token"
```

Sign in through Authentik, enter the setup token, and set the local ARTEX
administrator password. Then configure LLM providers under **System -> LLM** in
the UI. No LLM keys are configured in Git or in the Deployment; until a
provider is added the backend logs:

```text
[engine] no LLM provider configured - engine idle until set via /api/llm or env
```

LLM keys entered in the UI are encrypted in PostgreSQL with
`/app/keys/secret.key`. Back up the keys volume with the database; losing the
key makes the stored provider credentials unreadable.

## Persistence and backup

| Volume | Contents |
|---|---|
| `artex-postgres-ceph` | PostgreSQL data |
| `artex-keys-ceph` | `jwt.key` and `secret.key` |
| `artex-data-ceph` | Traffic index, task files, transcripts and evidence |

There is no off-cluster backup for any of these volumes. The keys and database
must be backed up together; the data volume is needed to restore captured
traffic and task archives. The PostgreSQL and data volumes use Ceph RF2 on the
NVMe OSDs.

## Security notes

ARTEX is an offensive-security tool. Use it only against systems you own or
are explicitly authorised to test. The public endpoint is protected by
Authentik and the application also has its own JWT login; keep the
`artex-admins` group small.

The container runs as root so the server can drop each agent process to the
unprivileged executor, and it keeps the default capability set for
`NET_RAW` (raw nmap scans) and `SETUID`/`SETGID` (the executor drop). It does
not mount a service-account token and has no Kubernetes API permissions.

## Validation

```sh
kubectl -n artex get pods,pvc
kubectl -n artex port-forward svc/artex 8787:8787
curl -fsS http://127.0.0.1:8787/api/health
curl -sI https://artex.glem.men   # redirects to Authentik
```
