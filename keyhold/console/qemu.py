"""QEMU console backend over QMP: screendump to PNG, send-key for input, system_reset for
power_cycle. QEMU runs inside a local Linux container (no KVM locally).

Two pieces:

- ``QemuConsole`` implements ``keyhold.types.Console`` on one QMP connection. It can only look at
  the screen, press keys and reset the machine. ``close()`` ends the console session; the server
  keeps running, as it does when you close a provider's web console.
- ``QemuVM`` starts and stops one guest in a runner container (see ``keyhold.console.docker``).
  QEMU gets two QMP sockets: one lent to the console, one kept for control (status, clean
  shutdown). The guest's SSH and web ports are forwarded to host loopback ports for the owner
  probe. Guest networking is QEMU user mode with ``restrict=on``: the guest can answer forwarded
  connections but cannot open connections of its own, so an agent at the keyboard cannot reach
  the internet from it.

The guest is a 64-bit Arm machine (``virt``) with UEFI firmware, a ``ramfb`` framebuffer (the
firmware, GRUB and the Linux console all draw on it), a USB keyboard for the firmware and GRUB and
a virtio keyboard for Linux. With KVM available on an
aarch64 docker host it uses KVM; otherwise TCG emulation.
"""

from __future__ import annotations

import io
import itertools
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from keyhold.console.base import ConsoleClosed, ConsoleError, normalize_chord, text_to_chords
from keyhold.console.docker import LOOPBACK, DockerError, DockerRunner
from keyhold.console.qmp import QMPClient, QMPConnectionError, QMPError

__all__ = [
    "FIRMWARE_CODE",
    "FIRMWARE_VARS",
    "QMP_CONSOLE_PORT",
    "QMP_CONTROL_PORT",
    "SSH_FORWARD_PORT",
    "HTTP_FORWARD_PORT",
    "GUEST_MAC",
    "GuestSpec",
    "qemu_argv",
    "QemuConsole",
    "QemuVM",
    "VMEndpoints",
]

# The Secure Boot build of the firmware, because it has no built-in UEFI shell: with a shell, an
# agent could edit the bootloader files on the EFI partition and walk around a GRUB password. With
# the plain variable store (no keys enrolled) Secure Boot stays off, so the guest boots as usual.
FIRMWARE_CODE = "/usr/share/AAVMF/AAVMF_CODE.secboot.fd"
FIRMWARE_VARS = "/usr/share/AAVMF/AAVMF_VARS.fd"
QMP_CONSOLE_PORT = 4444
QMP_CONTROL_PORT = 4445
SSH_FORWARD_PORT = 2222  # container port relayed to the guest's port 22
HTTP_FORWARD_PORT = 8080  # container port relayed to the guest's port 80
SSH_LOCAL_PORT = 12222  # QEMU's forward to guest port 22, on the container's loopback
HTTP_LOCAL_PORT = 18080  # QEMU's forward to guest port 80, on the container's loopback
ENTRYPOINT = "keyhold-vm-entry"  # tools/vm/vm-entry.sh in the runner image
GUEST_MAC = "52:54:00:4b:48:01"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class GuestSpec:
    """Everything ``qemu_argv`` needs. Paths are as QEMU sees them (inside the container)."""

    disk: str
    vars: str
    extra_drives: Sequence[str] = ()  # read-only raw images: a cloud-init seed or a plant disk
    serial_log: str | None = None
    name: str = "keyhold"
    memory_mb: int = 1024
    cpus: int = 2
    accel: str = "tcg"  # "tcg" or "kvm"
    listen: str = "0.0.0.0"  # address QMP binds to inside the container
    qmp_ports: Sequence[int] = (QMP_CONSOLE_PORT, QMP_CONTROL_PORT)
    # Guest port forwards: listening port -> guest port. They bind to loopback, because with
    # restrict=on the guest only answers connections that come from there (see vm-entry.sh).
    forward_listen: str = "127.0.0.1"
    forwards: Mapping[int, int] = field(default_factory=lambda: {SSH_LOCAL_PORT: 22, HTTP_LOCAL_PORT: 80})
    stay_on_poweroff: bool = True  # keep QEMU (and the screen) after the guest powers off
    firmware_code: str = FIRMWARE_CODE

    def __post_init__(self) -> None:
        if self.accel not in ("tcg", "kvm"):
            raise ValueError("accel must be 'tcg' or 'kvm'")
        if self.memory_mb < 256 or self.cpus < 1:
            raise ValueError("a guest needs at least 256 MB and one CPU")


