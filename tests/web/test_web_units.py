"""Unit tests for the web helpers: session cookie, rate limiter, spend meter, worlds."""

from __future__ import annotations

import json
import re
import types
from decimal import Decimal
from pathlib import Path

import pytest

from keyhold.agent.model import FakeModel, ModelUnavailable, text_reply
from keyhold.console.qemu import VMEndpoints
from keyhold.pay.ledger import Ledger
from keyhold.probe.spec import parse_public_key
from keyhold.types import ModelReply
from web.config import Settings
from web.security import RateLimiter, Session, SessionCodec, check_passcode
from web.spend import MeteredModel, SpendMeter, reply_cost
from web.views import probe_view, timeline_entry
from web.worlds import SimulatedWorlds, VmWorlds, WorldError, simulated_probe_spec

# -- session cookie ------------------------------------------------------------------------------


def test_session_cookie_round_trip_and_tampering() -> None:
    codec = SessionCodec("unit-test-secret-key-0123456789")
    session = Session.new(1000.0)
    session.unlock()
    cookie = codec.encode(session)
    back = codec.decode(cookie, 1001.0)
    assert back is not None and back.sid == session.sid and back.csrf == session.csrf and back.unlocked

    body, mac = cookie.split(".")
    assert codec.decode(body + "." + mac[:-2] + "AA", 1001.0) is None
    forged_body = body[:-2] + ("AA" if body[-2:] != "AA" else "BB")
    assert codec.decode(forged_body + "." + mac, 1001.0) is None
    assert codec.decode(cookie, 1000.0 + 13 * 3600) is None  # expired
    assert SessionCodec("another-secret-key-0123456789").decode(cookie, 1001.0) is None
    assert codec.decode("garbage", 1001.0) is None and codec.decode(None, 1001.0) is None
    assert back.csrf_ok(session.csrf) and not back.csrf_ok("x") and not back.csrf_ok(None)


def test_passcode_check() -> None:
    assert check_passcode("abc", "abc")
    assert not check_passcode("abd", "abc")
    assert not check_passcode("", "")  # an unset passcode never matches
    assert not check_passcode(None, "abc")


def test_rate_limiter_window(clock) -> None:
    limiter = RateLimiter(clock.now)
    assert limiter.hit("b", "k", limit=2, window_s=60) is None
    assert limiter.hit("b", "k", limit=2, window_s=60) is None
    wait = limiter.hit("b", "k", limit=2, window_s=60)
    assert wait is not None and 0 < wait <= 60
    assert limiter.hit("b", "other", limit=2, window_s=60) is None
    assert limiter.remaining("b", "k", limit=2, window_s=60) == 0
    clock.advance(61)
    assert limiter.hit("b", "k", limit=2, window_s=60) is None


# -- spend ------------------------------------------------------------------------------------------


def reply(input_tokens: int, output_tokens: int) -> ModelReply:
    return ModelReply(stop_reason="end_turn", content=[], input_tokens=input_tokens, output_tokens=output_tokens)


def test_reply_cost_uses_list_prices() -> None:
    assert reply_cost(reply(1_000_000, 0)) == Decimal(4)
    assert reply_cost(reply(0, 1_000_000)) == Decimal(20)
    assert reply_cost(reply(1234, 567)) == Decimal("0.016276")


def test_spend_meter_persists_and_rolls_over(tmp_path, clock) -> None:
    ledger = Ledger(tmp_path / "s.db", clock=clock)
    meter = SpendMeter(ledger, Decimal(5), clock=clock.now)
    meter.record(reply(0, 100_000), rescue_id=None, run_id="r1", purpose="free look")
    assert meter.spent() == Decimal(2)
    again = SpendMeter(ledger, Decimal(5), clock=clock.now)  # a restart reads the ledger
    assert again.spent() == Decimal(2) and again.remaining() == Decimal(3)
    clock.advance(24 * 3600 + 1)
    assert again.spent() == Decimal(0)
    ledger.close()


