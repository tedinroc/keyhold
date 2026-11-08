# Test guests

Keyhold's agent works on a real server console: a screen and a keyboard. For development and the
demo, that server is a local virtual machine: Debian 13 (the official "genericcloud" image) under
QEMU, inside a Linux container started with docker. Nothing here touches a remote server.

The guest has the architecture of the runner image, which `vm.py runner` builds for the docker
host: 64-bit Arm guests on an arm64 host, x86-64 (amd64) guests on an x86-64 host. See
"Architectures" below.

## Pieces

| File | What it is |
|---|---|
| `Dockerfile` | Runner image `keyhold-qemu-runner:3`: QEMU and UEFI firmware for the image's architecture (`qemu-system-aarch64` and AAVMF, or `qemu-system-x86_64` and OVMF), `qemu-img`, `genisoimage`, `socat`. |
| `vm-entry.sh` | Container entry point: port relays (see "Networking"), then QEMU. |
| `vm.py` | Command line for everything below. Logic lives in `keyhold/console/` and `keyhold/faults/`. |
| `prove.py` | Runs every fault on a real guest through the PayPal lock (FakePayPal) and checks the money outcome. See "Proof on real guests". |

## Steps

```
uv run python tools/vm/vm.py runner      # build the runner image (pulls debian:trixie-slim + QEMU); --no-cache for a clean build
uv run python tools/vm/vm.py fetch       # download the base image, verify SHA512 (about 340 MB)
uv run python tools/vm/vm.py golden      # one boot with a cloud-init seed -> golden image
uv run python tools/vm/vm.py plant all   # one prepare boot per fault -> fault images
uv run python tools/vm/vm.py up sshd-masked
uv run python tools/vm/vm.py down <run-id>
uv run python tools/vm/vm.py status
```

`runner` and `fetch` need the internet. The rest works offline. Measured on an Apple Silicon Mac
(docker under OrbStack, TCG emulation, no KVM), arm64 guests: a clean `runner --no-cache` build
37 s, `fetch` 2 min 15 s, the golden boot 1 min 55 s, `plant all` 4 min 10 s (29 to 52 s per
fault), and a run reaches its symptom 6 to 43 seconds after the container starts. Details, and
the amd64 times under emulation, in `docs/REAL_VM.md`.

`runner` pulls images with a temporary docker config that has no stored credentials, because a
desktop credential helper can block on a keychain prompt. Pass `--use-my-docker-config` to use your
own.

## Disk chain

Every image is a qcow2 overlay of the one above it, with a relative backing path:

```
var/vm/base/debian-13-genericcloud-<arch>-<build>.qcow2  official image, pinned by build and SHA512
  var/vm/golden/golden.qcow2       demo page, probe user and key, root console password
    var/vm/faults/<fault>.qcow2    the fault planted by a prepare boot
      var/vm/runs/<run>/disk.qcow2 what one rescue boots; deleted afterwards
```

A run overlay takes milliseconds to create, so every run starts from the same broken server.

**Base.** `fetch` checks the pinned SHA512 against the build's published `SHA512SUMS` first, then
downloads to a `.part` file and keeps it only if its SHA512 matches. A dropped connection is
resumed with an HTTP range request from the end of the `.part` file. After six failed connections
in a row that add nothing, `fetch` stops and keeps the `.part`; running it again resumes.

**Golden.** A NoCloud seed (`cidata` ISO) makes cloud-init:
- serve a small page on port 80 (systemd socket `demo-page.socket`, one shell process per request,
  no web server package); its sha256 is saved in `golden/golden.json` for the owner probe;
- create the `probe` user with only the owner probe's public key (`keys/probe_ed25519.pub`);
- set the root console password, read from a file on the seed disk so it never enters
  cloud-init's caches in the guest (`golden/console-password`, mode 600);
- show boot messages on the screen above the login prompt, and copy the journal to the serial log;
- install the fault-planting hook, switch cloud-init off for later boots, and power off.

**Faults.** A prepare boot attaches a second small ISO labelled `KHPLANT` holding `plant.sh`. The
hook runs it as root, removes itself, deletes the journal (no record of the planting is left in
the guest), prints `KEYHOLD-PLANT-OK <fault>` on the serial port and powers off. The fault
catalog is `keyhold/faults/__init__.py`.

## Running a guest

`keyhold.console.qemu.QemuVM` starts one container per guest (`keyhold-run-<id>`):

- the machine, screen and keyboard of the guest's architecture (see "Architectures"), with UEFI
  firmware: the Secure Boot build, which has no UEFI shell, so the firmware offers no way around a
  GRUB password (Secure Boot itself stays off, because the variable store has no keys);
