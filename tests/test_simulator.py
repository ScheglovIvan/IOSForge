"""iOS Simulator rendering: locale args, frame diff, stable/blank frames (toolchain mocked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from iosforge.mvp import simulator, xcode


def _frame(path: Path, *, shade: int = 255, boxes: int = 6, shift: int = 0) -> Path:
    img = Image.new("RGB", (120, 260), (shade, shade, shade))
    draw = ImageDraw.Draw(img)
    for i in range(boxes):
        draw.rectangle((10 + shift, 30 + i * 35, 110, 55 + i * 35), fill=(200, 40 + i * 30, 60))
    img.save(path)
    return path


@pytest.mark.parametrize(
    ("locale", "args"),
    [
        ("en-US", ("-AppleLanguages", "(en)", "-AppleLocale", "en_US")),
        ("ru_RU", ("-AppleLanguages", "(ru)", "-AppleLocale", "ru_RU")),
        ("de", ("-AppleLanguages", "(de)", "-AppleLocale", "de")),
    ],
)
def test_launch_args_follow_the_locale(locale: str, args: tuple[str, ...]) -> None:
    assert simulator.SimEnvironment(udid="U", locale=locale).launch_args() == args


def test_frame_diff(tmp_path: Path) -> None:
    a = _frame(tmp_path / "a.png")
    assert simulator.frame_diff(a, _frame(tmp_path / "b.png")) == 0.0
    assert simulator.frame_diff(a, _frame(tmp_path / "c.png", shift=8)) > 0.005
    Image.new("RGB", (60, 60)).save(tmp_path / "small.png")
    assert simulator.frame_diff(a, tmp_path / "small.png") == 1.0


def test_near_uniform(tmp_path: Path) -> None:
    assert simulator.near_uniform(_frame(tmp_path / "blank.png", boxes=0))
    assert not simulator.near_uniform(_frame(tmp_path / "drawn.png"))


def _scripted_screens(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, Any]]) -> None:
    queue = list(frames)

    def shoot(udid: str, out: Path) -> Path:
        spec = queue.pop(0) if len(queue) > 1 else queue[0]
        return _frame(out, **spec)

    monkeypatch.setattr(simulator.xcode, "screenshot", shoot)
    monkeypatch.setattr(simulator.time, "sleep", lambda s: None)


def test_capture_waits_for_two_matching_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scripted_screens(monkeypatch, [{"shift": 0}, {"shift": 6}, {"shift": 12}, {"shift": 12}])
    shot = simulator.capture_stable("U", tmp_path / "s.png")
    assert shot.frames == 4 and shot.diff < simulator.STABLE_FRAME_MAX_DIFF and not shot.blank
    assert not (tmp_path / "s.prev.png").exists()


def test_capture_raises_when_never_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shifts = iter(range(0, 1000, 7))

    def shoot(udid: str, out: Path) -> Path:
        return _frame(out, shift=next(shifts) % 40)

    monkeypatch.setattr(simulator.xcode, "screenshot", shoot)
    monkeypatch.setattr(simulator.time, "sleep", lambda s: None)
    with pytest.raises(simulator.UnstableFrame, match="no stable frame"):
        simulator.capture_stable("U", tmp_path / "s.png", timeout_s=0.05)


def test_blank_stable_frame_keeps_waiting_until_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scripted_screens(monkeypatch, [{"boxes": 0}, {"boxes": 0}, {"boxes": 0}, {}, {}])
    shot = simulator.capture_stable("U", tmp_path / "s.png")
    assert shot.frames == 5 and not shot.blank


def test_blank_screen_is_returned_flagged_at_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scripted_screens(monkeypatch, [{"boxes": 0}])
    shot = simulator.capture_stable("U", tmp_path / "s.png", timeout_s=0.05)
    assert shot.blank


def test_require_toolchain_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    with pytest.raises(simulator.SimulatorUnavailable, match="Mac worker"):
        simulator.require_toolchain()


def test_pin_environment_and_render_screen_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(xcode, "simctl", lambda *args, **kw: calls.append(args) or "")
    monkeypatch.setattr(
        simulator, "capture_stable", lambda udid, out: simulator.StableShot(out, 2, 0.0)
    )
    env = simulator.SimEnvironment(udid="U", locale="en-US")

    simulator.pin_environment(env)
    shot = simulator.render_screen(env, "com.example.app", "0011", tmp_path / "0011.png")

    assert calls[0] == ("bootstatus", "U", "-b")
    assert calls[1][:3] == ("status_bar", "U", "override")
    assert ("ui", "U", "appearance", "light") in calls
    assert ("ui", "U", "content_size", "large") in calls
    assert calls[-1] == (
        "launch", "--terminate-running-process", "U", "com.example.app",
        "-screen-id", "0011", "-AppleLanguages", "(en)", "-AppleLocale", "en_US",
    )  # fmt: skip
    assert shot.frames == 2


def test_minimal_screen_with_some_content_is_not_blank(tmp_path: Path) -> None:
    assert not simulator.near_uniform(_frame(tmp_path / "minimal.png", boxes=1))


def test_resolve_udid_prefers_configured_then_a_booted_iphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert simulator.resolve_udid("CONF") == "CONF"
    monkeypatch.setattr(xcode, "toolchain_available", lambda: True)
    listing = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.watchOS-11": [{"name": "Watch", "udid": "W"}],
            "com.apple.CoreSimulator.SimRuntime.iOS-18": [
                {"name": "iPad Air", "udid": "P", "state": "Booted"},
                {"name": "iPhone 15", "udid": "A", "state": "Shutdown"},
                {"name": "iPhone 16", "udid": "B", "state": "Booted"},
            ],
        }
    }
    monkeypatch.setattr(xcode, "simctl", lambda *a, **k: json.dumps(listing))
    assert simulator.resolve_udid("") == "B"
    monkeypatch.setattr(xcode, "simctl", lambda *a, **k: json.dumps({"devices": {}}))
    with pytest.raises(simulator.SimulatorUnavailable, match="IOS_SIMULATOR_UDID"):
        simulator.resolve_udid("")
