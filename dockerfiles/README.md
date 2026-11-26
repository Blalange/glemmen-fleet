# Custom Dockerfiles

This directory holds Dockerfiles for images that the cluster builds itself
instead of pulling from an upstream registry.

| Path | Upstream source | Build context | Builder |
|---|---|---|---|
| `artex/Dockerfile` | [BaaRai/ARTEX](https://github.com/BaaRai/ARTEX) tag `v0.6.0` | an ARTEX checkout at the same tag | `scripts/artex/build-image.sh` |
| `raidian/Dockerfile` | Debian 12 + pinned Broadcom/Dell/hwraid packages | `dockerfiles/raidian` | `scripts/raidian/build-image.sh` |

Rules:

- Keep the Dockerfile identical to the upstream file for the pinned tag. If a
  local change is unavoidable, document it here and in the image README.
- The build context is documented per image. Most of these Dockerfiles expect
  a source checkout, not this repository root.
- Do not put secrets, registry credentials or generated artifacts here.
- Record the upstream tag/commit and the builder so the image can be rebuilt.

## ARTEX

`artex/Dockerfile` is the upstream ARTEX Dockerfile copied verbatim from
`v0.6.0`. It builds the Go binary with the embedded Next.js UI and installs
the scanner toolchain (nmap, nuclei, httpx, bbot, Playwright and Chromium).
`scripts/artex/build-image.sh` clones the matching ARTEX tag and passes that
checkout as the build context, with this file selected via `--file`.

When upgrading ARTEX:

1. Replace `artex/Dockerfile` with the Dockerfile from the new upstream tag.
2. Update `ARTEX_VERSION` in `scripts/artex/build-image.sh` and the image tag
   in `flux/apps/artex/artex.yaml`.
3. Rebuild and push, then update the Flux revision.

## RAIDian

`raidian/Dockerfile` builds a Debian 12 RAID-management toolbox with MegaCLI,
PERCCLI, StorCLI, `mdadm`, `smartctl`, `nvme-cli` and related disk utilities.
It is published to GHCR as `ghcr.io/blalange/raidian`.
`scripts/raidian/build-image.sh` builds `linux/amd64` from this directory and
pushes the image.

The vendor packages are downloaded during the build and checked against pinned
SHA-256 sums in the Dockerfile. When upgrading a tool, update the URL, version
and checksum together. See [`raidian/README.md`](raidian/README.md) for the tool
versions, usage examples and the rebuild procedure.
