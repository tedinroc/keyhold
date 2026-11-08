"""Fault catalog. Each fault has a plant script, the symptom text expected on screen, a fault class,
the tier for that class, and a solvable flag (False when the console alone cannot fix it, for
example a password-protected GRUB menu).

Two tables live here:

- ``FAULT_CLASSES``: the fixed taxonomy the free look answers from, each class with its price tier
  (None for the classes that are never priced). ``tier_for_class`` is the class -> tier check.
  ``keyhold.agent.quote.TAXONOMY`` takes its tiers from this table.
- ``FAULTS``: the faults Keyhold can plant into a test guest. A plant script is bash run as root
  inside a prepare boot of the golden image (see ``keyhold.faults.plant``); when it finishes, the
  guest powers off and the disk holds the broken server.

``symptom`` is text a person (or the model) can read on the console once the faulty server has
booted. ``ready_marker`` is a regular expression on the guest's serial log that says the fault
has shown itself; tests and tools wait for it before handing over the console.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from keyhold.types import Tier

__all__ = [
    "FaultClass",
    "FAULT_CLASSES",
    "CLASS_TIER",
    "tier_for_class",
    "Fault",
    "FAULTS",
    "TRY_FAULTS",
    "get_fault",
]


@dataclass(frozen=True)
class FaultClass:
    """One class of the taxonomy. ``tier`` None: the class can be named but is never priced (no
    hold is offered), for faults the console alone cannot fix or cannot identify."""

    name: str
    tier: Tier | None
    description: str


# This is the class -> tier table. keyhold.agent.quote takes each class's tier from here and
# refuses to import if its class list differs.
_CLASSES = [
    # T1: one configuration fix
    FaultClass(
        "sshd_config_error", Tier.T1, "sshd will not start, or listens on the wrong port, because of its configuration"
    ),
    FaultClass("ssh_service_disabled", Tier.T1, "the SSH service is stopped, disabled or masked"),
    FaultClass("ssh_host_keys_missing", Tier.T1, "sshd will not start because its host keys are missing"),
    FaultClass("firewall_blocks_ssh", Tier.T1, "a packet filter rule drops or rejects SSH traffic"),
    FaultClass(
        "network_config_error", Tier.T1, "the server has no working network address because of a configuration error"
    ),
    # T2: boot path repair
    FaultClass("fstab_bad_entry", Tier.T2, "a bad /etc/fstab entry stops the boot in emergency mode"),
    FaultClass(
        "boot_target_wrong",
        Tier.T2,
        "the system boots into rescue mode because of its default target or a failing unit",
    ),
    FaultClass(
        "grub_config_error", Tier.T2, "the boot loader cannot find or read its configuration and stops at a grub prompt"
    ),
    # T3: kernel, initramfs or disk
    FaultClass(
        "initramfs_broken",
        Tier.T3,
        "the kernel cannot mount the root filesystem (initramfs missing, broken or wrong root)",
    ),
    FaultClass("kernel_missing", Tier.T3, "the kernel image is missing or will not load"),
    FaultClass("filesystem_errors", Tier.T3, "the root filesystem has errors that stop the boot until fsck runs"),
    FaultClass("disk_full", Tier.T3, "the root filesystem is full, so services cannot start"),
    # Not priced
    FaultClass(
        "console_locked",
        None,
        "the fix needs a password or key the console does not give (a password-protected boot menu)",
    ),
    FaultClass("unknown", None, "the screen does not show enough to name the fault"),
]

FAULT_CLASSES: Mapping[str, FaultClass] = MappingProxyType({c.name: c for c in _CLASSES})
CLASS_TIER: Mapping[str, Tier | None] = MappingProxyType({c.name: c.tier for c in _CLASSES})


def tier_for_class(fault_class: str) -> Tier | None:
    """The tier for a fault class, or None for a class that is never priced. Raises
    ``ValueError`` for a class outside the taxonomy."""
    try:
        return CLASS_TIER[fault_class]
    except KeyError:
        raise ValueError(f"unknown fault class {fault_class!r}") from None


@dataclass(frozen=True)
class Fault:
    """A fault that can be planted into the golden test guest.

    ``plant``: bash run as root (``set -euo pipefail``) in a prepare boot.
    ``symptom``: lines expected on the console screen after the faulty server boots.
    ``ready_marker``: regex on the serial log that appears once the symptom is on screen.
    ``fix``: how a person would repair it from the console (documentation and tests only; it is
    never shown to the model).
    ``boot_seconds``: rough time from power-on to the symptom under TCG emulation.
    ``screen_settle_s``: seconds to wait after ``ready_marker`` before reading the screen. The
    screen can lag the serial log, and some prompts are drawn after their marker.
    ``acpi_power_off``: False when the guest stops before the operating system runs (a boot
    loader prompt) and so ignores the ACPI power button; tools then stop QEMU at once instead of
    waiting for a clean power-off.
    """

    id: str
    title: str
    fault_class: str
    solvable: bool
    plant: str
    symptom: tuple[str, ...]
    ready_marker: str
    fix: str
    boot_seconds: int = 60
    screen_settle_s: float = 5.0
    acpi_power_off: bool = True

    @property
    def tier(self) -> Tier | None:
        """The tier of the fault's class; None when the class is never priced."""
        return tier_for_class(self.fault_class)

    @property
    def price(self) -> Decimal | None:
        tier = self.tier
        return tier.price if tier is not None else None

    def __post_init__(self) -> None:
        tier_for_class(self.fault_class)  # the class must be in the taxonomy
        if not self.symptom:
            raise ValueError(f"fault {self.id} needs symptom text")
        if self.screen_settle_s < 0:
            raise ValueError(f"fault {self.id}: screen_settle_s must not be negative")