def test_metered_model_stops_at_the_run_budget(tmp_path, clock) -> None:
    ledger = Ledger(tmp_path / "s.db", clock=clock)
    meter = SpendMeter(ledger, Decimal(100), clock=clock.now)
    priced = [ModelReply("end_turn", [{"type": "text", "text": "x"}], output_tokens=50_000)] * 5
    model = MeteredModel(FakeModel(priced), meter, run_id="r", budget_usd=Decimal(2))
    model.respond(system="s", messages=[])
    model.respond(system="s", messages=[])
    with pytest.raises(ModelUnavailable, match="model budget"):
        model.respond(system="s", messages=[])
    assert model.spent == Decimal(2)

    capped = SpendMeter(ledger, Decimal(1), clock=clock.now)
    with pytest.raises(ModelUnavailable, match="cap"):
        MeteredModel(FakeModel([text_reply("x")]), capped, run_id="r2", budget_usd=Decimal(9)).respond(
            system="s", messages=[]
        )
    ledger.close()


# -- views ----------------------------------------------------------------------------------------


def test_probe_view_hides_the_host_and_shows_the_fingerprint() -> None:
    spec = simulated_probe_spec()
    view = probe_view(spec.to_dict(), spec.spec_hash())
    assert view["key_fingerprint"] == parse_public_key(spec.probe_public_key).fingerprint
    assert "simulated.test" not in json.dumps(view)
    assert view["page"] == "HTTP GET / on port 80" and "keyhold demo page" in view["expect"]


def test_timeline_marks_firewall_refusals_and_hides_screenshots() -> None:
    event = types.SimpleNamespace(
        id=7, kind="agent_firewall_refusal", detail="Making a filesystem erases what is on the disk.",
        data={"rule": "mkfs", "input": "mkfs.ext4 /dev/vdb\n"}, at=1_800_000_000.0,
    )
    entry = timeline_entry(event)
    assert entry["group"] == "firewall" and entry["tone"] == "warn"
    assert entry["text"].endswith("Not sent: mkfs.ext4 /dev/vdb ↵")
    shot = types.SimpleNamespace(id=8, kind="agent_screenshot", detail="", data={}, at=1.0)
    assert timeline_entry(shot) is None
    normal = types.SimpleNamespace(id=9, kind="gate_revoked", detail="the agent session ended (done)", data={}, at=1.0)
    assert timeline_entry(normal)["tone"] == ""


# -- simulated worlds -----------------------------------------------------------------------------


def test_simulated_worlds_react_to_the_fix() -> None:
    worlds = SimulatedWorlds()
    sshd = worlds.boot("sshd-config-typo", "r", lambda text: None)
    console = sshd.console
    assert "[FAILED] Failed to start ssh.service" in console.screen_text()
    for line in ("root", sshd.console_password, "sed -i '3d' /etc/ssh/sshd_config", "systemctl restart ssh"):
        console.type_text(line + "\n")
    assert sshd.ssh.login(sshd.probe).ok

    fstab = worlds.boot("fstab-missing-disk", "r", lambda text: None)
    fstab.console.type_text(fstab.console_password + "\n")
    fstab.console.type_text("systemctl reboot\n")  # not fixed yet: back to emergency mode
    assert "emergency mode" in fstab.console.screen_text() and not fstab.ssh.login(fstab.probe).ok
    fstab.console.type_text(fstab.console_password + "\n")
    fstab.console.type_text("sed -i 's|^UUID=7d1e3c55|# &|' /etc/fstab\n")
    fstab.console.type_text("systemctl reboot\n")
    assert "login:" in fstab.console.screen_text() and fstab.ssh.login(fstab.probe).ok

    grub = worlds.boot("grub-password", "r", lambda text: None)
    assert "Enter username:" in grub.console.screen_text() and grub.console_password is None

    with pytest.raises(WorldError):
        worlds.boot("sshd-masked", "r", lambda text: None)


def test_each_simulated_world_gets_its_own_password() -> None:
    a = SimulatedWorlds().boot("sshd-config-typo", "a", lambda text: None)
    b = SimulatedWorlds().boot("sshd-config-typo", "b", lambda text: None)
    assert a.console_password != b.console_password
    assert re.fullmatch(r"kh[0-9a-f]{16}", a.console_password)


# -- VM worlds, wired with fakes (no docker, no VM) ----------------------------------------------


class FakeVM:
    def __init__(self, runner, *, name, run_dir, disk, show_fault=True):
        self.name, self.run_dir, self.disk = name, run_dir, disk
        self.accel = "tcg"
        self.show_fault = show_fault
        self.calls: list[str] = []
        self.console_obj = types.SimpleNamespace(closed=False)
        self.console_obj.close = lambda: setattr(self.console_obj, "closed", True)

    def start(self):
        self.calls.append("start")
        return VMEndpoints(host="loopback-for-test", qmp_console=1, qmp_control=2, ssh=40022, http=40080)

    def wait_for_serial(self, pattern, timeout):
        self.calls.append(f"wait {pattern}")
        return object() if self.show_fault else None

    def console(self):
        self.calls.append("console")
        return self.console_obj

    def shutdown(self, timeout):
        self.calls.append("shutdown")
        return True

    def kill(self):
        self.calls.append("kill")


