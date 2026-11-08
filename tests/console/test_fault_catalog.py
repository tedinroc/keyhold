"""The fault taxonomy, the fault catalog and planting (with a fake docker CLI and a fake VM)."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from console_fakes import write_golden_info

from keyhold.console.image import PLANT_LABEL, ImageError, VMHome
from keyhold.console.qemu import GUEST_MAC
from keyhold.faults import (
    CLASS_TIER,
    FAULT_CLASSES,
    FAULTS,
    TRY_FAULTS,
    Fault,
    get_fault,
    tier_for_class,
)
from keyhold.faults.plant import plant_fault, plant_marker, plant_script, write_plant_files
from keyhold.types import TIER_PRICE, Tier


def test_taxonomy_covers_every_tier() -> None:
    assert {c.tier for c in FAULT_CLASSES.values()} == {*Tier, None}
    assert dict(CLASS_TIER) == {name: c.tier for name, c in FAULT_CLASSES.items()}
    assert tier_for_class("sshd_config_error") is Tier.T1
    assert tier_for_class("fstab_bad_entry") is Tier.T2
    assert tier_for_class("initramfs_broken") is Tier.T3
    assert tier_for_class("console_locked") is None  # named, never priced
    with pytest.raises(ValueError):
        tier_for_class("made_up_class")
    with pytest.raises(TypeError):
        FAULT_CLASSES["x"] = FAULT_CLASSES["disk_full"]  # type: ignore[index]


def test_taxonomy_matches_the_free_look() -> None:
    quote = pytest.importorskip("keyhold.agent.quote")
    taxonomy = getattr(quote, "CLASS_TIERS", None)
    if taxonomy is None:
        pytest.skip("keyhold.agent.quote has no CLASS_TIERS table")
    assert dict(taxonomy) == dict(CLASS_TIER)


def test_catalog_has_the_required_faults() -> None:
    assert len(FAULTS) >= 5
    assert "emergency mode" in " ".join(FAULTS["fstab-missing-disk"].symptom)
    assert FAULTS["fstab-missing-disk"].fault_class == "fstab_bad_entry"
    assert "mask" in FAULTS["sshd-masked"].plant
    assert FAULTS["sshd-masked"].fault_class == "ssh_service_disabled"
    port = FAULTS["sshd-port-page-down"]
    assert re.search(r"^Port 2222$", port.plant, flags=re.MULTILINE) and "disable demo-page.socket" in port.plant
    assert "netplan" in FAULTS["network-static-wrong"].plant
    locked = FAULTS["grub-password"]
    assert locked.solvable is False and "password_pbkdf2" in locked.plant
    assert locked.fault_class == "console_locked" and locked.tier is None and locked.price is None
    assert [f.id for f in FAULTS.values() if not f.solvable] == [locked.id]
    assert all(f.tier is not None for f in FAULTS.values() if f.solvable)


@pytest.mark.parametrize("fault", list(FAULTS.values()), ids=list(FAULTS))
def test_each_fault_is_consistent(fault: Fault) -> None:
    assert fault.fault_class in FAULT_CLASSES
    assert fault.tier is CLASS_TIER[fault.fault_class]
    assert fault.price == (TIER_PRICE[fault.tier] if fault.tier else None)
    assert fault.symptom and all(line.strip() for line in fault.symptom)
    re.compile(fault.ready_marker)
    assert re.fullmatch(r"[a-z0-9-]+", fault.id)
    assert fault.fix and fault.title
    assert 0 < fault.boot_seconds < 600
    assert 0 <= fault.screen_settle_s <= 30


@pytest.mark.parametrize("fault", list(FAULTS.values()), ids=list(FAULTS))
def test_plant_scripts_are_valid_bash(fault: Fault) -> None:
    script = plant_script(fault)
    assert script.startswith("#!/bin/bash\n")
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_network_fault_targets_the_guest_nic() -> None:
    plant = FAULTS["network-static-wrong"].plant
    assert GUEST_MAC in plant
    assert "192.0.2." in plant  # TEST-NET-1: an address range reserved for documentation


def test_try_faults_and_lookup() -> None:
    assert set(TRY_FAULTS) <= set(FAULTS)
    assert any(not FAULTS[f].solvable for f in TRY_FAULTS)  # the honest stop is on offer
    assert get_fault("fstab-missing-disk").tier is Tier.T2
    with pytest.raises(KeyError):
        get_fault("nope")


def test_fault_rejects_unknown_class_or_empty_symptom() -> None:
    with pytest.raises(ValueError):
        Fault("x", "x", "made_up", True, "true", ("s",), "login:", "fix")
    with pytest.raises(ValueError):
        Fault("x", "x", "disk_full", True, "true", (), "login:", "fix")
    with pytest.raises(ValueError):
        Fault("x", "x", "disk_full", True, "true", ("s",), "login:", "fix", screen_settle_s=-1.0)


def test_screen_settle_and_power_off_follow_the_real_guests() -> None:
    # Measured on real guests: the emergency prompt is drawn well after its serial marker, and a
    # guest held at the GRUB password prompt never answers the ACPI power button.
    assert FAULTS["fstab-missing-disk"].screen_settle_s >= 10.0
    assert [f.id for f in FAULTS.values() if not f.acpi_power_off] == ["grub-password"]


def test_write_plant_files(tmp_path) -> None:
    fault = FAULTS["sshd-masked"]
    paths = write_plant_files(tmp_path / "disk", fault)
    assert [p.name for p in paths] == ["plant.sh", "fault-id"]
    assert (tmp_path / "disk" / "fault-id").read_text() == "sshd-masked\n"
    assert plant_marker(fault) == "KEYHOLD-PLANT-OK sshd-masked"
    assert plant_marker(fault, ok=False) == "KEYHOLD-PLANT-FAILED sshd-masked"


class FakePlantVM:
    serial = ""
    instances: list[FakePlantVM] = []

    def __init__(self, runner, **kwargs) -> None:
        self.kwargs = kwargs
        FakePlantVM.instances.append(self)

    def run_to_poweroff(self, timeout: float) -> str:
        assert self.kwargs["stay_on_poweroff"] is False
        assert self.kwargs["disk"].exists()
        return FakePlantVM.serial


@pytest.fixture
def planting(fake_docker):
    docker, runner = fake_docker
    home = VMHome(runner.vm_home).ensure()
    home.golden_disk.write_bytes(b"golden")
    write_golden_info(home.root)
    FakePlantVM.instances = []
    return home, runner, docker


def test_plant_fault(planting) -> None:
    home, runner, docker = planting
    fault = FAULTS["sshd-config-typo"]
    FakePlantVM.serial = f"boot...\nplant: done\n{plant_marker(fault)}\n"
    path = plant_fault(home, runner, fault, vm_factory=FakePlantVM)
    assert path == home.fault_disk(fault.id) and path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert not (home.faults_dir / f"{fault.id}.building.qcow2").exists()
    assert not (home.faults_dir / "build" / fault.id).exists()
    (vm,) = FakePlantVM.instances
    assert vm.kwargs["name"] == "keyhold-plant-sshd-config-typo"
    assert Path(vm.kwargs["extra_drives"][0]).name == "plant.iso"
    iso_call = next(c for c in docker.calls if "genisoimage" in c)
    assert iso_call[iso_call.index("-volid") + 1] == PLANT_LABEL
    overlay_call = next(c for c in docker.calls if "qemu-img" in c)
    assert overlay_call[overlay_call.index("-b") + 1] == "../golden/golden.qcow2"

    # Already built: no second boot unless forced.
    plant_fault(home, runner, fault, vm_factory=FakePlantVM)
    assert len(FakePlantVM.instances) == 1
    plant_fault(home, runner, fault, vm_factory=FakePlantVM, force=True)
    assert len(FakePlantVM.instances) == 2


def test_plant_fault_reports_script_failure(planting) -> None:
    home, runner, _ = planting
    fault = FAULTS["grub-password"]
    FakePlantVM.serial = f"plant: grub-mkpasswd-pbkdf2: not found\n{plant_marker(fault, ok=False)}\n"
    with pytest.raises(ImageError, match="plant script failed") as info:
        plant_fault(home, runner, fault, vm_factory=FakePlantVM)
    assert "grub-mkpasswd-pbkdf2: not found" in str(info.value)
    assert not home.fault_disk(fault.id).exists()
    assert not (home.faults_dir / f"{fault.id}.building.qcow2").exists()


def test_plant_fault_needs_golden(planting) -> None:
    home, runner, _ = planting
    os.unlink(home.golden_disk)
    with pytest.raises(ImageError, match="golden"):
        plant_fault(home, runner, FAULTS["sshd-masked"], vm_factory=FakePlantVM)


def test_symptoms_point_at_their_own_class() -> None:
    """Where a symptom line matches a free-look signature, it is the signature of the fault's
    own class, so the on-screen evidence never points at a different class."""
    quote = pytest.importorskip("keyhold.agent.quote")
    taxonomy = getattr(quote, "TAXONOMY", None)
    if not taxonomy or not hasattr(taxonomy[0], "signatures"):
        pytest.skip("keyhold.agent.quote has no signature table")
    for fault in FAULTS.values():
        for line in fault.symptom:
            matched = {c.name for c in taxonomy for sig in c.signatures if re.search(sig, line)}
            assert matched <= {fault.fault_class}, (fault.id, line, matched)
    assert any(
        re.search(sig, line)
        for line in FAULTS["fstab-missing-disk"].symptom
        for c in taxonomy
        if c.name == "fstab_bad_entry"
        for sig in c.signatures
    )
