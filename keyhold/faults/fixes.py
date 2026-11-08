"""Deterministic console fixes for the fault catalog.

Each solvable fault has a short script of agent tool calls (``type``, ``key``, ``power_cycle``,
then ``finished``) that repairs it from the console alone. The unsolvable fault's script ends
with ``give_up``. These scripts stand in for the model when Keyhold is proven on real guests
(``tools/vm/prove.py``): they go through the same agent loop, keystroke firewall, gated console
and PayPal hold as a model's tool calls would. They are never shown to the model.

``wait_for`` is a regular expression on the guest's serial log (kernel messages and the journal)
that shows the step has taken effect; the driver waits for it before sending the next step, so
the script does not depend on how fast the emulated guest happens to be. ``pause`` is a fixed
wait for steps that leave no trace on the serial log (a login prompt drawn on the screen only).

The console password never appears here. A step whose text holds ``PASSWORD`` gets the password
from the VM home at run time and is marked ``secret`` so the driver never logs its text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

__all__ = ["FIXES", "PASSWORD", "FixStep", "fix_for", "render_input"]

PASSWORD = "{console_password}"  # placeholder, replaced at run time
_TOOLS = frozenset({"type", "key", "power_cycle", "give_up", "finished"})


@dataclass(frozen=True)
class FixStep:
    """One agent tool call.

    ``text``: the text to type, the key combo (``key``), or the reason/summary (``give_up``,
    ``finished``). ``wait_for``: serial-log regex to wait for after this step (searched from the
    moment the step was sent). ``timeout``: seconds to wait for it. ``pause``: seconds to wait
    after that.
    """

    tool: str
    text: str = ""
    wait_for: str | None = None
    timeout: float = 120.0
    pause: float = 0.0

    def __post_init__(self) -> None:
        if self.tool not in _TOOLS:
            raise ValueError(f"unknown tool {self.tool!r}")
        if self.tool != "power_cycle" and not self.text:
            raise ValueError(f"a {self.tool} step needs text")
        if self.timeout <= 0 or self.pause < 0:
            raise ValueError("timeout must be positive and pause not negative")

    @property
    def secret(self) -> bool:
        return PASSWORD in self.text

    @property
    def final(self) -> bool:
        return self.tool in ("give_up", "finished")


def render_input(step: FixStep, console_password: str) -> dict[str, Any]:
    """The tool input for ``step``, with the password placeholder filled in."""
    text = step.text.replace(PASSWORD, console_password)
    if step.tool == "type":
        return {"text": text}
    if step.tool == "key":
        return {"combo": text}
    if step.tool == "give_up":
        return {"reason": text}
    if step.tool == "finished":
        return {"summary": text}
    return {}


# Logging in as root on the screen console (tty1). The login prompt is drawn on the screen only,
# so the first step waits a fixed time; the PAM session message reaches the serial log.
_LOGIN = (
    FixStep("type", "root\n", pause=4.0),
    FixStep("type", PASSWORD + "\n", wait_for=r"session opened for user root", timeout=60.0, pause=2.0),
)

_FIX_LIST: dict[str, tuple[FixStep, ...]] = {
    "sshd-config-typo": (
        *_LOGIN,
        FixStep("type", "sshd -t\n", pause=2.0),
        # Not "&& sshd -t" after the edit: with ssh.service down, /run/sshd does not exist and
        # sshd -t fails on that instead (seen on a real guest). systemd also gave up restarting
        # ssh at boot (start limit), so reset-failed comes before the restart.
        FixStep(
            "type",
            "sed -i '/^PermitRootLogn no$/d' /etc/ssh/sshd_config && systemctl reset-failed ssh.service"
            " && systemctl restart ssh.service\n",
            wait_for=r"Started ssh\.service",
        ),
        FixStep(
            "finished", "sshd_config had a misspelled option (PermitRootLogn); removed that line and restarted ssh."
        ),
    ),
    "sshd-masked": (
        *_LOGIN,
        FixStep(
            "type",
            "systemctl unmask ssh.service ssh.socket && systemctl enable --now ssh.service\n",
            wait_for=r"Started ssh\.service",
        ),
        FixStep("finished", "ssh.service was masked and disabled; unmasked, enabled and started it."),
    ),
    "sshd-port-page-down": (
        *_LOGIN,
        FixStep(
            "type",
            "rm /etc/ssh/sshd_config.d/10-port.conf && systemctl restart ssh"
            " && systemctl enable --now demo-page.socket\n",
            wait_for=r"Listening on demo-page\.socket",
        ),
        FixStep(
            "finished",
            "A drop-in moved sshd to port 2222 and the page socket was disabled; removed the drop-in, "
            "restarted ssh and enabled the page socket.",
        ),
    ),
    "network-static-wrong": (
        *_LOGIN,
        FixStep(
            "type",
            "sed -i -e 's/dhcp4: false/dhcp4: true/' -e '/addresses:/d' -e '/routes:/,$d'"
            " /etc/netplan/50-cloud-init.yaml && netplan apply\n",
            wait_for=r"DHCPv4 address",
            timeout=180.0,
            pause=3.0,
        ),
        FixStep("finished", "netplan had a static address from another network; switched eth0 back to DHCP."),
    ),
    "fstab-missing-disk": (
        # The emergency shell asks for the root password straight away.
        FixStep("type", PASSWORD + "\n", pause=5.0),
        FixStep(
            "type",
            "mount -o remount,rw / && sed -i 's|^UUID=7d1e3c55|#&|' /etc/fstab && systemctl daemon-reload\n",
            wait_for=r"Reloading finished",
            timeout=60.0,
        ),
        FixStep("type", "systemctl reboot\n", wait_for=r"login:", timeout=420.0, pause=5.0),
        FixStep(
            "finished",
            "fstab listed a missing data disk without nofail; commented that line out and rebooted.",
        ),
    ),
    "grub-password": (
        FixStep("type", "root\n", pause=3.0),
        FixStep("power_cycle", wait_for=r"Enter username", timeout=240.0, pause=2.0),
        FixStep(
            "give_up",
            "The boot menu asks for a GRUB user name and password before anything boots, and a reset "
            "lands on the same prompt. No password was given, so this cannot be fixed from the console.",
        ),
    ),
}

FIXES: Mapping[str, tuple[FixStep, ...]] = MappingProxyType(_FIX_LIST)


def fix_for(fault_id: str) -> tuple[FixStep, ...]:
    try:
        return FIXES[fault_id]
    except KeyError:
        raise KeyError(f"no console fix script for fault {fault_id!r}") from None