def vm_home(tmp_path: Path):
    from keyhold.console.image import VMHome

    home = VMHome(tmp_path / "vm").ensure()
    home.golden_disk.write_bytes(b"")
    for fault in ("sshd-config-typo", "fstab-missing-disk", "grub-password"):
        home.fault_disk(fault).write_bytes(b"")
    home.probe_key.write_text("not a real key\n")
    home.console_password_file.write_text("vm-console-pw-not-real\n")
    spec = simulated_probe_spec()
    home.golden_info_file.write_text(
        json.dumps(
            {
                "base_image": "base.qcow2", "base_sha512": "0" * 128, "hostname": "keyhold-demo",
                "probe_user": "probe", "probe_public_key": spec.probe_public_key, "console_user": "root",
                "page_sha256": "a" * 64, "page_marker": "page", "built_at": 0.0,
            }
        )
    )
    return home


def test_vm_world_boots_a_fresh_overlay_and_tears_it_down(tmp_path) -> None:
    home = vm_home(tmp_path)
    runner = types.SimpleNamespace(available=lambda: True, image_present=lambda: True)
    vms: list[FakeVM] = []

    def make_vm(runner, **kwargs):
        vms.append(FakeVM(runner, **kwargs))
        return vms[-1]

    def new_run(home, runner, fault_id, run_id):
        home.run_dir(run_id).mkdir(parents=True)
        return run_id, home.run_dir(run_id) / "disk.qcow2"

    settings = Settings(data_dir=tmp_path, db_path=tmp_path / "k.db")
    worlds = VmWorlds(
        settings, home=home, runner=runner, vm_factory=make_vm, new_run=new_run,
        model_factory=lambda: FakeModel([]), ssh_factory=lambda key, run: ("ssh", run),
        http_factory=lambda: "http", sleep=lambda s: None,
    )
    assert worlds.problems() == []
    notes: list[str] = []
    world = worlds.boot("sshd-config-typo", "abc123", notes.append)
    vm = vms[0]
    assert vm.name == "keyhold-run-abc123" and vm.calls[:2] == ["start", "wait login:"]
    assert world.probe.ssh_port == 40022 and world.probe.url.endswith(":40080/")
    assert world.probe.expect_sha256 == "a" * 64
    assert world.console_password == "vm-console-pw-not-real"
    assert world.paid_model and world.keep_slot_for_warranty and world.ssh == ("ssh", "abc123")
    assert notes and home.run_dir("abc123").exists()
    world.close()
    world.close()  # idempotent
    assert vm.calls.count("shutdown") == 1 and vm.console_obj.closed
    assert not home.run_dir("abc123").exists()


def test_vm_world_that_never_shows_the_fault_is_killed_and_removed(tmp_path) -> None:
    home = vm_home(tmp_path)
    vms: list[FakeVM] = []

    def make_vm(runner, **kwargs):
        vms.append(FakeVM(runner, show_fault=False, **kwargs))
        return vms[-1]

    def new_run(home, runner, fault_id, run_id):
        home.run_dir(run_id).mkdir(parents=True)
        return run_id, home.run_dir(run_id) / "disk.qcow2"

    worlds = VmWorlds(
        Settings(data_dir=tmp_path, db_path=tmp_path / "k.db"), home=home, runner=object(), vm_factory=make_vm,
        new_run=new_run, model_factory=lambda: FakeModel([]), sleep=lambda s: None,
    )
    with pytest.raises(WorldError, match="did not show the fault"):
        worlds.boot("fstab-missing-disk", "def456", lambda text: None)
    assert "kill" in vms[0].calls and not home.run_dir("def456").exists()