def qemu_argv(spec: GuestSpec) -> list[str]:
    """The qemu-system-aarch64 command line for ``spec``."""
    if spec.accel == "kvm":
        machine = ["-machine", "virt,gic-version=host", "-accel", "kvm", "-cpu", "host"]
    else:
        # pauth-impdef: a cheap pointer-authentication algorithm, much faster under emulation.
        machine = [
            "-machine",
            "virt,gic-version=3",
            "-accel",
            "tcg,thread=multi,tb-size=256",
            "-cpu",
            "max,pauth-impdef=on",
        ]
    argv = [
        "qemu-system-aarch64",
        "-nodefaults",
        "-name",
        spec.name,
        *machine,
        "-smp",
        str(spec.cpus),
        "-m",
        str(spec.memory_mb),
        "-rtc",
        "base=utc",
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={spec.firmware_code}",
        "-drive",
        f"if=pflash,format=raw,file={spec.vars}",
        "-drive",
        f"if=none,id=disk0,file={spec.disk},format=qcow2,discard=unmap",
        "-device",
        "virtio-blk-pci,drive=disk0,bootindex=0",
    ]
    for index, drive in enumerate(spec.extra_drives, start=1):
        argv += [
            "-drive",
            f"if=none,id=extra{index},file={drive},format=raw,readonly=on",
            "-device",
            f"virtio-blk-pci,drive=extra{index}",
        ]
    netdev = "user,id=net0,restrict=on"
    for listen_port, guest_port in spec.forwards.items():
        netdev += f",hostfwd=tcp:{spec.forward_listen}:{int(listen_port)}-:{int(guest_port)}"
    argv += [
        "-netdev",
        netdev,
        # romfile=: the firmware has its own virtio-net driver; no option ROM needed.
        "-device",
        f"virtio-net-pci,netdev=net0,mac={GUEST_MAC},romfile=",
        "-device",
        "virtio-rng-pci",
        "-device",
        "ramfb",
        # Two keyboards: the firmware and GRUB read the USB one; the Debian cloud kernel has no
        # USB support, so Linux reads the virtio one. QEMU sends keys to whichever the running
        # software has activated.
        "-device",
        "qemu-xhci,id=xhci",
        "-device",
        "usb-kbd,bus=xhci.0",
        "-device",
        "virtio-keyboard-pci",
        "-display",
        "none",
    ]
    if spec.serial_log:
        argv += ["-chardev", f"file,id=serial0,path={spec.serial_log}", "-serial", "chardev:serial0"]
    for port in spec.qmp_ports:
        argv += ["-qmp", f"tcp:{spec.listen}:{int(port)},server=on,wait=off"]
    if spec.stay_on_poweroff:
        argv.append("-no-shutdown")
    return argv


# ---------------------------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------------------------