- two QMP sockets: one lent to the console (`QemuConsole`: screendump, send-key, system_reset), one
  kept for control (status, clean shutdown with the ACPI power button);
- the serial port written to `runs/<id>/serial.log` (kernel messages and the journal);
- KVM when the runner image is native to the docker host and QEMU can start KVM in a container
  given `/dev/kvm` (the probe creates a VM on an empty machine and quits); otherwise TCG. If a
  guest still does not start with KVM, it is started again under TCG, and so are later guests.

All published ports bind to the host loopback.

## Architectures

| | arm64 runner | amd64 runner |
|---|---|---|
| QEMU | `qemu-system-aarch64`, machine `virt` | `qemu-system-x86_64`, machine `q35` with SMM (the Secure Boot OVMF build needs it) |
| Firmware | `AAVMF_CODE.secboot.fd`, `AAVMF_VARS.fd` | `OVMF_CODE_4M.secboot.fd`, `OVMF_VARS_4M.fd` |
| Screen | `ramfb`, 800x600 | standard VGA, 800x600 |
| Keyboard | USB keyboard (firmware, GRUB) and virtio keyboard (Linux) | the PC's PS/2 keyboard (firmware, GRUB and Linux) |
| Serial port in the guest | `ttyAMA0` | `ttyS0` |
| Base image | `debian-13-genericcloud-arm64-<build>.qcow2` | `debian-13-genericcloud-amd64-<build>.qcow2` (same build) |
| KVM | on an arm64 host with `/dev/kvm` | on an x86-64 host with `/dev/kvm` (for example a cloud VM with nested virtualization) |

Everything else is the same: networking, the relays, both QMP sockets, the disk chain, the
cloud-init seed (only the serial port's name differs), the fault catalog and the fix scripts.
Under KVM the guest gets 2 vCPUs and `-cpu host`; under TCG 4 vCPUs and `-cpu max`.

One VM home holds the images of one architecture. `golden/golden.json` records it, and planting
a fault or starting a run refuses a runner of the other architecture. To try the other
architecture on one machine (slowly, under emulation), give it its own runner image and VM home:

```
export KEYHOLD_RUNNER_IMAGE=keyhold-qemu-runner:3-amd64 KEYHOLD_VM_HOME=var/vm-amd64
uv run python tools/vm/vm.py runner --platform linux/amd64
uv run python tools/vm/vm.py fetch
uv run python tools/vm/vm.py golden
uv run python tools/vm/vm.py plant sshd-config-typo
uv run python tools/vm/prove.py sshd-config-typo
```

An emulated runner never uses the host's KVM, so its guests run under TCG inside an emulated
QEMU: slow, but the same guest as on a native host.

## Networking

The guest uses QEMU user-mode networking with `restrict=on`: it can answer the forwarded SSH and
web ports but cannot open connections of its own, so an agent at the keyboard cannot reach the
internet from the guest. In that mode the guest only answers connections that come from the
loopback address, while docker forwards published ports from a bridge address. `vm-entry.sh`
therefore runs a `socat` relay per port inside the container: container port 2222 to QEMU's
loopback forward on port 12222 (guest port 22), and 8080 to its loopback port 18080 (guest port 80).

## Secrets

Everything is under `var/vm` (git-ignored) or `$KEYHOLD_VM_HOME`. The probe private key and the
console password are mode 600 and never printed. The agent never sees the probe key.

## Proof on real guests

```
uv run python tools/vm/prove.py                  # every fault, one guest at a time
uv run python tools/vm/prove.py sshd-masked      # one fault
```

For each fault, `prove.py` boots a fresh run overlay, saves a screenshot of the symptom, runs the
owner probe (it must fail), runs the free look, checks that a gated session cannot type before
the hold, places the hold with FakePayPal, and runs `Orchestrator.run`. The fault's
deterministic fix script (`keyhold/faults/fixes.py`) plays the model's part, so every key goes
through the agent loop, the keystroke firewall, the gated console and the hold lease. A solvable
fault must end with two probe passes at least 30 seconds apart and exactly one capture
(`cap-{rescue_id}`); the unsolvable one must end with `give_up` and a void (`void-{rescue_id}`).
Results (screenshots, serial log, ledger, timings) go to `runs/real-vm/<UTC time>/`; the latest
summary is in `docs/REAL_VM.md`.

## Tests

`uv run pytest tests/console` runs the unit tests with fakes (no docker, no network).
`uv run pytest -m slow tests/console/test_real_guest.py` boots real guests (needs the runner image
and the base image; builds the golden and fault images if missing).
