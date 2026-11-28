# Browsertrix

Self-hosted [Browsertrix](https://browsertrix.com/) web archiving, served at
<https://browsertrix.glem.men>. The upstream Helm chart (pinned to v1.27.0)
runs the frontend, backend/operator, MongoDB, emails service and the
metacontroller-based crawler operator. Crawl pods run in the separate
`browsertrix-crawlers` namespace created by the chart.

## Storage and networking

- **MongoDB** is a single chart-managed replica on `nvme-rbd` (NVMe block
  storage, RF2). It is the only latency-sensitive PVC that needs to survive a
  crawl; release.yaml moves it off the chart-wide storage class with a
  post-render patch.
- **Crawl workspaces, per-crawl Redis and dedupe indexes** use
  `fast-fs-crawl`, a CMR CephFS class with `reclaimPolicy: Delete` defined in
  `flux/infrastructure/rook-ceph/resources/storageclasses.yaml`. CephFS is
  thin and grows online, and Delete reclaim removes the subvolume when the
  operator deletes a per-crawl PVC. The chart only has one dynamic PVC class,
  so this also covers Redis/dedupe; that data is either in memory or backed up
  to S3.
- **Archived WACZ files** live in the `btrix` bucket in the fast Ceph RGW.
  The backend uses the in-cluster RGW service; replay/download presigned URLs
  use <https://s3.glem.men> with path-style addressing.
- The crawler network policy is on. Crawlers can reach the public web (80/443)
  but not private RFC1918 ranges, except the Browsertrix services they need.
  The application namespace egress policy is also on.

### Replay CORS

Replay fetches WACZ files directly from `s3.glem.men` in the browser, so the
`btrix` bucket must send CORS headers for `https://browsertrix.glem.men` (a
cross-origin `GET`/`HEAD`). This is RGW bucket state, not a Kubernetes
resource; reapply it if the bucket is recreated:

```sh
AWS_ACCESS_KEY_ID="$(kubectl -n rook-ceph get secret rook-ceph-object-user-fast-btrix \
  -o jsonpath='{.data.AccessKey}' | base64 -d)"
AWS_SECRET_ACCESS_KEY="$(kubectl -n rook-ceph get secret rook-ceph-object-user-fast-btrix \
  -o jsonpath='{.data.SecretKey}' | base64 -d)"
AWS_DEFAULT_REGION=us-east-1 aws s3api put-bucket-cors \
  --bucket btrix \
  --cors-configuration file://flux/apps/browsertrix/bucket-cors.json \
  --endpoint-url https://s3.glem.men
```

Without it, the replay page loads but the WACZ request fails in the browser
because RGW sends no `Access-Control-Allow-Origin` header.

## Secret

The HelmRelease reads `browsertrix-secrets` (key `values.yaml`). Create it
before the first reconcile. It contains the superuser/Mongo passwords, the
backend cookie secret and the `btrix` S3 keys; nothing sensitive is in Git:

```sh
kubectl create namespace browsertrix --dry-run=client -o yaml | kubectl apply -f -

ACCESS_KEY="$(kubectl -n rook-ceph get secret rook-ceph-object-user-fast-btrix \
  -o jsonpath='{.data.AccessKey}' | base64 -d)"
SECRET_KEY="$(kubectl -n rook-ceph get secret rook-ceph-object-user-fast-btrix \
  -o jsonpath='{.data.SecretKey}' | base64 -d)"

cat > /tmp/browsertrix-secret.yaml <<EOF
superuser:
  password: "$(openssl rand -hex 24)"
mongo_auth:
  username: root
  password: "$(openssl rand -hex 24)"
backend_password_secret: "$(openssl rand -hex 32)"
storages:
  - name: "default"
    type: "s3"
    access_key: "${ACCESS_KEY}"
    secret_key: "${SECRET_KEY}"
    bucket_name: "btrix"
    # Keep the endpoint and access endpoint on the same public host. The chart
    # appends bucket_name to endpoint_url, so the stored endpoint is
    # https://s3.glem.men/btrix/; access_endpoint_url must match it. If they
    # differ, Browsertrix signs with SigV4 for the internal host and then
    # rewrites the host, which invalidates the signature.
    endpoint_url: "https://s3.glem.men/"
    access_endpoint_url: "https://s3.glem.men/btrix/"
    access_addressing_style: path
    region: "us-east-1"
    is_default_primary: true
EOF

kubectl -n browsertrix create secret generic browsertrix-secrets \
  --from-file=values.yaml=/tmp/browsertrix-secret.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
rm /tmp/browsertrix-secret.yaml
```

Retrieve the generated superuser password with:

```sh
kubectl -n browsertrix get secret browsertrix-secrets \
  -o jsonpath='{.data.values\.yaml}' | base64 -d | yq '.superuser.password'
```

The superuser email is `hexahigh0@gmail.com`, set in values.yaml. SMTP is not
configured, so invitation and password-reset links are only written to the
backend logs (redacted unless `btrix_env: development`).

## Importing the old organization

The organization files must be in the new bucket before the JSON import so the
presigned URLs resolve. The old SeaweedFS bucket is mounted at
`/home/simon/mount/buckets/btrix`; copy it with rclone using the btrix keys
(the endpoints are the same as in the Secret):

```sh
rclone copy /home/simon/mount/buckets/btrix/ :s3:btrix/ \
  --s3-endpoint https://s3.glem.men --s3-provider Ceph \
  --s3-access-key-id "$ACCESS_KEY" --s3-secret-access-key "$SECRET_KEY" \
  --s3-region us-east-1 --s3-force-path-style --transfers 4
```

Then import the database export with the superuser JWT. `org-export.json` is
deliberately not committed:

```sh
TOKEN="$(curl -sS -X POST \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=hexahigh0@gmail.com" \
  --data-urlencode "password=$(kubectl -n browsertrix get secret browsertrix-secrets \
    -o jsonpath='{.data.values\.yaml}' | base64 -d | yq '.superuser.password')" \
  https://browsertrix.glem.men/api/auth/jwt/login | jq -r .access_token)"

curl -sS -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @org-export.json \
  'https://browsertrix.glem.men/api/orgs/import/json?ignoreVersion=true&storageName=default'
```

The export is from DB version 0058 and the chart runs 0060, so the import uses
`ignoreVersion=true`; run the 0058-0060 migrations afterwards by setting
`rerun_from_migration: "0058"` in values.yaml, reconciling, checking the
backend logs, then removing it again. The export has no `userDetails`, so add
the superuser to the imported org's `users` map as owner (role 40) once. The
imported crawl metadata can reference files that are absent from the old
bucket; those replays will 404, but the rest of the org imports normally.

## Checks

```sh
kubectl -n browsertrix get pods
kubectl -n browsertrix get pvc
kubectl -n browsertrix get crawljobs
kubectl -n browsertrix-crawlers get pods,pvc
kubectl -n browsertrix logs deploy/browsertrix-cloud-backend --tail=100
```
