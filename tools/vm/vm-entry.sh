#!/bin/sh
# Entry point for guest containers:
#   keyhold-vm-entry [--relay PUBLIC:LOCAL]... -- qemu-system-aarch64 ...
#
# Each --relay listens on container port PUBLIC and forwards to 127.0.0.1:LOCAL, where QEMU's
# user-mode network forwards on to the guest. QEMU runs that network with restrict=on (the guest
# cannot open connections of its own), and in that mode the guest can only answer connections
# that come from the loopback address. Docker forwards published ports from a bridge address, so
# the relay is what makes the guest's SSH and web ports reachable from the host.
set -eu
while [ "$#" -gt 0 ]; do
  case "$1" in
    --relay)
      public=${2%%:*}
      local_port=${2##*:}
      socat -d0 "TCP-LISTEN:${public},fork,reuseaddr" "TCP:127.0.0.1:${local_port}" &
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done
exec "$@"
