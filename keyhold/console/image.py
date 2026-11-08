"""Guest images: download the Debian 13 arm64 genericcloud qcow2 and verify its SHA512, build the
cloud-init NoCloud seed, and create copy-on-write overlays so a fault can be reset instantly.

Disk chain (all qcow2, each file backed by the one before it through a relative path):

    base/debian-13-genericcloud-arm64-<build>.qcow2   the official image, read-only
      golden/golden.qcow2        one cloud-init boot: demo page, probe key, console password
        faults/<fault>.qcow2     one prepare boot that plants the fault (keyhold.faults.plant)
          runs/<run>/disk.qcow2  what a rescue run boots and the agent types into

A run overlay is created in milliseconds and thrown away afterwards, so every run starts from the
same broken server.

Secrets stay under the VM home (git-ignored ``var/vm`` by default) and are never printed: the
console password (``golden/console-password``, mode 600) and the probe private key
(``keys/probe_ed25519``, mode 600). Only the probe runner reads the private key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any

from keyhold.console.base import ConsoleError
from keyhold.console.docker import LOOPBACK, DockerRunner
from keyhold.console.qemu import GUEST_MAC, QemuVM, VMEndpoints
from keyhold.types import ProbeSpec, sha256_hex

__all__ = [
    "ImageError",
    "BaseImage",
    "DEBIAN_13_ARM64",
    "VMHome",
    "default_vm_home",
    "sha512_file",
    "parse_sha512sums",
    "fetch_base_image",
    "verify_base_image",
    "GoldenConfig",
    "GoldenInfo",
    "PAGE_MARKER",
    "GOLDEN_MARKER",
    "page_html",
    "render_user_data",
    "render_meta_data",
    "render_network_config",
    "write_seed_dir",
    "build_iso",
    "create_overlay",
    "ensure_probe_key",
    "ensure_console_password",
    "read_console_password",
    "build_golden",
    "build_lock",
    "load_golden_info",
    "new_run",
    "new_run_vm",
    "remove_run",
    "probe_spec_for",
]


class ImageError(ConsoleError):
    """A guest image could not be downloaded, verified or built."""


# ---------------------------------------------------------------------------------------------
# Base image
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseImage:
    """An official cloud image pinned by build date and SHA512."""

    filename: str
    url: str
    sha512: str
    sums_url: str
    size: int | None = None


DEBIAN_BUILD = "20261103-2651"
_DEBIAN_DIR = f"https://cloud.debian.org/images/cloud/trixie/{DEBIAN_BUILD}"
DEBIAN_13_ARM64 = BaseImage(
    filename=f"debian-13-genericcloud-arm64-{DEBIAN_BUILD}.qcow2",
    url=f"{_DEBIAN_DIR}/debian-13-genericcloud-arm64-{DEBIAN_BUILD}.qcow2",
    sha512=(
        "5d3ecec46fd29605db4084388a1e2eb82fd19a80202898f962840ea73793261d"
        "e721e505e3877c829ae86c37fdd2f6c205b0b3b1337a89d0d4b98d9081adc0c2"
    ),
    sums_url=f"{_DEBIAN_DIR}/SHA512SUMS",
    size=337_641_472,
)


def default_vm_home() -> Path:
    """``$KEYHOLD_VM_HOME`` if set, else ``var/vm`` in the project checkout."""
    env = os.environ.get("KEYHOLD_VM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "var" / "vm"


class VMHome:
    """File layout under the VM home directory."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).resolve() if root is not None else default_vm_home()

    @property
    def base_dir(self) -> Path:
        return self.root / "base"

    @property
    def golden_dir(self) -> Path:
        return self.root / "golden"

    @property
    def faults_dir(self) -> Path:
        return self.root / "faults"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def keys_dir(self) -> Path:
        return self.root / "keys"

    def base_image(self, image: BaseImage = DEBIAN_13_ARM64) -> Path:
        return self.base_dir / image.filename

    @property
    def golden_disk(self) -> Path:
        return self.golden_dir / "golden.qcow2"

    @property
    def golden_info_file(self) -> Path:
        return self.golden_dir / "golden.json"

    @property
    def console_password_file(self) -> Path:
        return self.golden_dir / "console-password"

    @property
    def probe_key(self) -> Path:
        return self.keys_dir / "probe_ed25519"

    @property
    def probe_public_key_file(self) -> Path:
        return self.keys_dir / "probe_ed25519.pub"

    def fault_disk(self, fault_id: str) -> Path:
        _check_id(fault_id, "fault id")
        return self.faults_dir / f"{fault_id}.qcow2"

    def run_dir(self, run_id: str) -> Path:
        _check_id(run_id, "run id")
        return self.runs_dir / run_id

    def ensure(self) -> VMHome:
        for directory in (self.base_dir, self.golden_dir, self.faults_dir, self.runs_dir, self.keys_dir):
            directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.keys_dir, 0o700)
        return self


