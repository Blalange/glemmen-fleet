# NetBird

This app runs two NetBird components in the `netbird` namespace:

- **Reverse proxy** for private services under `*.proxy.glem.men`.
- **External relay** at `nb-relay.glem.men`.

## Required secrets

Neither secret is stored in Git.

```sh
kubectl -n netbird create secret generic netbird-proxy-token \
  --from-literal=token='nbx_...'

kubectl -n netbird create secret generic netbird-relay-auth \
  --from-literal=auth-secret='...'
```

`netbird-relay-auth` must use the same value as `relays.secret` on the main
NetBird management server.

## Relay

Traefik terminates TLS for `nb-relay.glem.men` with the existing
`*.glem.men` wildcard certificate and forwards WebSocket upgrades to the relay
on port 8080. The relay advertises:

```text
rels://nb-relay.glem.men:443
```

The relay is running and the WebSocket handshake through Traefik returns
`101 Switching Protocols`.

STUN is exposed on UDP 3478 through a second MetalLB service that shares
`192.168.6.99` with Traefik. UniFi forwards UDP 3478 to that VIP. A STUN
binding request sent to `192.168.6.99:3478` returns a valid
`XOR-MAPPED-ADDRESS`, and no extra TCP port is exposed.

Traefik sets `X-Real-Ip` but not `X-Real-Port`, so the relay logs the Traefik
pod address for connections rather than the original client port. Relaying
still works; a Traefik plugin would be needed for per-peer client IP logging.

### Main server configuration

The main NetBird server has to be told about the external relay. Add this to
its configuration and restart it:

```yaml
server:
  stuns:
    - uri: "stun:nb-relay.glem.men:3478"
      proto: "udp"
  relays:
    addresses:
      - "rels://nb-relay.glem.men:443"
    secret: "<same value as netbird-relay-auth/auth-secret>"
    credentialsTTL: "24h"
```

Without this, the relay runs but no peers are told to use it. The relay logs
currently show authenticated peer connections, so the main server appears to
be advertising it already.

## Reverse proxy

Traefik passes `*.proxy.glem.men` TLS through to the reverse proxy. The proxy
has connected to `https://netbird.blalange.org` and completed its initial
mapping sync. A plain HTTP GET to `/management.ProxyService/` returns a
`404 text/plain` because that path is gRPC-only; that is not a proxy failure.

No service is registered yet, so unknown hosts return the proxy's own 404
page. Create the service in the NetBird dashboard with a target and a domain
under `proxy.glem.men` to verify end-to-end. If publishing fails, check the
proxy token's account scope: the access log currently reports
`account-scoped token cannot access account` for unmatched requests.
