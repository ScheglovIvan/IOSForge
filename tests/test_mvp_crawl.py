"""Crawler tests: greedy state-graph coverage, backtracking and dialog dismissal.

Drives :func:`iosforge.mvp.crawl.walk` against a fake adb device (a tiny screen
graph) so the exploration logic is exercised without a real emulator — the same
monkeypatching style as ``tests/test_mvp.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from iosforge.mvp import crawl
from iosforge.mvp.paths import RunPaths

APP = "com.example.app"
LAUNCHER = "com.android.launcher"


def _xml(elements: list[tuple[str, str, str, str]]) -> str:
    nodes = "".join(
        f'<node clickable="true" text="{text}" resource-id="{rid}" '
        f'content-desc="" bounds="{bounds}"/>'
        for rid, text, bounds, _dest in elements
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><hierarchy>{nodes}</hierarchy>'


class FakeDevice:
    """A scripted screen graph reachable over a minimal adb surface."""

    def __init__(self, start: str, screens: dict[str, dict[str, object]]) -> None:
        self.current = start
        self.screens = screens

    def adb(self, *args: str) -> SimpleNamespace:
        screen = self.screens[self.current]
        if args[:1] == ("shell",) and "uiautomator" in args:
            return SimpleNamespace(stdout="")
        if args[:2] == ("exec-out", "cat"):
            return SimpleNamespace(stdout=_xml(screen["elements"]))  # type: ignore[arg-type]
        if "dumpsys" in args:
            pkg, act = screen["package"], screen["activity"]
            return SimpleNamespace(
                stdout=f"mResumedActivity: ActivityRecord{{a0 u0 {pkg}/{act} t1}}"
            )
        if args[:2] == ("shell", "input") and "tap" in args:
            target = (int(args[3]), int(args[4]))
            for _rid, _text, bounds, dest in screen["elements"]:  # type: ignore[union-attr]
                if crawl._center(bounds) == target:
                    self.current = dest
                    break
            return SimpleNamespace(stdout="")
        if args[:2] == ("shell", "input") and "keyevent" in args:
            self.current = screen["back"]  # type: ignore[assignment]
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout="")

    def adb_raw(self, *args: str) -> bytes:
        return b"\x89PNG\r\n"

    def launch(self, package: str) -> None:
        self.current = "menu"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crawl.time, "sleep", lambda *_: None)


def _install(monkeypatch: pytest.MonkeyPatch, device: FakeDevice) -> None:
    monkeypatch.setattr(crawl.emulator, "adb", device.adb)
    monkeypatch.setattr(crawl.emulator, "adb_raw", device.adb_raw)
    monkeypatch.setattr(crawl.emulator, "launch", device.launch)


def test_walk_covers_all_states_via_backtracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    screens: dict[str, dict[str, object]] = {
        "menu": {
            "package": APP,
            "activity": "Menu",
            "back": "home",
            "elements": [
                ("id/btn_a", "A", "[0,0][100,100]", "sa"),
                ("id/btn_b", "B", "[0,100][100,200]", "sb"),
            ],
        },
        "sa": {
            "package": APP,
            "activity": "ScreenA",
            "back": "menu",
            "elements": [("id/btn_c", "C", "[0,0][100,100]", "sc")],
        },
        "sc": {"package": APP, "activity": "ScreenC", "back": "sa", "elements": []},
        "sb": {"package": APP, "activity": "ScreenB", "back": "menu", "elements": []},
        "home": {"package": LAUNCHER, "activity": "Home", "back": "home", "elements": []},
    }
    device = FakeDevice("menu", screens)
    _install(monkeypatch, device)

    paths = RunPaths.create(tmp_path)
    result = crawl.walk(paths, APP, max_screens=40)

    activities = {s["activity"].split("/")[-1] for s in result["screens"]}  # type: ignore[union-attr]
    assert {"Menu", "ScreenA", "ScreenB", "ScreenC"} <= activities
    assert result["screen_count"] == 4
    saved = {p.name for p in (paths.screens_dir).glob("*.png")}
    assert len(saved) == 4

    by_act = {s["activity"].split("/")[-1]: s for s in result["screens"]}  # type: ignore[union-attr]
    assert by_act["ScreenB"]["from"] == by_act["Menu"]["id"]
    assert by_act["ScreenB"]["tapped"]["text"] == "B"  # type: ignore[index]


def test_walk_dismisses_anr_dialog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    screens: dict[str, dict[str, object]] = {
        "menu_anr": {
            "package": APP,
            "activity": "Menu",
            "back": "home",
            "elements": [
                ("android:id/aerr_wait", "Wait", "[0,0][100,100]", "menu"),
                ("id/btn_a", "A", "[0,100][100,200]", "sa"),
            ],
        },
        "menu": {
            "package": APP,
            "activity": "Menu",
            "back": "home",
            "elements": [("id/btn_a", "A", "[0,100][100,200]", "sa")],
        },
        "sa": {"package": APP, "activity": "ScreenA", "back": "menu", "elements": []},
        "home": {"package": LAUNCHER, "activity": "Home", "back": "home", "elements": []},
    }
    device = FakeDevice("menu_anr", screens)
    _install(monkeypatch, device)

    paths = RunPaths.create(tmp_path)
    result = crawl.walk(paths, APP, max_screens=40)

    every_rid = {
        e["resource_id"]
        for s in result["screens"]
        for e in s["elements"]  # type: ignore[union-attr]
    }
    assert not any("aerr" in rid for rid in every_rid)
    activities = {s["activity"].split("/")[-1] for s in result["screens"]}  # type: ignore[union-attr]
    assert {"Menu", "ScreenA"} <= activities