@contextmanager
def build_lock(home: VMHome) -> Iterator[None]:
    """Hold an exclusive lock on the VM home while building images, so two processes never
    build the golden image or the same fault image at once. (No-op where flock is missing.)"""
    home.root.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX
        yield
        return
    with open(home.root / ".build.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")


def _check_id(value: str, what: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{what} must be 1-48 characters: lower-case letters, digits, '-' and '_'")


def sha512_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha512()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def parse_sha512sums(text: str) -> dict[str, str]:
    """Parse a ``SHA512SUMS`` file (``<hex>  <name>`` per line) into ``{name: hex}``."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{128}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


Opener = Callable[[str], AbstractContextManager[IO[bytes]]]


def _default_opener(url: str) -> AbstractContextManager[IO[bytes]]:
    if not url.startswith("https://"):
        raise ImageError(f"refusing to download over plain HTTP: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "keyhold-vm-tools"})
    return urllib.request.urlopen(request, timeout=60)  # noqa: S310 - https only, checked above


def verify_base_image(path: Path, image: BaseImage = DEBIAN_13_ARM64) -> bool:
    """True if ``path`` exists and its SHA512 equals the pinned value."""
    return path.is_file() and sha512_file(path) == image.sha512


def fetch_base_image(
    home: VMHome,
    image: BaseImage = DEBIAN_13_ARM64,
    *,
    opener: Opener | None = None,
    check_published_sums: bool = True,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download ``image`` into the VM home unless a verified copy is already there.

    The download goes to ``<name>.part`` and is renamed only after its SHA512 matches the pinned
    value. With ``check_published_sums`` the pinned value must also appear in the build's
    published SHA512SUMS, so a typo in the pin is caught before 300 MB are downloaded.
    """
    open_url = opener or _default_opener
    home.ensure()
    dest = home.base_image(image)
    if dest.exists():
        if verify_base_image(dest, image):
            return dest
        bad = dest.with_name(dest.name + ".bad")
        os.replace(dest, bad)

    if check_published_sums:
        with open_url(image.sums_url) as response:
            published = parse_sha512sums(response.read().decode("utf-8", errors="replace"))
        if published.get(image.filename) != image.sha512:
            raise ImageError(f"the published SHA512SUMS do not list {image.filename} with the pinned hash")

    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha512()
    received = 0
    with open_url(image.url) as response, open(part, "wb") as out:
        while block := response.read(1 << 20):
            digest.update(block)
            out.write(block)
            received += len(block)
            if progress is not None:
                progress(received, image.size)
    if digest.hexdigest() != image.sha512:
        part.unlink(missing_ok=True)
        raise ImageError(f"SHA512 mismatch for {image.filename}; the download was discarded")
    os.replace(part, dest)
    os.chmod(dest, 0o444)
    return dest


# ---------------------------------------------------------------------------------------------
# cloud-init seed for the golden image
# ---------------------------------------------------------------------------------------------

PAGE_MARKER = "keyhold-demo: the page is being served."
GOLDEN_MARKER = "KEYHOLD-GOLDEN-OK"
CONSOLE_USER = "root"
PLANT_LABEL = "KHPLANT"


@dataclass(frozen=True)
class GoldenConfig:
    """What the golden image's cloud-init boot sets up."""

    probe_public_key: str
    console_password: str
    page_token: str
    hostname: str = "keyhold-demo"
    probe_user: str = "probe"
    instance_id: str = "keyhold-golden"

    def __post_init__(self) -> None:
        key = self.probe_public_key.strip()
        if not re.fullmatch(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+( [^\n]*)?", key):
            raise ValueError("probe_public_key must be one OpenSSH public key line")
        if len(self.console_password) < 12 or any(c in self.console_password for c in "\n\r\\\"'"):
            raise ValueError("console_password must be 12+ characters without quotes or newlines")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", self.hostname):
            raise ValueError("hostname must be a lower-case DNS label")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.probe_user) or self.probe_user == "root":
            raise ValueError("probe_user must be a normal user name")
        if not re.fullmatch(r"[0-9a-f]{8,64}", self.page_token):
            raise ValueError("page_token must be hex")


def page_html(token: str) -> str:
    """The demo page. ``token`` makes each golden build's page (and its sha256) unique."""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"><title>keyhold-demo</title></head>\n'
        "<body>\n"
        f"<h1>{PAGE_MARKER}</h1>\n"
        f"<p>Build token: {token}</p>\n"
        "</body></html>\n"
    )


