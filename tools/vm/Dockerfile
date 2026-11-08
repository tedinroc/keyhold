# Runner image for Keyhold's test guests: QEMU for 64-bit Arm guests, qemu-img, the tools that
# build cloud-init seeds, and socat for the port relays in vm-entry.sh. Build it with
# `uv run python tools/vm/vm.py runner` (native platform of the docker host). QEMU uses KVM when
# the host passes /dev/kvm in and is itself aarch64; otherwise TCG emulation.
FROM debian:trixie-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      qemu-system-arm \
      qemu-efi-aarch64 \
      qemu-utils \
      genisoimage \
      socat \
      ca-certificates \
      procps \
 && rm -rf /var/lib/apt/lists/*

# Fail the build early if the firmware or binaries moved.
RUN qemu-system-aarch64 --version \
 && qemu-img --version \
 && genisoimage -version \
 && socat -V > /dev/null \
 && test -f /usr/share/AAVMF/AAVMF_CODE.secboot.fd \
 && test -f /usr/share/AAVMF/AAVMF_VARS.fd

COPY vm-entry.sh /usr/local/bin/keyhold-vm-entry
RUN chmod 755 /usr/local/bin/keyhold-vm-entry

LABEL org.opencontainers.image.title="keyhold-qemu-runner" \
      org.opencontainers.image.description="QEMU (aarch64 system), qemu-utils, genisoimage and socat for Keyhold test guests"

WORKDIR /vm
