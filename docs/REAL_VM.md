# Keyhold on real guests

Date: 2026-11-07. Everything below ran on one Apple Silicon Mac, in local containers. No remote
server, cloud account or real PayPal account was touched.

**Result.** In one run (`runs/real-vm/20261107T180339Z/`), all six faults in the catalog passed
on real Debian 13 guests. Each of the five solvable faults was fixed by typing through a
`GatedConsole` under a FakePayPal hold, passed the owner probe twice at least 30 seconds apart,
and ended with exactly one capture of the quoted amount (request id `cap-{rescue_id}`). For the
GRUB password fault the agent called `give_up`, and the hold was voided (`void-{rescue_id}`) with
no capture. The whole run took 10 minutes 30 seconds, with one guest at a time.

What this proves and what it does not:

| Real in this run | Stand-in in this run |
|---|---|
| Debian 13 arm64 guests under QEMU, booted from the official image | PayPal: `FakePayPal` (same interface as the sandbox client) |
| The console: QMP screendump to PNG, send-key typing, system_reset | Claude: a scripted model that replays a fixed list of tool calls |
| The agent loop, the keystroke firewall, `GatedConsole`, `HoldLease`, `Orchestrator.run` | |
| The owner probe: the system `ssh` client with the hidden probe key, and an HTTP GET with a sha256 check | |
| The ledger (SQLite), the outbox and its request ids | |

## Environment

- Host: Apple Silicon Mac. Docker through OrbStack; its Linux VM has 18 CPUs and 16 GB.
- Runner image `keyhold-qemu-runner:2` (`tools/vm/Dockerfile`): Debian trixie-slim with
  `qemu-system-aarch64`, UEFI firmware, `qemu-img`, `genisoimage` and `socat`. (The image is now
  `keyhold-qemu-runner:3`, which installs the same packages on arm64 and the x86 ones on amd64;
  see "x86-64 guests" below.)
- Acceleration: **TCG emulation, no KVM.** The OrbStack VM has no `/dev/kvm` on this machine:
  `docker run --device /dev/kvm ...` fails with "error gathering device information while adding
  custom device "/dev/kvm": no such file or directory". So QEMU emulates the CPU. All times below
  are under TCG; times on a KVM host were not measured here.
- Guest: Debian 13 arm64 "genericcloud" image, build 20261103-2651 (337,641,472 bytes), 4 vCPUs,
  1 GB RAM, UEFI, an 800x600 `ramfb` screen, a USB keyboard (firmware and GRUB) and a virtio
  keyboard (Linux). User-mode networking with `restrict=on`, so the guest cannot open
  connections of its own. SSH and the web port are forwarded to ports on the host loopback.

## Building

| Step | Command | Time | Notes |
|---|---|---|---|
| Runner image, from cache | `vm.py runner` | 1.6 s | |
| Runner image, clean | `vm.py runner --no-cache` | 37 s | pulls the base image and the QEMU packages again |
| Base image | `vm.py fetch` into an empty VM home | 2 min 15 s | download, then SHA512 check against the pinned value |
| Seed | `vm.py seed` | 0.6 s | `cidata` ISO: user-data, meta-data, network-config, console password |
| Golden image | `vm.py golden --force` | 1 min 55 s | one boot with the seed, then power-off |
| Fault images | `vm.py plant all` | 4 min 10 s | 29, 45, 52, 40, 41 and 44 s per fault |

The pinned SHA512 was also checked against the build's published `SHA512SUMS`: the published
entry for the image equals the pinned value, and both the fresh download and the copy already in
`var/vm/base` match it.

## What `tools/vm/prove.py` does for each fault

1. Creates a run overlay of the fault image (about 0.2 s) and boots it in its own container.
2. Waits for the fault's marker on the serial log, then saves a screenshot of the symptom.
3. Runs the owner probe: an SSH login as the probe user with the hidden key, and a GET of the
   demo page with its sha256. It must fail.
