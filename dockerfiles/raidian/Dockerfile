# syntax=docker/dockerfile:1
#
# RAIDian: a Debian-based RAID management toolbox.
#
# It bundles the vendor CLIs that are awkward to install on an immutable host
# (MegaCLI, PERCCLI and StorCLI) together with the usual Linux disk and
# enclosure tools (mdadm, smartctl, nvme, sg3_utils, lsscsi, ...).
#
# Build and push:
#   docker login ghcr.io
#   ./scripts/raidian/build-image.sh
#
# The image is linux/amd64 only: MegaCLI and PERCCLI are published for x86-64.

FROM debian:bookworm-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGETARCH
ARG DEBIAN_FRONTEND=noninteractive

# MegaCLI 8.07.14, packaged for Debian 12 by the hwraid repository.
ARG MEGACLI_URL=https://hwraid.le-vert.net/debian/pool-bookworm/megacli/megacli_8.07.14-4+Debian.12.bookworm_amd64.deb
ARG MEGACLI_SHA256=152efe1a413428315c6a68eed075a0c5bec4c3411d5f85623531f024dd65feda

# PERCCLI 7.3208.0 for Dell PERC/HBA controllers (Dell download FOLDER13074167M).
ARG PERCCLI_URL=https://dl.dell.com/FOLDER13074167M/1/PERCCLI_7.3208.0_Linux_A01.tar.gz
ARG PERCCLI_SHA256=d4d935a59fbb99614ad890f1a7bf3c6f31252cfb1fc66a574a0a71d3a1c5829f

# StorCLI 007.3703.0000.0000 (SAS3.5 P38) for Broadcom/LSI MegaRAID.
ARG STORCLI_PHASE=38
ARG STORCLI_VERSION=007.3703.0000.0000
ARG STORCLI_URL=https://docs.broadcom.com/docs-and-downloads/host-bus-adapters/host-bus-adapters-common-files/sas_sata_nvme_12g_p38/STORCLI_SAS3.5_P38.zip
ARG STORCLI_SHA256=d0f6065d3ed2993d64eaa1df274f08c624d4dc61bcdd802a1afdb552f0b6b511

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) ;; \
      *) echo "raidian supports linux/amd64 only (got ${TARGETARCH})" >&2; exit 1 ;; \
    esac

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      dmidecode \
      file \
      findutils \
      hdparm \
      ipmitool \
      ledmon \
      less \
      libgcc-s1 \
      libncurses5 \
      libstdc++6 \
      libtinfo5 \
      lsscsi \
      mdadm \
      nvme-cli \
      parted \
      pciutils \
      procps \
      sdparm \
      sg3-utils \
      smartmontools \
      unzip \
      usbutils \
      util-linux \
    && rm -rf /var/lib/apt/lists/*

# Download the vendor packages, verify their pinned checksums, and install them
# in one layer so the archives do not remain in the image.
RUN set -eux; \
    curl -fsSL --retry 3 -A 'raidian-build/1.0' -o /tmp/megacli.deb "${MEGACLI_URL}"; \
    echo "${MEGACLI_SHA256}  /tmp/megacli.deb" | sha256sum -c -; \
    dpkg -i /tmp/megacli.deb; \
    rm -f /tmp/megacli.deb; \
    \
    curl -fsSL --retry 3 -A 'raidian-build/1.0' -o /tmp/perccli.tar.gz "${PERCCLI_URL}"; \
    echo "${PERCCLI_SHA256}  /tmp/perccli.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/perccli; \
    tar -xzf /tmp/perccli.tar.gz -C /tmp/perccli; \
    perccli_deb="$(find /tmp/perccli -type f -name 'perccli_*_all.deb' -print -quit)"; \
    test -n "${perccli_deb}"; \
    dpkg -i "${perccli_deb}"; \
    rm -rf /tmp/perccli /tmp/perccli.tar.gz; \
    \
    curl -fsSL --retry 3 -A 'raidian-build/1.0' -o /tmp/storcli.zip "${STORCLI_URL}"; \
    echo "${STORCLI_SHA256}  /tmp/storcli.zip" | sha256sum -c -; \
    storcli_deb="STORCLI_SAS3.5_P${STORCLI_PHASE}/univ_viva_cli_rel/Unified_storcli_all_os/Ubuntu/storcli_${STORCLI_VERSION}_all.deb"; \
    unzip -p /tmp/storcli.zip "${storcli_deb}" > /tmp/storcli.deb; \
    test -s /tmp/storcli.deb; \
    dpkg -i /tmp/storcli.deb; \
    rm -f /tmp/storcli.zip /tmp/storcli.deb

# The vendor packages do not add PATH symlinks. Keep the historical /opt paths
# and provide the names used by scripts and runbooks.
RUN set -eux; \
    ln -sf /opt/MegaRAID/storcli/storcli64 /usr/local/bin/storcli; \
    ln -sf /opt/MegaRAID/storcli/storcli64 /usr/local/bin/storcli64; \
    ln -sf /opt/MegaRAID/perccli/perccli64 /usr/local/bin/perccli; \
    ln -sf /opt/MegaRAID/perccli/perccli64 /usr/local/bin/perccli64; \
    mkdir -p /opt/MegaRAID/MegaCli; \
    ln -sf /usr/sbin/megacli /usr/local/bin/MegaCli; \
    ln -sf /usr/sbin/megacli /usr/local/bin/MegaCli64; \
    ln -sf /usr/sbin/megacli /opt/MegaRAID/MegaCli/MegaCli; \
    ln -sf /usr/sbin/megacli /opt/MegaRAID/MegaCli/MegaCli64

# Smoke-test the CLIs. The version commands can return non-zero when no
# controller is present, so require version output instead of the exit code.
RUN set -eux; \
    ldd /usr/lib/megacli/megacli.real > /tmp/megacli.ldd 2>&1 || true; \
    if grep -q 'not found' /tmp/megacli.ldd; then \
      cat /tmp/megacli.ldd >&2; \
      echo "MegaCLI has unresolved shared libraries" >&2; \
      exit 1; \
    fi; \
    rm -f /tmp/megacli.ldd; \
    command -v megacli storcli storcli64 perccli perccli64 MegaCli MegaCli64 >/dev/null; \
    for cli in storcli64 perccli64 megacli; do \
      timeout 10 "${cli}" -v > "/tmp/${cli}.version" 2>&1 || true; \
      if [ ! -s "/tmp/${cli}.version" ]; then \
        timeout 10 "${cli}" -h > "/tmp/${cli}.version" 2>&1 || true; \
      fi; \
      test -s "/tmp/${cli}.version"; \
      if grep -qiE 'error while loading|cannot open shared object' "/tmp/${cli}.version"; then \
        cat "/tmp/${cli}.version" >&2; \
        exit 1; \
      fi; \
      rm -f "/tmp/${cli}.version"; \
    done

LABEL org.opencontainers.image.title="raidian" \
      org.opencontainers.image.description="Debian-based RAID management toolbox with MegaCLI, PERCCLI, StorCLI, mdadm, smartctl and nvme-cli" \
      org.opencontainers.image.source="https://github.com/Blalange/glemmen-fleet"

WORKDIR /root
CMD ["/bin/bash"]
