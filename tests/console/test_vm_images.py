"""Base image download and verification, the cloud-init seed, overlays and the golden build.

Everything runs against fakes: a fake URL opener, a fake docker CLI and a fake VM.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from keyhold.console.image import (
    DEBIAN_13_ARM64,
    GOLDEN_MARKER,
    PAGE_MARKER,
    BaseImage,
    GoldenConfig,
    ImageError,
    VMHome,
    _default_opener,
    build_golden,
    build_iso,
    create_overlay,
    ensure_console_password,
    ensure_probe_key,
    fetch_base_image,
    load_golden_info,
    new_run,
    new_run_vm,
    page_html,
    parse_sha512sums,
    probe_spec_for,
    read_console_password,
    remove_run,
    render_meta_data,
    render_network_config,
    render_user_data,
    sha512_file,
    verify_base_image,
    write_seed_dir,
)
from keyhold.console.qemu import GUEST_MAC, VMEndpoints
from keyhold.types import ProbeSpec

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly0000000000000000000 keyhold-owner-probe"
PASSWORD = "test-console-password-1"


def _image(payload: bytes, name: str = "test-image.qcow2") -> BaseImage:
    return BaseImage(
        filename=name,
        url=f"https://images.test/{name}",
        sha512=hashlib.sha512(payload).hexdigest(),
        sums_url="https://images.test/SHA512SUMS",
        size=len(payload),
    )


class FakeOpener:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.opened: list[str] = []

    @contextmanager
    def __call__(self, url: str):
        self.opened.append(url)
        yield io.BytesIO(self.files[url])


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------------------------- base image


def test_pinned_debian_image() -> None:
    assert DEBIAN_13_ARM64.filename.startswith("debian-13-genericcloud-arm64-")
    assert DEBIAN_13_ARM64.url.startswith("https://") and DEBIAN_13_ARM64.url.endswith(DEBIAN_13_ARM64.filename)
    assert len(DEBIAN_13_ARM64.sha512) == 128 and int(DEBIAN_13_ARM64.sha512, 16)


def test_parse_sha512sums() -> None:
    digest = "ab" * 64
    text = f"{digest}  a.qcow2\n{'cd' * 64} *b.raw\nnot a line\n{'zz' * 64}  bad.hex\n"
    assert parse_sha512sums(text) == {"a.qcow2": digest, "b.raw": "cd" * 64}


def test_fetch_downloads_verifies_and_marks_read_only(tmp_path) -> None:
    payload = os.urandom(3 * 1024 * 1024 + 17)
    image = _image(payload)
    opener = FakeOpener({image.sums_url: f"{image.sha512}  {image.filename}\n".encode(), image.url: payload})
    seen = []
    home = VMHome(tmp_path)
    path = fetch_base_image(home, image, opener=opener, progress=lambda done, total: seen.append((done, total)))
    assert path == home.base_image(image) and path.read_bytes() == payload
    assert _mode(path) == 0o444
    assert seen[-1] == (len(payload), len(payload))
    assert not path.with_name(path.name + ".part").exists()
    assert verify_base_image(path, image) and sha512_file(path) == image.sha512

    opener.opened.clear()
    assert fetch_base_image(home, image, opener=opener) == path
    assert opener.opened == []  # already there and verified: nothing downloaded


def test_fetch_rejects_a_bad_download(tmp_path) -> None:
    image = _image(b"the real image")
    opener = FakeOpener({image.sums_url: f"{image.sha512}  {image.filename}\n".encode(), image.url: b"tampered"})
    home = VMHome(tmp_path)
    with pytest.raises(ImageError, match="SHA512 mismatch"):
        fetch_base_image(home, image, opener=opener)
    assert list(home.base_dir.iterdir()) == []  # nothing kept, not even the .part file


def test_fetch_checks_the_published_sums_first(tmp_path) -> None:
    image = _image(b"payload")
    opener = FakeOpener({image.sums_url: f"{'00' * 64}  {image.filename}\n".encode(), image.url: b"payload"})
    with pytest.raises(ImageError, match="SHA512SUMS"):
        fetch_base_image(VMHome(tmp_path), image, opener=opener)
    assert opener.opened == [image.sums_url]  # the big download never started


def test_fetch_replaces_a_corrupt_local_copy(tmp_path) -> None:
    image = _image(b"good bytes")
    home = VMHome(tmp_path).ensure()
    home.base_image(image).write_bytes(b"bit rot")
    opener = FakeOpener({image.url: b"good bytes"})
    path = fetch_base_image(home, image, opener=opener, check_published_sums=False)
    assert path.read_bytes() == b"good bytes"
    assert path.with_name(path.name + ".bad").read_bytes() == b"bit rot"


def test_default_opener_refuses_plain_http() -> None:
    with pytest.raises(ImageError):
        _default_opener("http://images.test/x.qcow2")


# ------------------------------------------------------------------------------- seed


def _cfg(**overrides) -> GoldenConfig:
    values = {"probe_public_key": PUBKEY, "console_password": PASSWORD, "page_token": "0123456789abcdef"}
    values.update(overrides)
    return GoldenConfig(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"probe_public_key": "not a key"},
        {"probe_public_key": PUBKEY + "\nssh-ed25519 AAAA second"},
        {"console_password": "short"},
        {"console_password": 'has"quote-and-length'},
        {"hostname": "Bad_Host"},
        {"probe_user": "root"},
        {"page_token": "xyz"},
    ],
)
def test_golden_config_validation(overrides) -> None:
    with pytest.raises(ValueError):
        _cfg(**overrides)


def test_user_data_installs_page_probe_key_and_hook_without_the_password() -> None:
    text = render_user_data(_cfg())
    assert text.startswith("#cloud-config\n")
    config = json.loads(text.split("\n", 1)[1])
    assert PASSWORD not in text  # the password reaches the guest only through the seed disk
    assert "chpasswd" not in config and config["ssh_pwauth"] is False
    (probe,) = config["users"]
    assert probe["name"] == "probe" and probe["lock_passwd"] is True and probe["ssh_authorized_keys"] == [PUBKEY]
    files = {f["path"]: f for f in config["write_files"]}
    assert files["/srv/keyhold-demo/index.html"]["content"] == page_html("0123456789abcdef")
    assert "ListenStream=80" in files["/etc/systemd/system/demo-page.socket"]["content"]
    assert "Accept=yes" in files["/etc/systemd/system/demo-page.socket"]["content"]
    assert files["/usr/local/sbin/demo-page"]["permissions"] == "0755"
    assert "console=tty0" in files["/etc/default/grub.d/90-console.cfg"]["content"]
    assert "TTYVTDisallocate=no" in files["/etc/systemd/system/getty@tty1.service.d/keep-boot-messages.conf"]["content"]
    assert "KHPLANT" in files["/usr/local/sbin/keyhold-plant"]["content"]
    finish = files["/usr/local/sbin/golden-finish"]["content"]
    assert "chpasswd" in finish and "cloud-init.disabled" in finish and GOLDEN_MARKER in finish
    assert config["runcmd"] == [["/usr/local/sbin/golden-finish"]]
    assert config["power_state"]["mode"] == "poweroff"


def test_guest_scripts_are_valid_bash() -> None:
    config = json.loads(render_user_data(_cfg()).split("\n", 1)[1])
    for entry in config["write_files"]:
        if entry["content"].startswith("#!/bin/bash"):
            result = subprocess.run(["bash", "-n"], input=entry["content"], capture_output=True, text=True)
            assert result.returncode == 0, (entry["path"], result.stderr)


def test_page_meta_and_network() -> None:
    page = page_html("deadbeef00")
    assert PAGE_MARKER in page and "deadbeef00" in page
    assert render_meta_data(_cfg()) == "instance-id: keyhold-golden\nlocal-hostname: keyhold-demo\n"
    network = json.loads(render_network_config())
    assert network["ethernets"]["eth0"]["match"]["macaddress"] == GUEST_MAC
    assert network["ethernets"]["eth0"]["dhcp4"] is True


def test_seed_dir_is_private(tmp_path) -> None:
    paths = write_seed_dir(tmp_path / "seed", _cfg())
    assert sorted(p.name for p in paths) == ["console-password", "meta-data", "network-config", "user-data"]
    assert all(_mode(p) == 0o600 for p in paths)
    assert (tmp_path / "seed" / "console-password").read_text() == PASSWORD + "\n"


def test_build_iso_and_overlay_use_container_paths(fake_docker) -> None:
    docker, runner = fake_docker
    home = VMHome(runner.vm_home)
    files = write_seed_dir(home.golden_dir / "seed", _cfg())
    iso = build_iso(runner, home.golden_dir / "seed.iso", files, label="cidata")
    assert iso.exists()
    argv = docker.calls[-1]
    assert argv[:6] == ["docker", "run", "--rm", "--network", "none", "-v"]
    tool = argv[argv.index("genisoimage") :]
    assert tool[tool.index("-volid") + 1] == "cidata"
    assert "/vm/golden/seed/user-data" in tool and "/vm/golden/seed.iso" in tool
    with pytest.raises(ImageError):
        build_iso(runner, home.golden_dir / "empty.iso", [], label="x")
    # The seed holds the console password: the private flag makes the image mode 600.
    private = build_iso(runner, home.golden_dir / "seed-private.iso", files, label="cidata", private=True)
    assert _mode(private) == 0o600

    base = home.base_dir / "base.qcow2"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"base")
    overlay = create_overlay(runner, base, home.runs_dir / "r1" / "disk.qcow2")
    qemu_img = docker.calls[-1][docker.calls[-1].index("qemu-img") :]
    assert qemu_img[qemu_img.index("-b") + 1] == "../../base/base.qcow2"  # relative: valid on host and in container
    assert qemu_img[-1] == "/vm/runs/r1/disk.qcow2" and overlay.exists()
    with pytest.raises(ImageError):
        create_overlay(runner, home.base_dir / "missing.qcow2", home.runs_dir / "r2" / "disk.qcow2")


# ------------------------------------------------------------------------------- secrets


def _fake_keygen(argv, **kwargs):
    path = Path(argv[argv.index("-f") + 1])
    path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
    path.with_name(path.name + ".pub").write_text(PUBKEY + "\n")
    return subprocess.CompletedProcess(argv, 0, "", "")


def test_probe_key_is_created_once_and_private(tmp_path) -> None:
    home = VMHome(tmp_path)
    calls = []

    def keygen(argv, **kwargs):
        calls.append(argv)
        return _fake_keygen(argv, **kwargs)

    assert ensure_probe_key(home, run=keygen) == PUBKEY
    assert ensure_probe_key(home, run=keygen) == PUBKEY
    assert len(calls) == 1 and calls[0][:5] == ["ssh-keygen", "-q", "-t", "ed25519", "-N"]
    assert _mode(home.probe_key) == 0o600 and _mode(home.keys_dir) == 0o700


def test_probe_key_failure(tmp_path) -> None:
    def broken(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "no entropy")

    with pytest.raises(ImageError, match="ssh-keygen"):
        ensure_probe_key(VMHome(tmp_path), run=broken)


def test_console_password_is_created_once_and_private(tmp_path) -> None:
    home = VMHome(tmp_path)
    first = ensure_console_password(home)
    assert len(first) >= 20 and ensure_console_password(home) == first == read_console_password(home)
    assert _mode(home.console_password_file) == 0o600


# ------------------------------------------------------------------------------- golden and runs


class FakeVM:
    """Stands in for QemuVM in prepare boots: records its arguments and returns a serial log."""

    serial = f"cloud-init ...\n{GOLDEN_MARKER}\n"
    instances: list[FakeVM] = []

    def __init__(self, runner, **kwargs) -> None:
        self.runner = runner
        self.kwargs = kwargs
        FakeVM.instances.append(self)

    def run_to_poweroff(self, timeout: float) -> str:
        assert self.kwargs["stay_on_poweroff"] is False
        assert self.kwargs["disk"].exists()  # the overlay was created before the boot
        assert all(Path(d).exists() for d in self.kwargs["extra_drives"])
        return self.serial


@pytest.fixture
def golden_home(fake_docker):
    docker, runner = fake_docker
    home = VMHome(runner.vm_home).ensure()
    payload = b"pretend debian image"
    image = _image(payload, "debian-test.qcow2")
    home.base_image(image).write_bytes(payload)
    FakeVM.instances = []
    FakeVM.serial = f"cloud-init ...\n{GOLDEN_MARKER}\n"
    return home, runner, image, docker


def test_build_golden(golden_home) -> None:
    home, runner, image, docker = golden_home
    info = build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM)
    assert home.golden_disk.exists() and _mode(home.golden_disk) == 0o444
    assert not (home.golden_dir / "golden.building.qcow2").exists()
    assert not (home.golden_dir / "build").exists()  # seed files (with the password) are gone
    assert info.probe_public_key == PUBKEY and info.console_user == "root" and info.page_marker == PAGE_MARKER
    assert len(info.page_sha256) == 64
    assert load_golden_info(home) == info
    saved = home.golden_info_file.read_text()
    assert read_console_password(home) not in saved
    (vm,) = FakeVM.instances
    assert vm.kwargs["name"] == "keyhold-golden-build"
    assert [Path(d).name for d in vm.kwargs["extra_drives"]] == ["seed.iso"]

    # A second call returns the existing build without booting.
    assert build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM) == info
    assert len(FakeVM.instances) == 1


def test_build_golden_force_drops_fault_images(golden_home) -> None:
    home, runner, image, _ = golden_home
    build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM)
    old = home.fault_disk("sshd-masked")
    old.write_bytes(b"built on the old golden image")
    os.chmod(old, 0o444)
    build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM, force=True)
    assert not old.exists() and len(FakeVM.instances) == 2


def test_build_golden_failure_keeps_nothing(golden_home) -> None:
    home, runner, image, _ = golden_home
    FakeVM.serial = "kernel panic\n"
    with pytest.raises(ImageError, match="without finishing"):
        build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM)
    assert not home.golden_disk.exists()
    assert not (home.golden_dir / "golden.building.qcow2").exists()
    assert not (home.golden_dir / "build" / "seed").exists()
    assert not (home.golden_dir / "build" / "seed.iso").exists()


def test_build_golden_needs_a_verified_base(golden_home) -> None:
    home, runner, image, _ = golden_home
    home.base_image(image).write_bytes(b"something else")
    with pytest.raises(ImageError, match="SHA512"):
        build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM)


def test_runs(golden_home) -> None:
    home, runner, image, docker = golden_home
    with pytest.raises(ImageError, match="not built"):
        new_run(home, runner, None)
    build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM)
    run_id, disk = new_run(home, runner, None, "healthy-1")
    assert disk == home.run_dir("healthy-1") / "disk.qcow2" and disk.exists()
    qemu_img = docker.calls[-1][docker.calls[-1].index("qemu-img") :]
    assert qemu_img[qemu_img.index("-b") + 1] == "../../golden/golden.qcow2"
    with pytest.raises(ImageError, match="already exists"):
        new_run(home, runner, None, "healthy-1")
    with pytest.raises(ImageError, match="fault image"):
        new_run(home, runner, "sshd-masked")
    with pytest.raises(ValueError):
        new_run(home, runner, None, "../escape")

    home.fault_disk("sshd-masked").write_bytes(b"fault")
    vm = new_run_vm(home, runner, "sshd-masked", "r2")
    assert vm.name == "keyhold-run-r2" and vm.disk == home.run_dir("r2") / "disk.qcow2"
    remove_run(home, "r2")
    assert not home.run_dir("r2").exists()


def test_probe_spec_for_a_run(golden_home) -> None:
    home, runner, image, _ = golden_home
    info = build_golden(home, runner, image=image, keygen=_fake_keygen, vm_factory=FakeVM)
    spec = probe_spec_for(info, VMEndpoints(host="127.0.0.1", qmp_console=1, qmp_control=2, ssh=40022, http=40080))
    assert isinstance(spec, ProbeSpec)
    assert (spec.ssh_port, spec.ssh_user, spec.url) == (40022, "probe", "http://127.0.0.1:40080/")
    assert spec.expect_sha256 == info.page_sha256 and spec.expect_substring is None
    assert spec.probe_public_key == PUBKEY
