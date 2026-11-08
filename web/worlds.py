"""The broken server a /try run lends the keyboard to.

A ``World`` bundles everything one run needs: the console (screen and keyboard), the owner probe
spec and its SSH and HTTP transports, the model, and how to tear it all down.

Two kinds:

- ``VmWorlds`` boots a fresh copy-on-write overlay of a local test VM planted with the chosen
  fault (``keyhold.console.image.new_run`` + ``QemuVM``), waits for the fault to show on the
  serial log, and uses Claude. The probe logs in with the hidden probe key over the VM's
  forwarded SSH port and fetches its demo page. Tearing down shuts the VM off and deletes the
  overlay.
- ``SimulatedWorlds`` builds a scripted console (``FakeConsole``), a pretend server for the probe
  (``FakeServer``) and a recorded agent script (``FakeModel``) for each of the three faults on
  /try. Nothing is booted and no model is called. The pages say so.

Only the faults in ``keyhold.faults.TRY_FAULTS`` can be booted from the web.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from keyhold.agent.model import FakeModel, json_reply, tool_reply
from keyhold.console.fake import FakeConsole, InputRecord, Rule, Screen
from keyhold.faults import TRY_FAULTS, get_fault
from keyhold.probe.fake import FakeServer
from keyhold.types import Console, ModelClient, ProbeSpec, Tier

__all__ = [
    "SimulatedWorlds",
    "VmWorlds",
    "World",
    "WorldError",
    "WorldFactory",
]

Progress = Callable[[str], None]
log = logging.getLogger(__name__)


class WorldError(RuntimeError):
    """The server could not be prepared. The message is plain text for the page."""


@dataclass(eq=False)
class World:
    fault_id: str
    kind: str  # "simulated" or "vm"
    label: str
    console: Console
    probe: ProbeSpec
    ssh: Any  # keyhold.probe.owner.SshTransport
    http: Any  # keyhold.probe.owner.HttpTransport
    model: ModelClient
    paid_model: bool
    console_password: str | None
    screen_text: Callable[[], str] | None = None
    keep_slot_for_warranty: bool = True
    closer: Callable[[], None] | None = None
    server_ssh_port: int | None = None  # sshd's port inside the server, when the probe goes through a forward
    closed: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
        if self.closer is not None:
            self.closer()


class WorldFactory(Protocol):
    kind: str
    paid_model: bool

    def problems(self) -> list[str]:
        """Why a world cannot be booted right now (empty when ready)."""
        ...

    def boot(self, fault_id: str, run_id: str, progress: Progress) -> World: ...


# --------------------------------------------------------------------------------------------
# Simulated worlds
# --------------------------------------------------------------------------------------------

SIM_HOST = "simulated.test"
PAGE_TEXT = "keyhold demo page"
PAGE_BODY = f"<!doctype html><title>demo</title><p>{PAGE_TEXT}: the page is being served.</p>"
HOSTNAME = "keyhold-demo"


def _sim_public_key() -> str:
    """A well-formed ssh-ed25519 public key with no private half anywhere: the simulated probe
    never logs in for real, but the spec, its hash and the fingerprint look as they do live."""
    name = b"ssh-ed25519"
    point = hashlib.sha256(b"keyhold simulated probe key").digest()
    blob = len(name).to_bytes(4, "big") + name + len(point).to_bytes(4, "big") + point
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def _console_password() -> str:
    """A fresh root password for each simulated server. The agent types it at the password
    prompt; the orchestrator redacts it from every stored and streamed event."""
    return "kh" + secrets.token_hex(8)


def simulated_probe_spec() -> ProbeSpec:
    return ProbeSpec(
        host=SIM_HOST,
        ssh_port=22,
        ssh_user="probe",
        probe_public_key=_sim_public_key(),
        url=f"http://{SIM_HOST}/",
        expect_substring=PAGE_TEXT,
    )


class SimulatedConsole:
    """``Console`` over a ``FakeConsole`` that lets the simulated server react to what was typed
    (fix a file, restart a service, reboot) after each input has been fully processed."""

    def __init__(self, fake: FakeConsole, on_line: Callable[[str, str], None], on_power: Callable[[], None]) -> None:
        self.fake = fake
        self._on_line = on_line
        self._on_power = on_power
        self._pending: list[InputRecord] = []
        fake.on_input(self._record)

    def _record(self, record: InputRecord) -> None:
        self._pending.append(record)

    def screenshot(self) -> bytes:
        return self.fake.screenshot()

    def screen_text(self) -> str:
        return "\n".join(self.fake.visible_lines())

    def type_text(self, text: str, abort: Callable[[], bool] | None = None) -> None:
        self.fake.type_text(text, abort=abort)
        self._settle()

    def press(self, keys: list[str]) -> None:
        self.fake.press(keys)
        self._settle()

    def power_cycle(self) -> None:
        self.fake.power_cycle()
        self._settle()
        self._on_power()

    def close(self) -> None:
        self.fake.close()

    def _settle(self) -> None:
        pending, self._pending = self._pending, []
        for record in pending:
            if record.kind == "type" and isinstance(record.value, str):
                for line in record.value.split("\n")[:-1]:
                    self._on_line(record.screen, line.strip())


def _boot_lines(*middle: str) -> list[str]:
    return [
        "[  OK  ] Started systemd-journald.service - Journal Service.",
        "[  OK  ] Reached target network-online.target - Network is Online.",
        *middle,
        "",
        f"Debian GNU/Linux 13 {HOSTNAME} tty1",
        "",
    ]


SSHD_FAILED = "[FAILED] Failed to start ssh.service - OpenBSD Secure Shell server."
SSHD_BAD_LINE = "/etc/ssh/sshd_config line 3: Bad configuration option: PermitRootLogn"
FSTAB_LINE = "UUID=7d1e3c55-2b8f-4c1e-9a0d-5f2a61c0b9e4 /srv/data ext4 defaults,x-systemd.device-timeout=15s 0 2"
EMERGENCY = 'You are in emergency mode. After logging in, type "journalctl -xb" to view'
DEPEND = "[DEPEND] Dependency failed for srv-data.mount - /srv/data."


def _sshd_world(run_id: str) -> World:
    password = _console_password()
    server = FakeServer(ssh_up=False, http_up=True, body=PAGE_BODY)
    fake = FakeConsole(
        {
            "boot": Screen(
                _boot_lines(
                    "[  OK  ] Listening on demo-page.socket - Keyhold demo page.",
                    SSHD_FAILED,
                    "See 'systemctl status ssh.service' for details.",
                    "[  OK  ] Reached target multi-user.target - Multi-User System.",
                ),
                prompt=f"{HOSTNAME} login: ",
            ),
            "password": Screen([], prompt="Password: ", echo=False),
            "shell": Screen([f"Linux {HOSTNAME} 6.12.48+deb13-arm64 #1 SMP Debian aarch64", ""], prompt=f"root@{HOSTNAME}:~# "),
        },
        rules=[
            Rule(on="boot", line="root", goto="password"),
            Rule(on="password", line=re.escape(password), goto="shell"),
            Rule(on="password", line=".*", goto="boot", output=["", "Login incorrect"]),
            Rule(
                on="shell",
                line=r"sshd -t",
                output=[SSHD_BAD_LINE, "/etc/ssh/sshd_config: terminating, 1 bad configuration options"],
                once=True,
            ),
            Rule(on="shell", line=r"systemctl is-active ssh", output=["active"]),
        ],
    )
    state = {"fixed": False}

    def on_line(screen: str, line: str) -> None:
        if screen != "shell":
            return
        if re.fullmatch(r"sed -i '3d' /etc/ssh/sshd_config", line):
            state["fixed"] = True
        if re.fullmatch(r"systemctl restart (ssh|sshd)(\.service)?", line) and state["fixed"]:
            server.fix()

    console = SimulatedConsole(fake, on_line, on_power=lambda: None)
    script = [
        json_reply(
            {
                "fault_class": "sshd_config_error",
                "evidence": [SSHD_FAILED],
                "tier": Tier.T1.value,
                "plan": "1. Log in as root on the console.\n2. Run sshd -t to find the bad line.\n"
                "3. Back up sshd_config and remove the bad line.\n4. Restart ssh.",
            }
        ),
        tool_reply(
            "type", {"text": "root\n"}, thinking="ssh.service failed at boot. Logging in on the console as root."
        ),
        tool_reply("type", {"text": password + "\n"}),
        tool_reply("type", {"text": "sshd -t\n"}, thinking="Asking sshd to check its own configuration."),
        tool_reply(
            "type",
            {"text": "cp /etc/ssh/sshd_config /etc/ssh/sshd_config.keyhold-bak\n"},
            thinking="Line 3 has a misspelled option. Backing up the file before editing it.",
        ),
        tool_reply("type", {"text": "sed -i '3d' /etc/ssh/sshd_config\n"}),
        tool_reply("type", {"text": "systemctl restart ssh\n"}),
        tool_reply("type", {"text": "systemctl is-active ssh\n"}, thinking="Checking that sshd is running now."),
        tool_reply(
            "finished",
            {"summary": "Removed the misspelled option on line 3 of sshd_config (backup kept) and restarted ssh."},
        ),
    ]
    return World(
        fault_id="sshd-config-typo",
        kind="simulated",
        label="Simulated server (scripted console)",
        console=console,
        probe=simulated_probe_spec(),
        ssh=server.ssh,
        http=server.http,
        model=FakeModel(script),
        paid_model=False,
        console_password=password,
        screen_text=console.screen_text,
        keep_slot_for_warranty=False,
        closer=console.close,
    )


def _fstab_world(run_id: str) -> World:
    password = _console_password()
    server = FakeServer(ssh_up=False, http_up=False, body=PAGE_BODY)
    fake = FakeConsole(
        {
            "emergency": Screen(
                [
                    "[ TIME ] Timed out waiting for device dev-disk-by\\x2duuid-7d1e3c55.device.",
                    DEPEND,
                    "[DEPEND] Dependency failed for local-fs.target - Local File Systems.",
                    EMERGENCY,
                    'system logs, "systemctl reboot" to reboot, or "exit"',
                    "to continue bootup.",
                    "Enter root password for system maintenance",
                ],
                prompt="(or press Control-D to continue): ",
                echo=False,
            ),
            "shell": Screen(["", "Give root password for maintenance accepted."], prompt=f"root@{HOSTNAME}:~# "),
            "booted": Screen(
                _boot_lines(
                    "[  OK  ] Reached target local-fs.target - Local File Systems.",
                    "[  OK  ] Started ssh.service - OpenBSD Secure Shell server.",
                    "[  OK  ] Listening on demo-page.socket - Keyhold demo page.",
                    "[  OK  ] Reached target multi-user.target - Multi-User System.",
                ),
                prompt=f"{HOSTNAME} login: ",
            ),
        },
        rules=[
            Rule(on="emergency", line=re.escape(password), goto="shell"),
            Rule(on="emergency", line=".*", output=["Login incorrect", "Enter root password for system maintenance"]),
            Rule(on="shell", line=r"grep -n srv /etc/fstab", output=[f"4:{FSTAB_LINE}"]),
            Rule(on="shell", line=r"lsblk", output=["NAME   MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS", "vda    254:0    0   3G  0 disk",
                                                    "|-vda1 254:1    0 2.9G  0 part /", "`-vda15 254:15  0 127M  0 part /boot/efi"]),
        ],
    )
    state = {"fixed": False}

    def boot_again() -> None:
        if state["fixed"]:
            fake.goto("booted")
            server.fix()
        else:
            fake.goto("emergency")

    def on_line(screen: str, line: str) -> None:
        if screen != "shell":
            return
        if line.startswith("sed -i") and "/etc/fstab" in line and ("srv" in line or "7d1e3c55" in line):
            state["fixed"] = True
        if line in ("systemctl reboot", "reboot"):
            boot_again()

    console = SimulatedConsole(fake, on_line, on_power=boot_again)
    script = [
        json_reply(
            {
                "fault_class": "fstab_bad_entry",
                "evidence": [DEPEND, EMERGENCY],
                "tier": Tier.T2.value,
                "plan": "1. Enter the root password at the emergency prompt.\n2. Find the /srv/data line in "
                "/etc/fstab.\n3. Back up fstab and comment that line out.\n4. Reload systemd and reboot.",
            }
        ),
        tool_reply(
            "type",
            {"text": password + "\n"},
            thinking="The boot stopped in emergency mode waiting for /srv/data. Entering the root password.",
        ),
        tool_reply("type", {"text": "grep -n srv /etc/fstab\n"}),
        tool_reply("type", {"text": "lsblk\n"}, thinking="Checking which disks are attached."),
        tool_reply(
            "type",
            {"text": "mkfs.ext4 /dev/vdb\n"},
            thinking="The data disk is not there. Maybe a fresh filesystem would let the mount succeed.",
        ),
        tool_reply(
            "type",
            {"text": "cp /etc/fstab /etc/fstab.keyhold-bak\n"},
            thinking="Erasing disks is refused, and there is no second disk anyway. The fix is to stop "
            "waiting for it: back up fstab and comment out the /srv/data line.",
        ),
        tool_reply("type", {"text": "sed -i 's|^UUID=7d1e3c55|# &|' /etc/fstab\n"}),
        tool_reply("type", {"text": "systemctl daemon-reload\n"}),
        tool_reply("type", {"text": "systemctl reboot\n"}, thinking="Rebooting to check that the boot finishes."),
        tool_reply(
            "finished",
            {
                "summary": "Commented out the /srv/data line in /etc/fstab (the volume is gone; backup kept), "
                "reloaded systemd and rebooted. The boot now reaches the login prompt."
            },
        ),
    ]
    return World(
        fault_id="fstab-missing-disk",
        kind="simulated",
        label="Simulated server (scripted console)",
        console=console,
        probe=simulated_probe_spec(),
        ssh=server.ssh,
        http=server.http,
        model=FakeModel(script),
        paid_model=False,
        console_password=password,
        screen_text=console.screen_text,
        keep_slot_for_warranty=False,
        closer=console.close,
    )


def _grub_world(run_id: str) -> World:
    server = FakeServer(ssh_up=False, http_up=False, body=PAGE_BODY)
    fake = FakeConsole(
        {"grub": Screen(["", "                     GNU GRUB  version 2.12", ""], prompt="Enter username: ")}
    )
    console = SimulatedConsole(fake, lambda screen, line: None, on_power=lambda: None)
    script = [
        json_reply(
            {
                "fault_class": "console_locked",
                "evidence": ["Enter username:"],
                "tier": Tier.T2.value,
                "plan": "1. The boot menu asks for a GRUB superuser name and password.\n2. Without them "
                "nothing boots, so the console alone cannot fix this. No hold should be placed.",
            }
        )
    ]
    return World(
        fault_id="grub-password",
        kind="simulated",
        label="Simulated server (scripted console)",
        console=console,
        probe=simulated_probe_spec(),
        ssh=server.ssh,
        http=server.http,
        model=FakeModel(script),
        paid_model=False,
        console_password=None,
        screen_text=console.screen_text,
        keep_slot_for_warranty=False,
        closer=console.close,
    )


_SIMULATED: dict[str, Callable[[str], World]] = {
    "sshd-config-typo": _sshd_world,
    "fstab-missing-disk": _fstab_world,
    "grub-password": _grub_world,
}


class _PacedModel:
    """The recorded agent with a pause before each action after the free look, so the simulated
    Stop path lasts long enough to press Stop. Every other attribute is the wrapped model's."""

    def __init__(self, inner: Any, pause_s: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self._inner = inner
        self._pause_s = pause_s
        self._sleep = sleep
        self._calls = 0

    def respond(self, **kwargs: Any) -> Any:
        self._calls += 1
        if self._calls > 1:  # the first answer is the free look: no pause there
            self._sleep(self._pause_s)
        return self._inner.respond(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# The Stop path: the simulated agent is paced on this fault only.
STOP_PATH = "fstab-missing-disk"


class SimulatedWorlds:
    kind = "simulated"
    paid_model = False

    def __init__(self, *, stop_path_turn_s: float = 0.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.stop_path_turn_s = stop_path_turn_s
        self._sleep = sleep
        self._stop_steps: int | None = None

    def problems(self) -> list[str]:
        return []

    def stop_window_s(self) -> float:
        """About how long Stop can still void the hold on the Stop path: the agent's actions (paced
        or not) plus the 10 s between the two passes of the judge-mode probe."""
        if self._stop_steps is None:
            self._stop_steps = max(0, _fstab_world("0" * 12).model.remaining - 1)  # minus the free look
        return self.stop_path_turn_s * self._stop_steps + 10.0

    def boot_span(self) -> str:
        """How long the boot usually takes, for the run page: the scripted server starts at once."""
        return "a few seconds"

    def look_span(self) -> str:
        """How long the free look usually takes: the recorded agent answers at once."""
        return "a few seconds"

    def boot(self, fault_id: str, run_id: str, progress: Progress) -> World:
        if fault_id not in TRY_FAULTS or fault_id not in _SIMULATED:
            raise WorldError(f"fault {fault_id!r} is not offered here")
        progress("Building a scripted copy of the broken server (no VM in this mode).")
        world = _SIMULATED[fault_id](run_id)
        if fault_id == STOP_PATH and self.stop_path_turn_s > 0:
            world.model = _PacedModel(world.model, self.stop_path_turn_s, self._sleep)
        return world


# --------------------------------------------------------------------------------------------
# VM worlds
# --------------------------------------------------------------------------------------------


class VmWorlds:
    """Boots one local VM per run. Factories are injectable so the wiring can be tested."""

    kind = "vm"
    paid_model = True
    PROBLEM_CACHE_S = 60.0

    def __init__(
        self,
        settings: Any,
        *,
        home: Any = None,
        runner: Any = None,
        vm_factory: Callable[..., Any] | None = None,
        new_run: Callable[..., tuple[str, Any]] | None = None,
        model_factory: Callable[[], ModelClient] | None = None,
        ssh_factory: Callable[[Any, str], Any] | None = None,
        http_factory: Callable[[], Any] | None = None,
        environ: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._home = home
        self._runner = runner
        self._vm_factory = vm_factory
        self._new_run = new_run
        self._model_factory = model_factory
        self._ssh_factory = ssh_factory
        self._http_factory = http_factory
        self._environ = os.environ if environ is None else environ
        self._sleep = sleep
        self._monotonic = monotonic
        self._problems: tuple[float, list[str]] | None = None

    # -- lazily built pieces (keeps imports and docker calls out of app start-up) -------------

    @property
    def home(self) -> Any:
        if self._home is None:
            from keyhold.console.image import VMHome

            self._home = VMHome(self.settings.vm_home)
        return self._home

    @property
    def runner(self) -> Any:
        if self._runner is None:
            from keyhold.console.docker import DockerRunner

            self._runner = DockerRunner(self.home.root)
        return self._runner

    def problems(self) -> list[str]:
        now = self._monotonic()
        if self._problems is not None and now - self._problems[0] < self.PROBLEM_CACHE_S:
            return list(self._problems[1])
        found: list[str] = []
        try:
            if not self.runner.available():
                found.append("docker is not available on this host")
            elif not self.runner.image_present():
                found.append("the QEMU runner image is not built (tools/vm/vm.py runner)")
        except Exception as exc:  # noqa: BLE001 - report, do not crash the page
            found.append(f"docker could not be checked ({type(exc).__name__})")
        home = self.home
        if not home.golden_disk.is_file():
            found.append("the golden image is not built (tools/vm/vm.py golden)")
        for fault_id in TRY_FAULTS:
            if not home.fault_disk(fault_id).is_file():
                found.append(f"the fault image {fault_id} is not built (tools/vm/vm.py plant {fault_id})")
        if not home.probe_key.is_file():
            found.append("the probe key is missing (tools/vm/vm.py golden creates it)")
        if self._model_factory is None and not self._environ.get("ANTHROPIC_API_KEY"):
            found.append("ANTHROPIC_API_KEY is not set")
        self._problems = (now, found)
        return list(found)

    def boot_span(self) -> str:
        """How long a fault takes to show, for the /try page: seconds with KVM, longer emulated."""
        try:
            return "5 to 30 seconds" if self.runner.kvm_available() else "30 to 90 seconds"
        except Exception:
            return "30 to 90 seconds"

    def look_span(self) -> str:
        """How long the free look usually takes: a screenshot and one model answer."""
        return "20 to 60 seconds"

    def boot(self, fault_id: str, run_id: str, progress: Progress) -> World:
        from keyhold.console.image import (
            load_golden_info,
            probe_spec_for,
            read_console_password,
            remove_run,
        )
        from keyhold.console.image import new_run as image_new_run
        from keyhold.console.qemu import QemuVM

        if fault_id not in TRY_FAULTS:
            raise WorldError(f"fault {fault_id!r} is not offered here")
        fault = get_fault(fault_id)
        home, runner = self.home, self.runner
        make_run = self._new_run or image_new_run
        vm_run, disk = make_run(home, runner, fault_id, run_id)
        make_vm = self._vm_factory or QemuVM
        vm = make_vm(runner, name=f"keyhold-run-{vm_run}", run_dir=home.run_dir(vm_run), disk=disk)
        console = None
        try:
            progress("Starting a fresh overlay of the test VM with the fault planted.")
            endpoints = vm.start()
            span = "5 to 30 seconds" if vm.accel == "kvm" else "30 to 90 seconds"
            progress(f"Booting ({vm.accel}). The fault shows after {span}.")
            if vm.wait_for_serial(fault.ready_marker, timeout=self.settings.boot_timeout_s) is None:
                raise WorldError("the VM did not show the fault in time")
            # The screen can lag the serial marker; the free look must see the whole symptom.
            self._sleep(fault.screen_settle_s)
            console = vm.console()
            info = load_golden_info(home)
            spec = probe_spec_for(info, endpoints)
            password = read_console_password(home)
            ssh = self._make_ssh(vm_run)
            http = self._make_http()
            model = self._make_model()
        except BaseException:
            if console is not None:
                with contextlib.suppress(Exception):
                    console.close()
            try:
                vm.kill()
            finally:
                remove_run(home, vm_run)
            raise

        def close() -> None:
            with contextlib.suppress(Exception):
                console.close()
            try:
                # A guest held at a boot-loader prompt ignores the ACPI power button, so waiting
                # for a clean power-off would only keep the server slot busy.
                if not fault.acpi_power_off or not vm.shutdown(timeout=60.0):
                    vm.kill()
            except Exception:  # a VM that will not shut down cleanly is killed
                log.warning("shutting down VM %s failed; killing it", vm.name, exc_info=True)
                with contextlib.suppress(Exception):
                    vm.kill()
            remove_run(home, vm_run)

        progress("The fault is on the screen.")
        return World(
            fault_id=fault_id,
            kind="vm",
            label=f"Local test VM ({vm.accel})",
            console=console,
            probe=spec,
            ssh=ssh,
            http=http,
            model=model,
            paid_model=True,
            console_password=password,
            screen_text=None,
            keep_slot_for_warranty=True,
            closer=close,
            server_ssh_port=22,
        )

    def transports_for(self, rec: Any) -> tuple[Any, Any] | None:
        """Probe transports for a captured rescue this process did not start (after a restart,
        or a capture the reconciler completed). The probe spec stored at checkout says where to
        connect; a VM that is gone simply fails the checks, and the warranty refunds."""
        if not rec.probe_spec:
            return None
        return self._make_ssh(f"warranty-{str(rec.id)[:24]}"), self._make_http()

    def _make_ssh(self, vm_run: str) -> Any:
        if self._ssh_factory is not None:
            return self._ssh_factory(self.home.probe_key, vm_run)
        from keyhold.probe.owner import SubprocessSsh, known_hosts_path

        return SubprocessSsh(self.home.probe_key, known_hosts_path(self.settings.probe_state_dir, vm_run))

    def _make_http(self) -> Any:
        if self._http_factory is not None:
            return self._http_factory()
        from keyhold.probe.owner import HttpxFetcher

        return HttpxFetcher()

    def _make_model(self) -> ModelClient:
        if self._model_factory is not None:
            return self._model_factory()
        from keyhold.agent.model import ClaudeModel

        return ClaudeModel(
            model=self.settings.model,
            effort=self.settings.model_effort,
            session_header=self.settings.model_session_header or None,
        )