def test_vm_world_waits_for_the_screen_and_kills_a_guest_held_at_the_boot_loader(tmp_path) -> None:
    from keyhold.faults import get_fault

    home = vm_home(tmp_path)
    vms: list[FakeVM] = []
    sleeps: list[float] = []

    def make_vm(runner, **kwargs):
        vms.append(FakeVM(runner, **kwargs))
        return vms[-1]

    def new_run(home, runner, fault_id, run_id):
        home.run_dir(run_id).mkdir(parents=True)
        return run_id, home.run_dir(run_id) / "disk.qcow2"

    worlds = VmWorlds(
        Settings(data_dir=tmp_path, db_path=tmp_path / "k.db"), home=home, runner=object(), vm_factory=make_vm,
        new_run=new_run, model_factory=lambda: FakeModel([]), ssh_factory=lambda key, run: None,
        http_factory=lambda: None, sleep=sleeps.append,
    )
    # The emergency prompt is drawn after its serial marker: the free look waits the fault's
    # own settle time, as the real-guest driver does.
    fstab = worlds.boot("fstab-missing-disk", "f00001", lambda text: None)
    assert sleeps == [get_fault("fstab-missing-disk").screen_settle_s] and sleeps[0] >= 10.0
    fstab.close()
    assert vms[0].calls.count("shutdown") == 1

    # A guest at the GRUB password prompt ignores the ACPI power button: it is killed at once.
    grub = worlds.boot("grub-password", "f00002", lambda text: None)
    grub.close()
    assert "shutdown" not in vms[1].calls and "kill" in vms[1].calls
    assert not home.run_dir("f00002").exists()


def test_vm_lab_problems_are_listed(tmp_path) -> None:
    from keyhold.console.image import VMHome

    home = VMHome(tmp_path / "empty").ensure()
    runner = types.SimpleNamespace(available=lambda: False, image_present=lambda: False)
    worlds = VmWorlds(Settings(data_dir=tmp_path, db_path=tmp_path / "k.db"), home=home, runner=runner, environ={})
    problems = worlds.problems()
    text = " ".join(problems)
    assert "docker is not available" in text and "golden image" in text and "ANTHROPIC_API_KEY" in text
    assert any("sshd-config-typo" in p for p in problems)


def test_reply_cost_uses_each_models_rate_card() -> None:
    from dataclasses import replace

    haiku = replace(reply(1_000_000, 1_000_000), model="claude-haiku-5-5")
    # 1M input + 1M output on a prompt over 100K tokens: the long-prompt card.
    assert reply_cost(haiku) == Decimal("3.00")
    short = replace(reply(50_000, 10_000), model="claude-haiku-5-5")
    assert reply_cost(short) == Decimal("0.010")
    # A reply with no model name is priced by the model the request asked for.
    unnamed = replace(reply(50_000, 10_000), model="")
    assert reply_cost(unnamed, "claude-haiku-5-5") == Decimal("0.010")
    assert reply_cost(unnamed) == reply_cost(unnamed, "claude-opus-5-5")


def test_keyhold_model_setting() -> None:
    from web.config import Settings, SettingsError

    assert Settings.from_env({}).model == "claude-opus-5-5"
    assert Settings.from_env({"KEYHOLD_MODEL": "claude-haiku-5-5"}).model == "claude-haiku-5-5"
    with pytest.raises(SettingsError, match="KEYHOLD_MODEL"):
        Settings.from_env({"KEYHOLD_MODEL": "gpt-4"})


def test_model_session_header_setting() -> None:
    from web.config import Settings, SettingsError

    assert Settings.from_env({}).model_session_header == ""
    assert Settings.from_env({"KEYHOLD_MODEL_SESSION_HEADER": "x-session-id"}).model_session_header == "x-session-id"
    with pytest.raises(SettingsError, match="KEYHOLD_MODEL_SESSION_HEADER"):
        Settings.from_env({"KEYHOLD_MODEL_SESSION_HEADER": "bad header"})


def test_session_header_setting_refuses_sdk_headers() -> None:
    from web.config import Settings, SettingsError

    with pytest.raises(SettingsError, match="KEYHOLD_MODEL_SESSION_HEADER"):
        Settings.from_env({"KEYHOLD_MODEL_SESSION_HEADER": "x-api-key"})


def test_spend_for_shows_fractions_of_a_cent(tmp_path, clock) -> None:
    from keyhold.pay.ledger import Ledger
    from web.views import spend_for

    ledger = Ledger(tmp_path / "k.db", clock=clock)
    ledger.log(None, "model_spend", "x", {"usd": "0.0034"})
    assert spend_for(ledger, None) == "0.0034"
    ledger.log(None, "model_spend", "y", {"usd": "1.20"})
    assert spend_for(ledger, None) == "1.20"
    assert spend_for(ledger, "no-such-rescue") is None
