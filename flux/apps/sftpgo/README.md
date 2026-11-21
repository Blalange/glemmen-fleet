# SFTPGo file access

`flux/apps/sftpgo` runs [SFTPGo](https://github.com/drakkan/sftpgo) as the
cluster's file service:

- **WebDAV** at <https://dav.glem.men/>
- **SFTP** at `sftp.glem.men:22022` (and `192.168.6.99:22022` on the LAN)
- **Homes** on a 100 GiB CephFS volume on the CMR `fast-fs` tier
- **Authentication** against Authentik through its LDAP outpost

Each Authentik user is auto-created in SFTPGo on first login and gets
`/data/homes/<username>` with full permissions below that directory. There is
no Samba/SMB service in the cluster.

## Architecture

| Resource | Purpose |
| --- | --- |
| `sftpgo-homes-ceph` PVC | `fast-fs`, 100Gi, `ReadWriteMany`; per-user homes under `/data/homes` |
| `sftpgo-state-ceph` PVC | `nvme-rbd`, 5Gi; SQLite DB, SSH host keys and DB backups |
| `Deployment/sftpgo` | SFTPGo WebDAV (8081) and SFTP (2022 internal); HTTPD 8080 stays internal |
| `Deployment/authentik-ldap` | in-cluster Authentik LDAP outpost (3389) |
| `Service/sftpgo-webdav` | ClusterIP backend for the Traefik Ingress |
| `Service/sftpgo-sftp` | LoadBalancer on `192.168.6.99:22022` |
| `Ingress/sftpgo-webdav` | `https://dav.glem.men` with the wildcard TLS certificate |

The SFTPGo image does not include the LDAP auth plugin. An init container
downloads the pinned `sftpgo-plugin-auth` release into a shared `emptyDir`,
checks its SHA256, and SFTPGo checks the same hash again before spawning it.
The plugin uses `--ldap-bind-dn` to search and then binds as the user to verify
the password.

## Authentik setup

Authentik itself is external (`https://auth.blalange.org`). The objects below
are created by hand in the Authentik admin UI. The names and base DN must match
the SFTPGo Deployment.

### 1. LDAP authentication flow

WebDAV and SFTP clients cannot complete a browser login or an MFA challenge on
every request. Create a dedicated LDAP authentication flow without MFA, with a
password stage that accepts **User database + app passwords**:

1. Create a **Password stage** `ldap-password` with backends
   `User database + app passwords`.
2. Create an **Identification stage** `ldap-identification` with the fields
   `Username` and `Email`, and select `ldap-password` as its password stage.
3. Create a **User Login stage** `ldap-login`.
4. Create an **Authentication flow** `ldap-authentication` with stage bindings
   in order: identification (10), login (30).

Users who need MFA can create an **app password** in Authentik and use that as
their WebDAV/SFTP password. The app password satisfies the password stage.

If you prefer the default flow instead, users with TOTP/DUO must append
`;123456` to their password, which most WebDAV/SFTP clients handle poorly.

### 2. Service account and search permission

The plugin needs a read-only account to search the directory:

1. Create a user `ldapservice` with a strong password (or an app password).
2. Create a role `SFTPGo LDAP search` and assign `ldapservice` to it.
3. Create the LDAP provider first (next section), then open its **Permissions**
   tab and use **Assign Role Object Permission** to give that role the
   **Search full LDAP directory** permission.
4. Give `ldapservice` access to the `SFTPGo` LDAP application. If the
   application uses policy bindings, include the service account or the group
   that contains it.

### 3. LDAP provider and application

Create an application `SFTPGo` with an **LDAP provider**:

- **Name**: `SFTPGo`
- **Base DN**: `ou=sftpgo,dc=ldap,dc=goauthentik,dc=io`
- **Bind flow**: `ldap-authentication`
- **Search mode**: cached is fine; direct is also supported
- **Code-based MFA support**: off for this flow

Create a group `sftpgo-users` and bind the `SFTPGo` application to it. The group
must contain the human users, `ldapservice`, and the LDAP outpost service
accounts (`ak-outpost-...`), because Authentik runs the application access
check with those service identities as well. A user who is not in the group is
rejected by the provider's authorization flow even if the LDAP bind succeeds.

The base DN gives this provider its own namespace in Authentik. Users are
under `ou=users,ou=sftpgo,dc=ldap,dc=goauthentik,dc=io`, so the service
account DN is:

```text
cn=ldapservice,ou=users,ou=sftpgo,dc=ldap,dc=goauthentik,dc=io
```

The Deployment searches the user subtree
(`SFTPGO_PLUGIN_AUTH_LDAP_BASE_DN=ou=users,ou=sftpgo,...`), not the provider
root, so group entries and virtual groups are not returned. If you use a
different provider base DN, update that value and
`SFTPGO_PLUGIN_AUTH_LDAP_BIND_DN` in `deployment.yaml` as well.

### 4. LDAP outpost

Create an outpost named `glemmen-sftpgo-ldap`:

- **Type**: LDAP
- **Integration**: none/manual (this Deployment is the outpost)
- **Applications**: `SFTPGo`

Copy the generated outpost token. The outpost connects outbound to
`https://auth.blalange.org`; its Service is only reachable inside the cluster
as `authentik-ldap.sftpgo.svc.cluster.local:3389`.

The outpost also exposes LDAPS on 6636 if you later configure a certificate on
the LDAP provider. `deployment.yaml` currently uses plain LDAP inside the
cluster; switch the plugin URL to `ldaps://...:6636` for encrypted LDAP.

### 5. Create the Secret

The token and the `ldapservice` password are never stored in Git:

```sh
kubectl -n sftpgo create secret generic sftpgo-authentik \
  --from-literal=token='<outpost token>' \
  --from-literal=ldap-password='<ldapservice password>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

The Deployment reads `token` for the outpost and `ldap-password` for the
plugin's bind DN. If either is wrong, the outpost or login fails; check the
outpost and SFTPGo logs.

## Users and permissions

- Any user allowed by the Authentik application can log in; no SFTPGo local
  account is needed.
- On first login the plugin creates the user in SFTPGo with
  `HomeDir=/data/homes/<username>` and permissions `*` on `/`. SFTPGo confines
  each session to its home directory.
- The plugin caches successful binds for 300 seconds
  (`SFTPGO_PLUGIN_AUTH_CACHE_TIME`). A password change in Authentik can take up
  to five minutes to take effect on new WebDAV/SFTP requests.
- Access is controlled by the `sftpgo-users` Authentik group bound to the
  application. Add or remove users there; the `ldapservice` bind account and
  the outpost service accounts must stay in the group for search and access
  checks to work.
- There is no per-user quota configured. Add SFTPGo groups/quotas later if the
  shared CephFS volume needs limits.

## Using it

### WebDAV

```sh
curl -u 'USER:PASSWORD' -X PROPFIND https://dav.glem.men/ \
  -H 'Depth: 1'
```

Clients:

- macOS Finder: **Go -> Connect to Server** -> `https://dav.glem.men/`.
- rclone: `rclone config create dav webdav url=https://dav.glem.men vendor=other user=USER`.
- Windows: map `https://dav.glem.men/` with the WebClient service enabled.
  Windows WebDAV has a 50 MB default file-size limit and requires Basic auth
  over TLS; SMB was a better native Windows drive protocol, but it is no longer
  deployed.

### SFTP

```sh
sftp -P 22022 USER@sftp.glem.men
```

The SSH host keys live on `sftpgo-state-ceph`, so the fingerprint stays stable
across pod restarts. UniFi forwards **TCP 22022** to `192.168.6.99`; the
public Service maps 22022 to SFTPGo's internal 2022 listener because external
port 2022 is already used by another host. On the LAN use `192.168.6.99` or
`sftp.glem.men` with port 22022.

external-dns creates `sftp.glem.men` pointing at the WAN address
`109.247.229.146`. Update the Service annotation if the WAN address changes.

## Storage and backups

- Homes: `sftpgo-homes-ceph` on `fast-fs` (CMR OSDs), `Retain`, 100Gi.
- State: `sftpgo-state-ceph` on `nvme-rbd`, 5Gi; keeps the SQLite database,
  auto-generated SSH host keys and SFTPGo's database backups.
- No off-cluster backup is configured. Ceph RF2 protects against a single
  OSD/host failure; back up important home data separately.

## Security notes

- WebDAV uses HTTP Basic auth over TLS. User passwords and the
  `ldapservice` bind password travel from SFTPGo to the in-cluster LDAP outpost
  over plain LDAP on the pod network; the outpost talks to Authentik over
  HTTPS. Use LDAPS/StartTLS if you want the pod-network hop encrypted as well.
- The SFTPGo HTTPD/WebAdmin/REST listener on 8080 is disabled from the Ingress
  and has WebAdmin/WebClient/REST turned off. Use `kubectl port-forward` if
  you ever need to inspect it.
- The plugin release is pinned and verified by SHA256 in both the init
  container and SFTPGo's plugin configuration.

## Troubleshooting

```sh
kubectl -n sftpgo get pods,pvc,svc,ingress
kubectl -n sftpgo logs deployment/sftpgo -c sftpgo
kubectl -n sftpgo logs deployment/authentik-ldap
```

- `CreateContainerConfigError` for `sftpgo`: the `sftpgo-authentik` Secret is
  missing or lacks `token`/`ldap-password`.
- `user "..." does not exist` in the plugin logs: the LDAP search base/filter
  or the service account's **Search full LDAP directory** permission is wrong.
- `invalid credentials` with a correct Authentik password: the LDAP provider's
  bind flow is rejecting the bind (MFA, policy binding, or the user lacks
  access to the LDAP application). Use an app password or the dedicated
  password-only flow.
- WebDAV returns 401: test the same credentials with `curl` and check the
  SFTPGo and LDAP outpost logs; the Ingress itself does not authenticate.
- SFTP host key changed after a restart: the `sftpgo-state-ceph` PVC was lost
  or replaced; clients must accept the new fingerprint.

## Upgrading

- **SFTPGo**: update the image tag in `deployment.yaml` and the plugin if
  required.
- **Auth plugin**: update the version in the init-container URL, the SHA256 in
  the `echo ... | sha256sum -c` line, and
  `SFTPGO_PLUGINS__0__SHA256SUM` together.
- **Authentik**: keep the LDAP outpost image aligned with the other outposts
  (`ghcr.io/goauthentik/ldap:2026.8.5`).
