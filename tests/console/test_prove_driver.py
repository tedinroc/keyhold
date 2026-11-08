"""Pieces of tools/vm/prove.py that run without docker: the pacing of the scripted model, the
gate check before a hold, and the leak scan. No VM, no network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from keyhold.faults.fixes import PASSWORD, FixStep
from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger
from keyhold.types import Quote, Tier

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def prove():
    spec = importlib.util.spec_from_file_location("keyhold_tools_prove", ROOT / "tools" / "vm" / "prove.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


class SerialVM:
    """Stands in for QemuVM: a serial log that grows when the test says so."""

    def __init__(self) -> None:
        self.text = ""
        self.waited: list[tuple[str, int]] = []

    def serial_text(self) -> str:
        return self.text

    def wait_for_serial(self, pattern, timeout, *, start=0):
        import re

        self.waited.append((pattern, start))
        return re.search(pattern, self.text[start:])


class RecordingConsole:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    def type_text(self, text: str) -> None:
        self.calls.append("type")

    def press(self, keys: list[str]) -> None:
        self.calls.append("key")

    def power_cycle(self) -> None:
        self.calls.append("power_cycle")

    def close(self) -> None:
        pass


def test_pacer_waits_for_the_previous_marker_and_fills_the_password(prove) -> None:
    vm = SerialVM()
    steps = (
        FixStep("type", PASSWORD + "\n", wait_for=r"session opened"),
        FixStep("finished", "done"),
    )
    pacer = prove.Pacer(vm, steps, "not-a-real-password")
    model = pacer.model()
    first = model.respond(system="", messages=[])
    assert first.content[0]["input"] == {"text": "not-a-real-password\n"}
    vm.text += "boot\nsession opened for user root\n"
    second = model.respond(system="", messages=[])
    assert second.content[0]["name"] == "finished"
    assert vm.waited == [(r"session opened", 0)]
    assert pacer.waits[0]["seen"] is True


def test_pacer_stops_the_script_when_a_marker_never_shows(prove) -> None:
    vm = SerialVM()
    pacer = prove.Pacer(vm, (FixStep("type", "x\n", wait_for="never", timeout=1), FixStep("finished", "d")), "pw")
    model = pacer.model()
    model.respond(system="", messages=[])
    with pytest.raises(RuntimeError, match="did not appear"):
        model.respond(system="", messages=[])
    assert pacer.error and "never" in pacer.error


def test_gate_refuses_every_input_before_the_hold(prove, tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    quote = Quote.build("ssh_service_disabled", ["ssh is masked"], Tier.T1, "Unmask ssh.")
    rec = ledger.create_rescue(quote)
    console = prove.CountingConsole(RecordingConsole())
    outcome = prove._gate_refuses(console, ledger, FakePayPal(), rec.id)
    assert outcome["refused"] is True
    assert outcome["inputs_reached_console"] == 0
    assert outcome["inputs_refused"] == 3
    assert console.inputs == []
    ledger.close()


def test_leak_scan_finds_the_needle(prove, tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "clean.txt").write_text("nothing here")
    (tmp_path / "a" / "dirty.txt").write_text("xx secret-value yy")
    assert prove._files_containing(tmp_path, b"secret-value") == [tmp_path / "a" / "dirty.txt"]
