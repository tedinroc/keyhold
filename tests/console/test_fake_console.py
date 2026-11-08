"""FakeConsole: scripted screens, rules, recorded input, and the same input checks as QEMU."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from keyhold.console.base import ConsoleClosed, ConsoleError, UnsupportedKey, UnsupportedText
from keyhold.console.fake import FakeConsole, Rule, Screen, render_text_png
from keyhold.types import Console


def _login_console() -> FakeConsole:
    return FakeConsole(
        {
            "login": Screen(["Debian GNU/Linux 13 demo tty1", ""], prompt="demo login: "),
            "password": Screen([], prompt="Password: ", echo=False),
            "shell": Screen([], prompt="root@demo:~# "),
            "rebooting": Screen(["Rebooting."]),
        },
        rules=[
            Rule(on="login", line="root", goto="password"),
            Rule(on="password", line="s3cret-pass", goto="shell"),
            Rule(on="password", line=".*", goto="login", output=["Login incorrect"]),
            Rule(on="shell", line=r"systemctl (re)?start ssh", output=["(started)"]),
            Rule(on="shell", line="reboot", goto="rebooting"),
            Rule(keys=["ctrl", "alt", "delete"], goto="rebooting"),
        ],
        on_power_cycle="login",
    )


def _png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        return image.size


def test_is_a_console() -> None:
    assert isinstance(FakeConsole(), Console)


def test_screenshot_is_png_and_shows_typed_text() -> None:
    console = _login_console()
    assert _png_size(console.screenshot()) == (1024, 768)
    console.type_text("ro")
    assert console.visible_lines()[-1] == "demo login: ro_"
    first = console.screenshot()
    console.type_text("o")
    assert console.screenshot() != first
    assert console.screenshots == 3


def test_rules_drive_a_login_and_a_command() -> None:
    console = _login_console()
    console.type_text("root\n")
    assert console.screen == "password"
    console.type_text("s3cret-pass")
    assert "s3cret-pass" not in "".join(console.visible_lines())  # no echo at a password prompt
    console.press(["Enter"])
    assert console.screen == "shell"
    console.type_text("systemctl restart ssh\n")
    assert console.visible_lines()[-2:] == ["(started)", "root@demo:~# _"]
    assert console.entered == ["root", "s3cret-pass", "systemctl restart ssh"]


def test_wrong_password_goes_back_to_login() -> None:
    console = _login_console()
    console.type_text("root\nnope\n")
    assert console.screen == "login"
    assert "Login incorrect" in console.visible_lines()


def test_line_editing_keys() -> None:
    console = _login_console()
    console.type_text("rooot")
    console.press(["backspace"])
    console.press(["backspace"])
    assert console.buffer == "roo"
    console.press(["ctrl", "u"])
    assert console.buffer == ""
    console.type_text("abc")
    console.press(["ctrl", "c"])
    assert console.buffer == "" and console.visible_lines()[-2] == "demo login: abc^C"


def test_key_rule_and_power_cycle() -> None:
    console = _login_console()
    console.type_text("root\ns3cret-pass\n")
    console.press(["ctrl", "alt", "del"])
    assert console.screen == "rebooting"
    console.power_cycle()
    assert console.screen == "login" and console.power_cycles == 1
    assert [r.kind for r in console.inputs] == ["type", "press", "power_cycle"]
    assert console.inputs[1].value == ["ctrl", "alt", "delete"]
    assert console.inputs[1].screen == "shell"


def test_power_cycle_rule_and_once() -> None:
    console = FakeConsole(
        {"broken": ["Enter username:"], "fixed": ["login:"]},
        rules=[Rule(power_cycle=True, goto="fixed", once=True)],
    )
    console.power_cycle()
    assert console.screen == "fixed"
    console.goto("broken")
    console.power_cycle()
    assert console.screen == "broken"  # the rule fired once; default is the start screen


def test_bad_input_is_refused_before_it_is_recorded() -> None:
    console = _login_console()
    with pytest.raises(UnsupportedText):
        console.type_text("root\ncafé\n")
    with pytest.raises(UnsupportedKey):
        console.press(["hyper"])
    with pytest.raises(TypeError):
        console.type_text(b"root")  # type: ignore[arg-type]
    assert console.inputs == [] and console.screen == "login"


def test_png_screens_are_returned_as_given() -> None:
    png = render_text_png(["GRUB", "Enter username:"], (640, 480))
    console = FakeConsole({"grub": png})
    assert console.screenshot() == png
    assert console.visible_lines() == []
    assert _png_size(png) == (640, 480)


def test_close_and_injected_errors() -> None:
    console = _login_console()
    console.fail_next("screenshot", ConsoleError("display gone"))
    with pytest.raises(ConsoleError):
        console.screenshot()
    assert console.screenshot()  # only the next call fails
    seen = []
    console.on_input(seen.append)
    console.type_text("x")
    assert [r.value for r in seen] == ["x"]
    console.close()
    for call in (console.screenshot, lambda: console.type_text("a"), lambda: console.press(["a"]), console.power_cycle):
        with pytest.raises(ConsoleClosed):
            call()
    with pytest.raises(ValueError):
        console.fail_next("reboot", RuntimeError())


def test_constructor_checks() -> None:
    with pytest.raises(ValueError):
        FakeConsole({})
    with pytest.raises(ValueError):
        FakeConsole({"a": ["x"]}, rules=[Rule(line="x", goto="missing")])
    with pytest.raises(ValueError):
        Rule(goto="a")  # no trigger
    with pytest.raises(ValueError):
        Rule(line="x", power_cycle=True)  # two triggers
    single = FakeConsole(["only screen"])
    assert single.screen == "screen" and single.visible_lines()[0] == "only screen"
    assert FakeConsole("one\ntwo").visible_lines()[:2] == ["one", "two"]


def test_typed_text_joins_inputs() -> None:
    console = FakeConsole()
    console.type_text("ab")
    console.press(["ret"])
    console.type_text("cd")
    assert console.typed_text() == "abcd"