4. Runs the free look on the real screen (the scripted model answers with the fault's class) and
   stores the quote in a new ledger.
5. Opens a `GatedConsole` on a new `HoldLease` before any hold exists and tries `type_text`,
   `press` and `power_cycle`. All three must raise `HoldRevoked` with no input reaching QEMU (a
   counting wrapper sits between the gate and the QEMU console).
6. Places the hold: FakePayPal order (intent AUTHORIZE), owner approval, authorization. The
   ledger must show HELD.
7. Calls `Orchestrator.run`. The fault's fix script (`keyhold/faults/fixes.py`) plays the
   model's part, so each key goes through the agent loop, the keystroke firewall, the gated
   console and the lease, and the lease checks the authorization with FakePayPal. Before each
   step the script waits for the previous step's message on the serial log, so it keeps pace
   with the emulated guest.
8. Solvable fault: `finished`, then the owner probe until two passes at least 30 s apart, then
   the capture. The script then replays the capture through `Payments` and checks that
   FakePayPal still has one capture. Unsolvable fault: `give_up`, then the void.
9. Opens a new gated session after the rescue ended. Input must be refused again.
10. Saves a final screenshot, probes once more, shuts the guest down (ACPI power button) and
    removes the container and the run overlay.
11. Scans every saved file for the console password. It must appear in none of them.

Each solvable fault has 18 checks and the GRUB fault has 16. They are listed in each fault's
`result.json`. Every check passed in this run.

## Results (run 20261107T180339Z)

| Fault | Class, tier | On screen at boot | Probe before | Boot to symptom | Keyboard session | `finished` to capture | Outcome |
|---|---|---|---|---|---|---|---|
| sshd-config-typo | sshd_config_error, T1 | `[FAILED] Failed to start ssh.service` | SSH fails, page OK | 24 s | 24 s, 4 inputs | 41 s | CAPTURED $9.00 |
| sshd-masked | ssh_service_disabled, T1 | a normal login prompt | SSH fails, page OK | 22 s | 22 s, 3 inputs | 41 s | CAPTURED $9.00 |
| sshd-port-page-down | sshd_config_error, T1 | a normal login prompt | both fail | 24 s | 21 s, 3 inputs | 41 s | CAPTURED $9.00 |
| network-static-wrong | network_config_error, T1 | a normal login prompt | both time out | 24 s | 26 s, 3 inputs | 41 s | CAPTURED $9.00 |
| fstab-missing-disk | fstab_bad_entry, T2 | emergency mode, root password prompt | both fail | 31 s | 47 s, 3 inputs (includes a reboot) | 41 s | CAPTURED $19.00 |
| grub-password | console_locked, no tier | `Enter username:` | both time out | 7 s | 27 s, 2 inputs (one is a reset) | void at once | VOIDED, nothing captured |

"Keyboard session" runs from the moment the lease granted input to the agent's `finished` or
`give_up`. "`finished` to capture" is the probe phase: probes every 10 s, and the capture comes
with the first pass at least 30 s after the first one (the passes were 38.5 s apart each time).
Each fault took 80 to 133 seconds from overlay to cleanup.

PayPal calls per rescue, as FakePayPal recorded them: `order-{id}`, `auth-{id}`, then
`cap-{id}` (solvable faults) or `void-{id}` (GRUB). The capture replay with `cap-{id}` returned
the stored capture and made no new call. `Ledger.check_invariants` found no problem for any
rescue. The lease asked FakePayPal for the authorization twice per session (at the grant and
once more during the session).

For the GRUB fault, the free look answered `console_locked`, which has no price, so no hold was
offered. That is the intended behaviour. To also prove that `give_up` after a hold voids it,
`prove.py` then opened a T2 quote by hand (as if the look had read the prompt as a GRUB
configuration error), placed the hold, and let the script try: it typed a user name (GRUB then
asked for a password), reset the machine, saw the same prompt again, and gave up.

### The fix scripts

The commands typed at the console (the password is typed only at a password prompt and is stored
as `[console password]` in the ledger):

| Fault | Typed after logging in as root |
|---|---|
| sshd-config-typo | `sshd -t`, then `sed -i '/^PermitRootLogn no$/d' /etc/ssh/sshd_config && systemctl reset-failed ssh.service && systemctl restart ssh.service` |
| sshd-masked | `systemctl unmask ssh.service ssh.socket && systemctl enable --now ssh.service` |
| sshd-port-page-down | `rm /etc/ssh/sshd_config.d/10-port.conf && systemctl restart ssh && systemctl enable --now demo-page.socket` |
| network-static-wrong | `sed -i -e 's/dhcp4: false/dhcp4: true/' -e '/addresses:/d' -e '/routes:/,$d' /etc/netplan/50-cloud-init.yaml && netplan apply` |
| fstab-missing-disk | the root password at the emergency prompt, then `mount -o remount,rw / && sed -i 's\|^UUID=7d1e3c55\|#&\|' /etc/fstab && systemctl daemon-reload`, then `systemctl reboot` |
| grub-password | `root` at the GRUB user name prompt, a reset, then `give_up` |

The keystroke firewall allowed every one of them (no refusals in the run). A unit test
(`tests/console/test_fault_fixes.py`) feeds each script through the firewall in order, so a
change that the firewall would refuse fails before any VM boots.

## Findings

1. **A fix that works by hand can fail as a script.** In the first full run
   (`runs/real-vm/20261107T174851Z/`), the sshd-config-typo script typed
   `sed ... && sshd -t && systemctl restart ssh`. After the edit, `sshd -t` failed with "Missing
   privilege separation directory: /run/sshd", because ssh.service had never started, so the
   `&&` chain stopped before the restart. systemd had also given up restarting ssh at boot
   ("Start request repeated too quickly"). The script now drops `sshd -t` from the chain and runs
   `systemctl reset-failed` before the restart, and passed twice since. The money side handled
   the failed attempt correctly: the scripted agent stopped with an error, the hold was voided
   with `void-{id}`, and nothing was captured. The other five faults passed in that run.
2. **Three of the six faults look like a healthy boot.** sshd-masked, sshd-port-page-down and
   network-static-wrong show a normal boot log and a login prompt (see their `symptom.png`). A
   free look from one screenshot cannot name these faults; it needs the owner's description or
   the probe result. In this run the scripted model named the class directly. The three faults on
   the public `/try` page (sshd-config-typo, fstab-missing-disk, grub-password) are the three
   whose fault is visible on the screen.
3. **A GRUB password prompt ignores the ACPI power button.** A guest waiting at `Enter
   username:` does not power off cleanly, so `prove.py` and the web app's VM world remove that
   container directly (the fault's `acpi_power_off` is False in the catalog). A reset
   (`system_reset`) brings back the same prompt, as the honest-stop case expects.
4. **The probe's message for a closed SSH port is vague.** When the guest's port 22 is closed,
   the relay accepts the connection and then closes it, and `ssh` (at `LogLevel=ERROR`) prints
   nothing, so the probe reports "ssh failed with no message". The probe still fails as it
   should; only the wording on the status page could be clearer.
5. **The seed ISO was world-readable.** The cloud-init seed holds the console password, but the
   ISO was written with mode 644. `build_iso(..., private=True)` now makes it mode 600; the
   golden build already deleted it after use.
6. **Timeouts add up for an unreachable guest.** For network-static-wrong and grub-password the
   probe before the fix takes 20 seconds (10 s SSH connect timeout, then 10 s HTTP timeout).

## The slow test suite

`uv run pytest -m slow tests/console/test_real_guest.py` also passed on the same images (3 tests,
2 min 24 s; `runs/real-vm/slow-pytest.log`). It boots a healthy guest and checks the screenshot,
types a command whose output shows up on the serial log, power-cycles back to the login prompt
and shuts down cleanly; fixes the masked sshd from the keyboard; and checks that a reset of the
GRUB-locked guest lands on the same prompt.

## x86-64 guests (for a KVM host)

The hosted demo is meant to run on a Linux x86-64 VM with nested virtualization, where KVM can run
x86-64 guests. The guest's architecture follows the runner image's, so on such a host the runner
is amd64 and so are the guests: `qemu-system-x86_64`, machine `q35` with SMM, the Secure Boot
OVMF build with the plain variable store, standard VGA at 800x600, the PC's PS/2 keyboard, and
the serial port `ttyS0`. The base image is `debian-13-genericcloud-amd64-20261103-2651.qcow2`
(341,180,416 bytes), from the same Debian build as the arm64 one; its pinned SHA512 is the one in
that build's published `SHA512SUMS`. KVM is used when the runner image is native to the docker
host and the runner's QEMU, in a container given `/dev/kvm`, can start KVM (the probe creates a VM
on an empty machine and quits); otherwise TCG. A guest that still fails to start with KVM is
started again under TCG, and later guests skip KVM.

### What was verified, and where

All of this ran on the same Apple Silicon Mac, with an amd64 runner image that docker runs under
x86-64 emulation (`vm.py runner --platform linux/amd64`, tagged `keyhold-qemu-runner:3-amd64`,
with its own VM home `var/vm-amd64`). So QEMU itself was emulated and the guests ran under TCG
inside it. Nothing here ran on an x86-64 host or under KVM.

| Step | Time | Notes |
|---|---|---|
| amd64 runner image, first build | 1 min 45 s | under emulation; the per-arch self-checks passed (`qemu-system-x86_64` 10.0.13, the OVMF files) |
| arm64 runner image `:3` | 1 min 7 s | native |
| Base image | see note | `vm.py fetch` failed four times on this network: the server closed the connection at 17 to 41 %, and each partial file was rejected by the SHA512 check (`runs/real-vm/port-20261108/fetch-amd64*.log`). The image was then downloaded with a resuming client, checked against `SHA512SUMS`, and `vm.py fetch` verified the copy in the VM home. `vm.py fetch` now resumes by itself (see below). |
| Golden image | 4 min 6 s | arm64 under native TCG: 1 min 55 s |
| Fault images | 157, 155 and 149 s | sshd-config-typo, fstab-missing-disk, grub-password |

`tools/vm/prove.py` on the three `/try` faults (run `amd64-20261108T045906Z`, 8 minutes in all):

| Fault | Boot to symptom | Keyboard session | `finished` to capture | Checks | Outcome |
|---|---|---|---|---|---|
| sshd-config-typo | 54 s | 24 s, 4 inputs | 41 s | 18 of 18 | CAPTURED $9.00 |
| fstab-missing-disk | 47 s | 149 s, 3 inputs (includes a reboot) | 51 s | 18 of 18 | CAPTURED $19.00 |
| grub-password | 6 s | 27 s, 2 inputs (one is a reset) | void at once | 16 of 16 | VOIDED, nothing captured |

The columns mean the same as in the arm64 table above. Each fault took 78 to 271 seconds from
overlay to cleanup. The same three faults on arm64 guests under native TCG took 80 to 133 seconds
in run 20261107T180339Z and 82 to 165 seconds in the re-run `arm64-20261108T050736Z` below.

The screenshots in that run show what the agent needs on the VGA screen: the firmware and GRUB's
`Enter username:` prompt, the Linux console with `[FAILED] Failed to start ssh.service` above the
login prompt, and the emergency-mode root password prompt (drawn within the fault's 10 s settle
time). Text typed with the PS/2 keyboard reached GRUB, the login prompt, the emergency shell and
the root shell. The serial markers (`login:`, `Reached target emergency.target`,
`Enter username`, the journal lines the fix scripts wait for) all came through `ttyS0`. The fault
scripts and fix scripts needed no change for amd64; only the serial port's name in the golden
seed differs.

The slow test suite (`uv run pytest -m slow tests/console/test_real_guest.py`) passed on the
amd64 images in one run of all three tests (507 s: the healthy-guest test 350 s, the
masked-sshd fix 131 s, the GRUB reset 24 s; `runs/real-vm/slow-pytest-amd64-20261108T064205Z.log`).
The healthy-guest test takes a screenshot, types a command into the root shell whose output
reaches `ttyS0`, power-cycles back to the login prompt and powers off cleanly with ACPI.

Before that, during the port, the suite ran in pieces, and its logins failed twice. The test typed
the password one second after the user name, before the emulated guest showed its prompt (PAM
logged an authentication failure). Then, in one slow boot (2 min 14 s to the login prompt,
against 44 s in other boots), the masked-sshd test's login timed out, apparently because the user
name typed 3 s after the serial prompt never reached the screen's login; the healthy-guest test
passed in that run. The test's login now waits 4 seconds for the password prompt, as the fix
scripts do, checks the PAM session line on the serial log, and retries. The masked-sshd test then
passed alone, and the run above, made with the committed test code, passed all three. Of the runs
during the port, logs of the last two are in `runs/real-vm/port-20261108/` (`slow-amd64-2.log`,
`slow-amd64-3.log`; they do not record which version of the login they ran); the earlier ones,
among them the first GRUB reset pass, were not saved. The fix scripts in `prove.py` already
waited and were not affected.

The same code was run again on the existing arm64 images with the new `keyhold-qemu-runner:3`:
the same three faults passed `prove.py` (run `arm64-20261108T050736Z`; 110, 165 and 82 s), and
the slow test suite passed (3 tests, 3 min 18 s; `runs/real-vm/port-20261108/slow-arm64.log`).
After the changes below it passed again (3 tests, 2 min 58 s;
`runs/real-vm/slow-pytest-arm64-20261108T063906Z.log`). The arm64 seed is byte-for-byte the same
as before, so arm64 images built earlier stay valid.

### KVM probe, TCG fallback and resumed downloads

Checked on the same Mac (no KVM), with the logs in `runs/real-vm/`:

- **KVM probe** (`kvm-probe-and-fallback.log`). `kvm_available()` now has the runner's QEMU start
  `-accel kvm` on an empty machine and quit, because `test -r`/`-w /dev/kvm` always pass for the
  container's root with `DAC_OVERRIDE`. In both runner images the probe's command fails without
  `/dev/kvm` ("failed to initialize kvm"), and the same command with `-accel tcg` succeeds, so
  QEMU's own exit status is what it reports. On this host `kvm_available()` answers no for both.
  The probe answering yes was not seen: that needs a host with KVM.
- **TCG fallback** (same log). With the probe forced to yes, a healthy arm64 guest still started,
  under TCG, both when docker refused `--device /dev/kvm` and when QEMU got `-accel kvm` without
  the device and exited; `kvm_fallback` held the error and the runner then answered no.
- **`vm.py fetch` resumes** (`fetch-resume-amd64.log`). With a `.part` holding the first
  300,000,000 bytes of the verified amd64 image, `fetch_base_image` asked `cloud.debian.org` for
  the rest, got HTTP 206 for bytes 300000000 to 341180415, and the result matched the pinned
  SHA512. A connection cut in the middle was tested only with a fake server.

### The firmware has no UEFI shell

The GRUB-password fault is honest only if the firmware offers no way around the password. With a
UEFI shell, an agent could edit the GRUB files on the EFI partition. Checked for Debian 13's OVMF
(package `ovmf` 2025.02-8+deb13u1):

- **Contents.** After decompressing every LZMA section of the firmware images, the plain build
  (`OVMF_CODE_4M.fd`) holds the UEFI Shell application (file GUID `7C04A583-...`) and its
  strings; the Secure Boot builds (`OVMF_CODE_4M.secboot.fd` and `.secboot.strictnx.fd`) hold no
  shell application, only the boot manager's reference to its GUID. AAVMF shows the same split.
- **Behaviour.** Booted with no disk, the plain build starts "EFI Internal Shell" and stops at
  `Shell>`. The Secure Boot build prints "No bootable option or device was found" and opens the
  setup menu, whose Boot Manager lists no shell.
- **SMM.** The Secure Boot build needs SMM: on `q35` without `smm=on` it printed nothing on the
  screen or the serial port in 75 seconds. Hence `q35,smm=on` and the secure flash in `qemu_argv`.

What the firmware still offers: after a reset, Escape opens its setup menu from the console
(seen on an amd64 guest held at the GRUB prompt; AAVMF's Secure Boot build carries the same
setup application). Its Boot Manager lists the disk and network boot entries (PXE and HTTP; not
tried, and with `restrict=on` the guest cannot open connections), and its Boot Maintenance
Manager has "Boot From File" and a file explorer for the EFI partition.
That partition holds shim, GRUB, MokManager and the shim fallback (`EFI/BOOT` and `EFI/debian`),
and no shell. Whether these setup screens give any way around the GRUB password was not
evaluated.

### Still to verify on the x86-64 KVM host

- KVM itself: `/dev/kvm` passed into the container, QEMU as the container's root with only
  `DAC_OVERRIDE`, `-accel kvm -cpu host` on `q35,smm=on` under nested virtualization, the
  `kvm_available()` probe answering yes there, and no TCG fallback in `prove.py`'s timeline.
- The amd64 runner image built natively, and `vm.py fetch` over that host's network.
- Timing under KVM: the fix scripts wait for serial markers, but a few steps use fixed pauses and
  the fstab fault a 10 s screen settle time; they were measured only under emulation, where the
  guest is slower.
- The other catalog faults on amd64 through `prove.py` (sshd-masked was fixed from the keyboard
  only by the slow test; sshd-port-page-down and network-static-wrong were not planted), and the
  web app's VM world (`/try`) with amd64 guests.

## On an x86-64 host with KVM (run 20261108T062651Z)

A cloud VM with nested virtualization (2 vCPU, 8 GB, Debian 13) built the amd64 images and ran
`tools/vm/prove.py` over the whole catalog. Building the runner image, fetching and verifying the
base image, the golden boot and planting all six faults took 173 s together (about 15 min under
emulation on the Mac). Every guest ran under KVM; the scripted agent and FakePayPal stand in as
in the other runs.

| Fault | Checks | End | Symptom on serial | Whole run | Accel |
|---|---|---|---|---|---|
| sshd-config-typo | pass (0 failed checks) | CAPTURED | 12.8 s | 88 s | kvm |
| sshd-masked | pass (0 failed checks) | CAPTURED | 11.4 s | 78 s | kvm |
| sshd-port-page-down | pass (0 failed checks) | CAPTURED | 12.4 s | 82 s | kvm |
| network-static-wrong | pass (0 failed checks) | CAPTURED | 12.4 s | 106 s | kvm |
| fstab-missing-disk | pass (0 failed checks) | CAPTURED | 23.6 s | 111 s | kvm |
| grub-password | pass (0 failed checks) | VOIDED | 4.2 s | 77 s | kvm |

A later run of `sshd-config-typo` with the KVM probe that starts QEMU (rather than only checking
that `/dev/kvm` exists) also chose KVM and passed in 86 s. The same host serves the hosted `/try`
with two test servers at once.

## Not covered on real guests

- Revoking the hold in the middle of a session (owner Stop, or PayPal voiding the authorization)
  and the stop taking effect within one action. This is covered by the unit, property and E2E
  tests with fakes, not on a real guest.
- The real PayPal sandbox and the real model. This run used FakePayPal and a scripted model. Later
  runs with both, on the sshd fault, are in [evidence/](evidence/README.md).
- The warranty. `prove.py` turns it off (`RescueConfig(warranty=False)`). The live sandbox rescues
  ran the judge-mode warranty on a test VM (24 checks each, all passed; see [evidence/](evidence/README.md)).
  A failing warranty, which refunds, has not run on a real guest.
- KVM speed (no KVM on this machine, see above), and x86-64 guests under KVM (see "Still to
  verify on the x86-64 KVM host").

## Files

Under `runs/real-vm/` (git-ignored):

- `<UTC time>/summary.json`: pass or fail, time and timeline for each fault.
- `<UTC time>/<fault>/result.json`: every check, the probe results, the hold and capture ids,
  the outbox rows, the PayPal calls, the agent's events and the pacing waits.
- `<UTC time>/<fault>/symptom.png`, `after.png`, and `shots/NN-turnTT-*.png` (every screenshot
  the free look and the agent took).
- `<UTC time>/<fault>/serial.log`, `ledger.sqlite3`, `fakepaypal.json`.
- `runner-build*.log`, `fetch-fresh.log`, `seed.log`, `golden-and-faults-build.log`: build
  timings.
- `slow-pytest*.log`: the slow test suite (`slow-pytest.log` is the first arm64 run).
- `port-20261108/`: logs kept from the amd64 port (the failed `vm.py fetch` runs and three slow
  suite runs).
- `kvm-probe-and-fallback.log`, `fetch-resume-amd64.log`: the checks under "KVM probe, TCG
  fallback and resumed downloads".

## Reproduce

```
uv run python tools/vm/vm.py runner
uv run python tools/vm/vm.py fetch
uv run python tools/vm/vm.py golden
uv run python tools/vm/vm.py plant all
uv run python tools/vm/prove.py              # all faults, about 11 minutes under TCG
uv run python tools/vm/prove.py sshd-masked  # one fault
uv run python tools/vm/vm.py status          # nothing left running afterwards
```