# Serves one HTTP request on stdin/stdout. systemd starts one copy per connection (Accept=yes),
# so the page needs no web server package.
_PAGE_RESPONDER = r"""#!/bin/bash
# Answer one HTTP request with the demo page (systemd socket activation, one process per connection).
page=/srv/keyhold-demo/index.html
IFS= read -r -t 10 request || exit 0
while IFS= read -r -t 10 header; do
  header=${header%$'\r'}
  [ -z "$header" ] && break
done
method=${request%% *}
if [ ! -r "$page" ]; then
  printf 'HTTP/1.0 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
  exit 0
fi
printf 'HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %s\r\nConnection: close\r\n\r\n' "$(stat -c %s "$page")"
[ "$method" = HEAD ] || cat "$page"
"""

_PAGE_SOCKET = """[Unit]
Description=Demo web page on port 80

[Socket]
ListenStream=80
Accept=yes

[Install]
WantedBy=sockets.target
"""

_PAGE_SERVICE = """[Unit]
Description=Demo web page request

[Service]
ExecStart=/usr/local/sbin/demo-page
StandardInput=socket
StandardOutput=socket
StandardError=journal
User=nobody
Group=nogroup
"""

# Keeps the boot messages on tty1 above the login prompt, so the screen shows what failed.
_GETTY_NOCLEAR = """[Service]
TTYVTDisallocate=no
"""

# The last console= is /dev/console, so systemd's boot status goes to the screen. loglevel=4 keeps
# routine kernel messages (audit notices, device probes) off the screen, where they would scroll
# away prompts such as the emergency-mode password prompt; the journal copy on serial keeps them.
_GRUB_DEFAULTS = """# Boot status on the screen (tty0) and kernel warnings on both consoles; GRUB on the firmware console.
GRUB_CMDLINE_LINUX="console=ttyAMA0,115200 console=tty0 consoleblank=0 loglevel=4"
GRUB_TERMINAL=console
"""

# Copies journal messages to the serial port: the operator's log of what the guest did.
_JOURNALD_SERIAL = """[Journal]
ForwardToConsole=yes
TTYPath=/dev/ttyAMA0
MaxLevelConsole=info
"""

# The fault-planting hook: runs only when a disk labelled KHPLANT is attached (a prepare boot),
# runs its plant.sh, removes itself, clears the journal of the planting and powers off.
_PLANT_HOOK = f"""#!/bin/bash
set -u
udevadm settle --timeout=30 || true
dev=$(blkid -L {PLANT_LABEL} 2>/dev/null) || exit 0
mnt=$(mktemp -d)
mount -o ro "$dev" "$mnt" || {{ echo "KEYHOLD-PLANT-FAILED mount" > /dev/ttyAMA0; exit 1; }}
cp "$mnt/plant.sh" /run/plant.sh
fault=$(tr -cd 'a-z0-9_-' < "$mnt/fault-id")
umount "$mnt"
rmdir "$mnt"
status=FAILED
if bash -euo pipefail /run/plant.sh > /run/plant.log 2>&1; then
  status=OK
fi
sed 's/^/plant: /' /run/plant.log > /dev/ttyAMA0
rm -f /run/plant.sh /run/plant.log
systemctl disable keyhold-plant.service > /dev/null 2>&1 || true
rm -f /etc/systemd/system/keyhold-plant.service /usr/local/sbin/keyhold-plant
# Leave no record of the planting in the guest's persistent journal.
journalctl --relinquish-var > /dev/null 2>&1 || true
find /var/log/journal -type f -name '*.journal*' -delete 2>/dev/null || true
sync
echo "KEYHOLD-PLANT-$status $fault" > /dev/ttyAMA0
systemctl poweroff --no-block
"""