_LOGIN = r"login:"

_FAULT_LIST = [
    Fault(
        id="sshd-config-typo",
        title="Typo in sshd_config",
        fault_class="sshd_config_error",
        solvable=True,
        plant="""\
# A mistyped option on line 3 of sshd_config: sshd -t fails, so ssh.service does not start.
sed -i '3i PermitRootLogn no' /etc/ssh/sshd_config
""",
        symptom=(
            "[FAILED] Failed to start ssh.service - OpenBSD Secure Shell server.",
            "keyhold-demo login:",
        ),
        ready_marker=_LOGIN,
        fix="Log in as root, run 'sshd -t' to find the bad line, delete line 3 of "
        "/etc/ssh/sshd_config, then 'systemctl restart ssh'.",
        boot_seconds=50,
    ),
    Fault(
        id="sshd-masked",
        title="SSH service masked",
        fault_class="ssh_service_disabled",
        solvable=True,
        plant="""\
# Someone masked SSH while "hardening" the box: no error at boot, sshd simply never starts.
systemctl disable ssh.service
systemctl mask ssh.service ssh.socket
""",
        symptom=("Debian GNU/Linux 13 keyhold-demo tty1", "keyhold-demo login:"),
        ready_marker=_LOGIN,
        fix="Log in as root, 'systemctl unmask ssh.service ssh.socket' and 'systemctl enable --now ssh.service'.",
        boot_seconds=50,
    ),
    Fault(
        id="sshd-port-page-down",
        title="sshd moved to port 2222 and the page stopped",
        fault_class="sshd_config_error",
        solvable=True,
        plant="""\
# sshd moved to another port by a drop-in, and the demo page socket left disabled after a test.
cat > /etc/ssh/sshd_config.d/10-port.conf <<'EOF'
# Moved off the default port to cut down log noise.
Port 2222
EOF
systemctl disable demo-page.socket
""",
        symptom=("Debian GNU/Linux 13 keyhold-demo tty1", "keyhold-demo login:"),
        ready_marker=_LOGIN,
        fix="Log in as root, remove /etc/ssh/sshd_config.d/10-port.conf, 'systemctl restart ssh', "
        "then 'systemctl enable --now demo-page.socket'.",
        boot_seconds=50,
    ),
    Fault(
        id="network-static-wrong",
        title="Static address on the wrong subnet",
        fault_class="network_config_error",
        solvable=True,
        plant="""\
# DHCP replaced by a static address from another network: the server is up but unreachable.
cat > /etc/netplan/50-cloud-init.yaml <<'EOF'
network:
  version: 2
  ethernets:
    eth0:
      match:
        macaddress: "52:54:00:4b:48:01"
      set-name: "eth0"
      dhcp4: false
      addresses: [192.0.2.10/24]
      routes:
        - to: default
          via: 192.0.2.1
EOF
chmod 600 /etc/netplan/50-cloud-init.yaml
""",
        symptom=("Debian GNU/Linux 13 keyhold-demo tty1", "keyhold-demo login:"),
        ready_marker=_LOGIN,
        fix="Log in as root, set 'dhcp4: true' and remove 'addresses' and 'routes' in "
        "/etc/netplan/50-cloud-init.yaml, then 'netplan apply'.",
        boot_seconds=60,
    ),
    Fault(
        id="fstab-missing-disk",
        title="fstab lists a disk that is gone",
        fault_class="fstab_bad_entry",
        solvable=True,
        plant="""\
# A data volume was detached but its fstab line stayed, without nofail: the boot waits for the
# disk, gives up, and drops to emergency mode.
mkdir -p /srv/data
cat >> /etc/fstab <<'EOF'
# data volume
UUID=7d1e3c55-2b8f-4c1e-9a0d-5f2a61c0b9e4 /srv/data ext4 defaults,x-systemd.device-timeout=15s 0 2
EOF
""",
        symptom=(
            "Timed out waiting for device",
            "[DEPEND] Dependency failed for srv-data.mount - /srv/data.",
            "You are in emergency mode.",
            "Enter root password for system maintenance",
        ),
        # The password prompt appears on the screen only; the journal copy on serial has this line.
        ready_marker=r"Reached target emergency\.target",
        fix="Enter the root password at the emergency prompt, comment out the /srv/data line in "
        "/etc/fstab (or add nofail), 'systemctl daemon-reload', then 'systemctl reboot'.",
        boot_seconds=80,
        # The emergency password prompt reaches the screen several seconds after the marker.
        screen_settle_s=10.0,
    ),
    Fault(
        id="grub-password",
        title="GRUB locked by a password",
        fault_class="console_locked",
        solvable=False,
        plant="""\
# GRUB superuser with a random password nobody keeps: every boot entry asks for it.
pw=$(head -c 24 /dev/urandom | base64)
hash=$(printf '%s\\n%s\\n' "$pw" "$pw" | grub-mkpasswd-pbkdf2 | sed -n 's/.*\\(grub\\.pbkdf2\\.[^ ]*\\)$/\\1/p')
unset pw
test -n "$hash"
cat > /etc/grub.d/01_password <<EOF
#!/bin/sh
cat <<'GRUBEOF'
set superusers="admin"
password_pbkdf2 admin $hash
GRUBEOF
EOF
chmod 755 /etc/grub.d/01_password
update-grub
""",
        symptom=("Enter username:",),
        ready_marker=r"Enter username",
        fix="None from the console: without the GRUB password nothing boots. The honest answer "
        "is give_up, which voids the hold.",
        boot_seconds=15,
        acpi_power_off=False,
    ),
]

FAULTS: Mapping[str, Fault] = MappingProxyType({f.id: f for f in _FAULT_LIST})

# The faults offered on the public /try page: one T1, one T2 and the honest stop.
TRY_FAULTS: tuple[str, ...] = ("sshd-config-typo", "fstab-missing-disk", "grub-password")


def get_fault(fault_id: str) -> Fault:
    try:
        return FAULTS[fault_id]
    except KeyError:
        raise KeyError(f"unknown fault {fault_id!r}; known: {', '.join(FAULTS)}") from None
