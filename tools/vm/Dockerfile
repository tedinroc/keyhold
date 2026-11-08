# Runner image for Keyhold's test guests: QEMU for guests of the image's own architecture, UEFI
# firmware, qemu-img, the tools that build cloud-init seeds, and socat for the port relays in
# vm-entry.sh. Build it with `uv run python tools/vm/vm.py runner` (native platform of the docker
# host).
#
#   arm64 image: qemu-system-aarch64 and AAVMF, for 64-bit Arm guests
#   amd64 image: qemu-system-x86_64 and OVMF, for x86-64 guests
#
# The guest architecture follows the image's architecture. QEMU uses KVM when the host passes
# /dev/kvm in and the image runs natively on the host; otherwise TCG emulation.
FROM debian:trixie-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN set -eu; \
    arch=$(dpkg --print-architecture); \
    case "$arch" in \
      arm64) qemu="qemu-system-arm qemu-efi-aarch64" ;; \
      amd64) qemu="qemu-system-x86 ovmf" ;; \
      *) echo "no Keyhold guest for architecture $arch" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      $qemu \
      qemu-utils \
      genisoimage \
      socat \
      ca-certificates \
      procps; \
    rm -rf /var/lib/apt/lists/*

# Fail the build early if the firmware or binaries moved. The Secure Boot firmware builds are used
# because they have no built-in UEFI shell (see keyhold/console/qemu.py).
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      arm64) \
        qemu-system-aarch64 --version; \
        test -f /usr/share/AAVMF/AAVMF_CODE.secboot.fd; \
        test -f /usr/share/AAVMF/AAVMF_VARS.fd ;; \
      amd64) \
        qemu-system-x86_64 --version; \
        test -f /usr/share/OVMF/OVMF_CODE_4M.secboot.fd; \
        test -f /usr/share/OVMF/OVMF_VARS_4M.fd ;; \
    esac; \
    qemu-img --version; \
    genisoimage -version; \
    socat -V > /dev/null

COPY vm-entry.sh /usr/local/bin/keyhold-vm-entry
RUN chmod 755 /usr/local/bin/keyhold-vm-entry

LABEL org.opencontainers.image.title="keyhold-qemu-runner" \
      org.opencontainers.image.description="QEMU (system emulator for the image's architecture), UEFI firmware, qemu-utils, genisoimage and socat for Keyhold test guests"

WORKDIR /vm