_PLANT_UNIT = """[Unit]
Description=Provisioning hook (does nothing without a provisioning disk)
After=local-fs.target systemd-udev-settle.service network.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/keyhold-plant

[Install]
WantedBy=multi-user.target
"""

_GOLDEN_FINISH = f"""#!/bin/bash
# Last step of the golden build: set the root console password from the seed disk (so it never
# passes through cloud-init's caches), enable the demo units, rebuild the GRUB config, switch
# cloud-init off for every later boot, and report on the serial port.
set -euo pipefail
seed=$(blkid -L cidata)
mnt=$(mktemp -d)
mount -o ro "$seed" "$mnt"
printf '{CONSOLE_USER}:%s\\n' "$(cat "$mnt/console-password")" | chpasswd
umount "$mnt"
rmdir "$mnt"
systemctl daemon-reload
systemctl enable demo-page.socket keyhold-plant.service
for unit in apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer fstrim.timer; do
  systemctl disable "$unit" > /dev/null 2>&1 || true
done
update-grub
touch /etc/cloud/cloud-init.disabled
rm -f /usr/local/sbin/golden-finish
sync
echo "{GOLDEN_MARKER}" > /dev/ttyAMA0
"""


def render_user_data(cfg: GoldenConfig) -> str:
    """cloud-config for the golden boot (JSON is valid YAML, so no YAML library is needed)."""
    files = [
        {"path": "/srv/keyhold-demo/index.html", "permissions": "0644", "content": page_html(cfg.page_token)},
        {"path": "/usr/local/sbin/demo-page", "permissions": "0755", "content": _PAGE_RESPONDER},
        {"path": "/etc/systemd/system/demo-page.socket", "permissions": "0644", "content": _PAGE_SOCKET},
        {"path": "/etc/systemd/system/demo-page@.service", "permissions": "0644", "content": _PAGE_SERVICE},
        {
            "path": "/etc/systemd/system/getty@tty1.service.d/keep-boot-messages.conf",
            "permissions": "0644",
            "content": _GETTY_NOCLEAR,
        },
        {"path": "/etc/default/grub.d/90-console.cfg", "permissions": "0644", "content": _GRUB_DEFAULTS},
        {"path": "/etc/systemd/journald.conf.d/serial.conf", "permissions": "0644", "content": _JOURNALD_SERIAL},
        {"path": "/usr/local/sbin/keyhold-plant", "permissions": "0755", "content": _PLANT_HOOK},
        {"path": "/etc/systemd/system/keyhold-plant.service", "permissions": "0644", "content": _PLANT_UNIT},
        {"path": "/usr/local/sbin/golden-finish", "permissions": "0755", "content": _GOLDEN_FINISH},
    ]
    config: dict[str, Any] = {
        "hostname": cfg.hostname,
        "preserve_hostname": False,
        "users": [
            {
                "name": cfg.probe_user,
                "gecos": "Owner probe (SSH key login only)",
                "shell": "/bin/bash",
                "lock_passwd": True,
                "ssh_authorized_keys": [cfg.probe_public_key.strip()],
            }
        ],
        "ssh_pwauth": False,
        "disable_root": True,
        "write_files": files,
        "runcmd": [["/usr/local/sbin/golden-finish"]],
        "power_state": {"mode": "poweroff", "delay": "now", "timeout": 300, "condition": True},
    }
    return "#cloud-config\n" + json.dumps(config, indent=2) + "\n"


def render_meta_data(cfg: GoldenConfig) -> str:
    return f"instance-id: {cfg.instance_id}\nlocal-hostname: {cfg.hostname}\n"


def render_network_config() -> str:
    """One NIC, named eth0 by its MAC address, configured by DHCP."""
    config = {
        "version": 2,
        "ethernets": {
            "eth0": {"match": {"macaddress": GUEST_MAC}, "set-name": "eth0", "dhcp4": True},
        },
    }
    return json.dumps(config, indent=2) + "\n"


def write_seed_dir(directory: Path, cfg: GoldenConfig) -> list[Path]:
    """Write the seed files, mode 600. ``console-password`` is not a cloud-init file: the golden
    build's last step reads it from the seed disk and sets the root password itself, so the
    password never lands in cloud-init's copies of user-data inside the guest."""
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "user-data": render_user_data(cfg),
        "meta-data": render_meta_data(cfg),
        "network-config": render_network_config(),
        "console-password": cfg.console_password + "\n",
    }
    paths = []
    for name, text in files.items():
        path = directory / name
        _write_private(path, text)
        paths.append(path)
    return paths