class QemuConsole:
    """``keyhold.types.Console`` over a QMP connection.

    Screenshots: QMP ``screendump`` writes a PNG into ``shot_dir`` as QEMU sees it
    (``shot_dir_qemu``), and the console reads it back from the same directory on this machine
    (``shot_dir_local``), then deletes it.

    Typing: one ``send-key`` per character with ``hold_ms`` between key events. QEMU queues the
    events and plays them at that pace; the console sends text in chunks and waits for each chunk
    to play out, so QEMU's queue never overflows and a screenshot taken right after
    ``type_text`` returns shows the text.
    """

    def __init__(
        self,
        qmp: QMPClient,
        *,
        shot_dir_qemu: str,
        shot_dir_local: Path,
        hold_ms: int = 25,
        chunk: int = 40,
        settle: float = 0.15,
        read_timeout: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 5 <= hold_ms <= 500:
            raise ValueError("hold_ms must be 5-500")
        self._qmp = qmp
        self._shot_dir_qemu = shot_dir_qemu.rstrip("/")
        self._shot_dir_local = Path(shot_dir_local)
        self._shot_dir_local.mkdir(parents=True, exist_ok=True)
        self._hold_ms = hold_ms
        self._chunk = max(1, chunk)
        self._settle = settle
        self._read_timeout = read_timeout
        self._sleep = sleep
        self._clock = clock
        self._counter = itertools.count(1)
        self._tag = uuid.uuid4().hex[:8]
        self._png_supported = True
        self.closed = False

    # ------------------------------------------------------------------ Console protocol

    def screenshot(self) -> bytes:
        self._check_open()
        number = next(self._counter)
        if self._png_supported:
            name = f"shot-{self._tag}-{number}.png"
            try:
                self._qmp.execute("screendump", {"filename": f"{self._shot_dir_qemu}/{name}", "format": "png"})
                return self._take(name, png=True)
            except QMPError as exc:
                if "format" not in exc.desc.lower() and "png" not in exc.desc.lower():
                    raise ConsoleError(f"screendump failed: {exc.desc}") from exc
                self._png_supported = False  # QEMU built without PNG: fall back to PPM
        name = f"shot-{self._tag}-{number}.ppm"
        try:
            self._qmp.execute("screendump", {"filename": f"{self._shot_dir_qemu}/{name}"})
        except QMPError as exc:
            raise ConsoleError(f"screendump failed: {exc.desc}") from exc
        return self._take(name, png=False)

    def type_text(self, text: str) -> None:
        self._check_open()
        if not isinstance(text, str):
            raise TypeError("type_text takes a string")
        chords = text_to_chords(text)  # validates everything before the first key goes out
        for start in range(0, len(chords), self._chunk):
            batch = chords[start : start + self._chunk]
            for chord in batch:
                self._send_chord(chord)
            self._sleep(self._play_time(batch))
        if chords:
            self._sleep(self._settle)

    def press(self, keys: list[str]) -> None:
        self._check_open()
        chord = normalize_chord(keys)
        self._send_chord(chord)
        self._sleep(self._play_time([chord]) + self._settle)

    def power_cycle(self) -> None:
        """Hard reset, like a provider's "reset" button. A guest that powered itself off is
        started again."""
        self._check_open()
        status = self._qmp.execute("query-status") or {}
        self._qmp.execute("system_reset")
        if not status.get("running", True):
            self._qmp.execute("cont")

    def close(self) -> None:
        """End this console session. The guest keeps running."""
        if not self.closed:
            self.closed = True
            self._qmp.close()

    # ------------------------------------------------------------------ internals

    def _check_open(self) -> None:
        if self.closed:
            raise ConsoleClosed("console is closed")

    def _send_chord(self, chord: list[str]) -> None:
        keys = [{"type": "qcode", "data": key} for key in chord]
        try:
            self._qmp.execute("send-key", {"keys": keys, "hold-time": self._hold_ms})
        except QMPError as exc:
            raise ConsoleError(f"send-key {chord} failed: {exc.desc}") from exc

    def _play_time(self, chords: Sequence[Sequence[str]]) -> float:
        # QEMU waits hold_ms after every press and every release.
        return sum(2 * len(chord) for chord in chords) * self._hold_ms / 1000.0

    def _take(self, name: str, *, png: bool) -> bytes:
        path = self._shot_dir_local / name
        deadline = self._clock() + self._read_timeout
        data = b""
        while True:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                data = b""
            complete = data.startswith(PNG_SIGNATURE) and data.endswith(b"IEND\xaeB`\x82") if png else bool(data)
            if complete:
                break
            if self._clock() >= deadline:
                raise ConsoleError(f"screendump file {name} did not appear")
            self._sleep(0.05)
        try:
            path.unlink()
        except OSError:
            pass
        return data if png else _ppm_to_png(data)


def _ppm_to_png(data: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="PNG")
        return out.getvalue()


# ---------------------------------------------------------------------------------------------
# VM lifecycle
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VMEndpoints:
    """Host loopback ports of a running guest."""

    host: str
    qmp_console: int
    qmp_control: int
    ssh: int
    http: int


class QemuVM:
    """One guest in a runner container.

    ``run_dir`` (under the runner's VM home) receives the UEFI variable store, the serial log and
    the console's screenshots. ``disk`` and ``extra_drives`` are host paths under the VM home.

    Use ``start()``, then ``console()`` for a ``QemuConsole``, and ``shutdown()`` (clean: ACPI
    power button, then quit) or ``kill()`` (immediate). ``run_to_poweroff()`` is for prepare boots
    that end by powering the guest off.
    """

    def __init__(
        self,
        runner: DockerRunner,
        *,
        name: str,
        run_dir: Path,
        disk: Path,
        extra_drives: Sequence[Path] = (),
        memory_mb: int = 1024,
        cpus: int | None = None,
        accel: str = "auto",
        stay_on_poweroff: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", name):
            raise ValueError("VM name must be 1-63 characters: letters, digits, _ . -")
        if accel not in ("auto", "tcg", "kvm"):
            raise ValueError("accel must be auto, tcg or kvm")
        self.runner = runner
        self.name = name
        self.run_dir = Path(run_dir)
        self.disk = Path(disk)
        self.extra_drives = [Path(p) for p in extra_drives]
        self.memory_mb = memory_mb
        self._cpus = cpus
        self._accel = accel
        self.stay_on_poweroff = stay_on_poweroff
        self._sleep = sleep
        self._clock = clock
        self.endpoints: VMEndpoints | None = None
        self.accel: str | None = None
        self._control: QMPClient | None = None

    # ------------------------------------------------------------------ paths

    @property
    def serial_log(self) -> Path:
        return self.run_dir / "serial.log"

    @property
    def vars_file(self) -> Path:
        return self.run_dir / "efivars.fd"

    @property
    def shot_dir(self) -> Path:
        return self.run_dir / "shots"

    def spec(self) -> GuestSpec:
        accel = self.accel or ("kvm" if self._accel == "kvm" else "tcg")
        cpus = self._cpus or (2 if accel == "kvm" else 4)
        to_qemu = self.runner.container_path
        return GuestSpec(
            disk=to_qemu(self.disk),
            vars=to_qemu(self.vars_file),
            extra_drives=tuple(to_qemu(p) for p in self.extra_drives),
            serial_log=to_qemu(self.serial_log),
            name=self.name,
            memory_mb=self.memory_mb,
            cpus=cpus,
            accel=accel,
            stay_on_poweroff=self.stay_on_poweroff,
        )

    def container_argv(self) -> list[str]:
        """The container command: the relay entry point, then QEMU."""
        return [
            ENTRYPOINT,
            "--relay",
            f"{SSH_FORWARD_PORT}:{SSH_LOCAL_PORT}",
            "--relay",
            f"{HTTP_FORWARD_PORT}:{HTTP_LOCAL_PORT}",
            "--",
            *qemu_argv(self.spec()),
        ]

    # ------------------------------------------------------------------ lifecycle

    def start(self, *, wait: float = 60.0) -> VMEndpoints:
        """Start the container and wait until QEMU answers on its control socket."""
        if self.endpoints is not None:
            raise ConsoleError(f"{self.name} is already started")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        if self.serial_log.exists():
            self.serial_log.unlink()
        if not self.vars_file.exists():
            self.runner.tool(["cp", FIRMWARE_VARS, self.runner.container_path(self.vars_file)])
        if self._accel == "auto":
            self.accel = "kvm" if self.runner.kvm_available() else "tcg"
        else:
            self.accel = self._accel
        self.runner.remove(self.name)  # a container left over from a crashed run
        ports = [QMP_CONSOLE_PORT, QMP_CONTROL_PORT, SSH_FORWARD_PORT, HTTP_FORWARD_PORT]
        self.runner.start(
            self.name,
            self.container_argv(),
            publish=ports,
            kvm=self.accel == "kvm",
            writable=[self.run_dir, self.disk.parent],
            memory_mb=self.memory_mb,
        )
        try:
            self.endpoints = VMEndpoints(
                host=LOOPBACK,
                qmp_console=self.runner.port(self.name, QMP_CONSOLE_PORT),
                qmp_control=self.runner.port(self.name, QMP_CONTROL_PORT),
                ssh=self.runner.port(self.name, SSH_FORWARD_PORT),
                http=self.runner.port(self.name, HTTP_FORWARD_PORT),
            )
            self._control = QMPClient.connect(
                LOOPBACK, self.endpoints.qmp_control, wait=wait, clock=self._clock, sleep=self._sleep
            )
        except (DockerError, QMPConnectionError) as exc:
            logs = self.runner.logs(self.name)
            self.runner.remove(self.name)
            self.endpoints = None
            raise ConsoleError(f"QEMU did not start: {exc}\n{logs}") from exc
        return self.endpoints

    def attach(self, *, wait: float = 10.0) -> VMEndpoints:
        """Reconnect to a guest that is already running in its container (for example after the
        process that started it restarted)."""
        if not self.runner.running(self.name):
            raise ConsoleError(f"{self.name} is not running")
        self.endpoints = VMEndpoints(
            host=LOOPBACK,
            qmp_console=self.runner.port(self.name, QMP_CONSOLE_PORT),
            qmp_control=self.runner.port(self.name, QMP_CONTROL_PORT),
            ssh=self.runner.port(self.name, SSH_FORWARD_PORT),
            http=self.runner.port(self.name, HTTP_FORWARD_PORT),
        )
        self._control = QMPClient.connect(
            LOOPBACK, self.endpoints.qmp_control, wait=wait, clock=self._clock, sleep=self._sleep
        )
        return self.endpoints

    def console(self, **options: Any) -> QemuConsole:
        """A new console session on the console QMP socket (one session at a time)."""
        if self.endpoints is None:
            raise ConsoleError(f"{self.name} is not started")
        qmp = QMPClient.connect(LOOPBACK, self.endpoints.qmp_console, wait=30.0, clock=self._clock, sleep=self._sleep)
        return QemuConsole(
            qmp,
            shot_dir_qemu=self.runner.container_path(self.shot_dir),
            shot_dir_local=self.shot_dir,
            **options,
        )

    def status(self) -> str:
        """QEMU's run state: running, shutdown (guest powered off), paused, ..."""
        return str((self._require_control().execute("query-status") or {}).get("status", "unknown"))

    def shutdown(self, timeout: float = 120.0) -> bool:
        """Clean shutdown: press the ACPI power button, wait for the guest to power off, then
        quit QEMU and remove the container. Returns True if the guest powered off by itself
        within ``timeout``; False if QEMU had to be stopped without that."""
        clean = False
        control = self._control
        if control is not None and not control.closed:
            try:
                control.execute("system_powerdown")
                deadline = self._clock() + timeout
                while True:
                    status = str((control.execute("query-status") or {}).get("status"))
                    # Events that arrived with that reply are buffered; timeout=0 only looks there.
                    if status == "shutdown" or control.wait_event("SHUTDOWN", timeout=0) is not None:
                        clean = True
                        break
                    if self._clock() >= deadline:
                        break
                    self._sleep(1.0)
                try:
                    control.execute("quit")
                except (QMPConnectionError, QMPError):
                    pass  # QEMU closes the socket as it quits, sometimes before it replies
            except (QMPConnectionError, QMPError):
                # The control socket dropped: QEMU exited, which it does when the guest powers off
                # and stay_on_poweroff is False.
                clean = clean or self._wait_exit(15.0) == 0
        self._wait_exit(15.0)
        self.kill()
        return clean

    def kill(self) -> None:
        """Stop at once: remove the container."""
        if self._control is not None:
            self._control.close()
            self._control = None
        self.runner.remove(self.name)
        self.endpoints = None

    def run_to_poweroff(self, timeout: float) -> str:
        """Start a guest that powers itself off when done (a prepare boot), wait for that, and
        return the serial log. Raises ``ConsoleError`` on timeout or a QEMU failure."""
        if self.stay_on_poweroff:
            raise ConsoleError("run_to_poweroff needs stay_on_poweroff=False")
        self.start()
        try:
            code = self._wait_exit(timeout)
            if code is None:
                raise ConsoleError(
                    f"{self.name} did not power off within {timeout:.0f}s; serial log tail:\n"
                    + self.serial_text()[-2000:]
                )
            if code != 0:
                raise ConsoleError(f"QEMU exited with {code}: {self.runner.logs(self.name)}")
            return self.serial_text()
        finally:
            self.kill()

    def __enter__(self) -> QemuVM:
        return self

    def __exit__(self, *exc: object) -> None:
        self.kill()

    # ------------------------------------------------------------------ serial log

    def serial_text(self) -> str:
        try:
            return self.serial_log.read_bytes().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def wait_for_serial(
        self, pattern: str | re.Pattern[str], timeout: float, *, start: int = 0
    ) -> re.Match[str] | None:
        """Wait until the serial log (from character ``start`` on) matches ``pattern``."""
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = self._clock() + timeout
        while True:
            match = regex.search(self.serial_text(), start)
            if match:
                return match
            if self._clock() >= deadline:
                return None
            if self.endpoints is not None and not self.runner.running(self.name):
                return regex.search(self.serial_text(), start)
            self._sleep(1.0)

    # ------------------------------------------------------------------ internals

    def _require_control(self) -> QMPClient:
        if self._control is None or self._control.closed:
            raise ConsoleError(f"{self.name} has no control connection")
        return self._control

    def _wait_exit(self, timeout: float) -> int | None:
        deadline = self._clock() + timeout
        while True:
            code = self.runner.exit_code(self.name)
            if code is not None:
                return code
            if self._clock() >= deadline:
                return None
            self._sleep(1.0)