def build_iso(runner: DockerRunner, out: Path, files: Iterable[Path], *, label: str, private: bool = False) -> Path:
    """Build an ISO 9660 image (Joliet + Rock Ridge) holding ``files`` with volume ``label``.
    ``private``: make the image mode 600 (the cloud-init seed holds the console password)."""
    paths = list(files)
    if not paths:
        raise ImageError("an ISO needs at least one file")
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = ["genisoimage", "-quiet", "-output", runner.container_path(out), "-volid", label, "-joliet", "-rock"]
    argv += [runner.container_path(p) for p in paths]
    runner.tool(argv)
    if not out.is_file():
        raise ImageError(f"genisoimage did not write {out}")
    if private:
        os.chmod(out, 0o600)
    return out


def create_overlay(runner: DockerRunner, backing: Path, overlay: Path) -> Path:
    """Create a qcow2 overlay of ``backing``. The backing name is stored as a relative path so the
    chain opens both on the host and inside a runner container."""
    backing = Path(backing).resolve()
    overlay = Path(overlay).resolve()
    if not backing.is_file():
        raise ImageError(f"backing image {backing} does not exist")
    overlay.parent.mkdir(parents=True, exist_ok=True)
    relative = os.path.relpath(backing, overlay.parent)
    runner.tool(
        ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", relative, runner.container_path(overlay)]
    )
    if not overlay.is_file():
        raise ImageError(f"qemu-img did not create {overlay}")
    return overlay


# ---------------------------------------------------------------------------------------------
# Secrets: probe key and console password
# ---------------------------------------------------------------------------------------------


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def ensure_probe_key(home: VMHome, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Create the owner probe's ed25519 key pair if missing; return the public key line.

    The private key is written with mode 600 and never read by this module.
    """
    home.ensure()
    if not home.probe_key.exists():
        proc = run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "keyhold-owner-probe", "-f", str(home.probe_key)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise ImageError(f"ssh-keygen failed: {proc.stderr.strip()}")
        os.chmod(home.probe_key, 0o600)
    return home.probe_public_key_file.read_text().strip()


def ensure_console_password(home: VMHome) -> str:
    """Create the root console password for the golden image if missing and return it."""
    home.ensure()
    if not home.console_password_file.exists():
        _write_private(home.console_password_file, secrets.token_urlsafe(18) + "\n")
    return read_console_password(home)


def read_console_password(home: VMHome) -> str:
    return home.console_password_file.read_text().strip()


# ---------------------------------------------------------------------------------------------
# Golden image
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenInfo:
    """Facts about a built golden image, saved as golden/golden.json (no secrets)."""

    base_image: str
    base_sha512: str
    hostname: str
    probe_user: str
    probe_public_key: str
    console_user: str
    page_sha256: str
    page_marker: str
    built_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def load_golden_info(home: VMHome) -> GoldenInfo:
    try:
        data = json.loads(home.golden_info_file.read_text())
    except FileNotFoundError:
        raise ImageError("no golden image yet; run tools/vm/vm.py golden") from None
    return GoldenInfo(**data)


def build_golden(
    home: VMHome,
    runner: DockerRunner,
    *,
    image: BaseImage = DEBIAN_13_ARM64,
    timeout: float = 1500.0,
    force: bool = False,
    keygen: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    vm_factory: Callable[..., QemuVM] = QemuVM,
) -> GoldenInfo:
    """Boot the base image once with a cloud-init seed and keep the result as golden.qcow2.

    The seed installs the demo page, the probe user and key, the root console password, the
    screen and serial console settings and the fault-planting hook, then switches cloud-init off
    and powers the guest down. Rebuilding (``force``) deletes the fault images built on the old
    golden image, because their backing file changes.
    """
    with build_lock(home):
        return _build_golden_locked(
            home, runner, image=image, timeout=timeout, force=force, keygen=keygen, vm_factory=vm_factory
        )


def _build_golden_locked(
    home: VMHome,
    runner: DockerRunner,
    *,
    image: BaseImage,
    timeout: float,
    force: bool,
    keygen: Callable[..., subprocess.CompletedProcess],
    vm_factory: Callable[..., QemuVM],
) -> GoldenInfo:
    home.ensure()
    base = home.base_image(image)
    if not verify_base_image(base, image):
        raise ImageError(f"{base.name} is missing or does not match its SHA512; run tools/vm/vm.py fetch")
    if home.golden_disk.exists():
        if not force:
            return load_golden_info(home)
        for disk in home.faults_dir.glob("*.qcow2"):
            _force_unlink(disk)
        _force_unlink(home.golden_disk)

    cfg = GoldenConfig(
        probe_public_key=ensure_probe_key(home, run=keygen),
        console_password=ensure_console_password(home),
        page_token=secrets.token_hex(8),
    )
    work = home.golden_dir / "build"
    seed_files = write_seed_dir(work / "seed", cfg)
    seed_iso = build_iso(runner, work / "seed.iso", seed_files, label="cidata", private=True)
    building = create_overlay(runner, base, home.golden_dir / "golden.building.qcow2")
    vm = vm_factory(
        runner,
        name="keyhold-golden-build",
        run_dir=work / "boot",
        disk=building,
        extra_drives=[seed_iso],
        stay_on_poweroff=False,
    )
    try:
        serial = vm.run_to_poweroff(timeout)
        if GOLDEN_MARKER not in serial:
            raise ImageError("the golden boot powered off without finishing; serial log tail:\n" + serial[-3000:])
        os.replace(building, home.golden_disk)
        os.chmod(home.golden_disk, 0o444)
    finally:
        building.unlink(missing_ok=True)
        _force_unlink(seed_iso)  # the seed holds the console password
        shutil.rmtree(work / "seed", ignore_errors=True)
    # Only on success: after a failure the serial log in build/boot is kept for inspection.
    shutil.rmtree(work, ignore_errors=True)

    info = GoldenInfo(
        base_image=image.filename,
        base_sha512=image.sha512,
        hostname=cfg.hostname,
        probe_user=cfg.probe_user,
        probe_public_key=cfg.probe_public_key.strip(),
        console_user=CONSOLE_USER,
        page_sha256=sha256_hex(page_html(cfg.page_token)),
        page_marker=PAGE_MARKER,
        built_at=time.time(),
    )
    home.golden_info_file.write_text(info.to_json())
    return info


def _force_unlink(path: Path) -> None:
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------------------------


def new_run(home: VMHome, runner: DockerRunner, fault_id: str | None, run_id: str | None = None) -> tuple[str, Path]:
    """Create ``runs/<run_id>/disk.qcow2`` over the fault image (or the golden image when
    ``fault_id`` is None). Returns ``(run_id, disk)``."""
    run_id = run_id or uuid.uuid4().hex[:12]
    backing = home.golden_disk if fault_id is None else home.fault_disk(fault_id)
    if not backing.is_file():
        what = "golden image" if fault_id is None else f"fault image {fault_id}"
        raise ImageError(f"the {what} is not built yet; see tools/vm/README.md")
    run_dir = home.run_dir(run_id)
    if run_dir.exists():
        raise ImageError(f"run {run_id} already exists")
    run_dir.mkdir(parents=True)
    disk = create_overlay(runner, backing, run_dir / "disk.qcow2")
    return run_id, disk


def new_run_vm(
    home: VMHome,
    runner: DockerRunner,
    fault_id: str | None,
    run_id: str | None = None,
    **vm_options: Any,
) -> QemuVM:
    """A fresh run overlay and an unstarted ``QemuVM`` for it (container ``keyhold-run-<id>``)."""
    run_id, disk = new_run(home, runner, fault_id, run_id)
    return QemuVM(runner, name=f"keyhold-run-{run_id}", run_dir=home.run_dir(run_id), disk=disk, **vm_options)


def remove_run(home: VMHome, run_id: str) -> None:
    """Delete a run directory (overlay, serial log, screenshots)."""
    shutil.rmtree(home.run_dir(run_id), ignore_errors=True)


def probe_spec_for(info: GoldenInfo, endpoints: VMEndpoints) -> ProbeSpec:
    """The owner probe for a run of the golden image: SSH as the probe user on the forwarded SSH
    port, and the demo page on the forwarded HTTP port with its exact sha256."""
    return ProbeSpec(
        host=endpoints.host or LOOPBACK,
        ssh_port=endpoints.ssh,
        ssh_user=info.probe_user,
        probe_public_key=info.probe_public_key,
        url=f"http://{endpoints.host or LOOPBACK}:{endpoints.http}/",
        expect_sha256=info.page_sha256,
    )
