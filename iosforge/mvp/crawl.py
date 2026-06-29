"""adb-based UI crawler with greedy state-graph exploration.

Explores an app the way DroidBot's greedy-BFS policy does: every distinct UI
state (keyed by a signature of its clickable elements) is a node; on each state
the first not-yet-tried clickable is tapped; when a state has no untried element
the crawler presses BACK to retreat and keep exploring other branches, instead
of stopping at the first dead end. System interruptions (ANR / permission /
crash dialogs) are dismissed, and the crawler keeps itself inside the target
package, relaunching if it drifts out. Termination is guaranteed: the set of
(signature, element) pairs is finite and each is tried at most once.

Produces screens/NNNN.png (one per distinct state) and screens.json — a flat
screen list keeping the legacy ``id/screenshot/activity/elements/from/tapped``
fields plus ``navigates_to`` edges, so the downstream analysis stays compatible.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET

from iosforge.common.logging import get_logger
from iosforge.mvp import emulator
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.crawl")

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_RESUMED_RE = re.compile(r"(?:mResumedActivity|topResumedActivity)\D*\{[^}]*\s(\S+)/(\S+)")

_PERMISSION_PKGS = frozenset(
    {
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
        "com.android.packageinstaller",
    }
)
_ALLOW_TEXTS = frozenset(
    {
        "allow",
        "while using the app",
        "allow only while using the app",
        "ok",
    }
)


def _ui_xml() -> str:
    emulator.adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    return emulator.adb("exec-out", "cat", "/sdcard/ui.xml").stdout


def _resumed() -> tuple[str, str]:
    out = emulator.adb("shell", "dumpsys", "activity", "activities").stdout
    m = _RESUMED_RE.search(out)
    return (m.group(1), m.group(2)) if m else ("", "")


def _current_activity() -> str:
    pkg, act = _resumed()
    return f"{pkg}/{act}" if pkg else "unknown"


def _foreground_package() -> str:
    return _resumed()[0]


def _center(bounds: str) -> tuple[int, int] | None:
    m = _BOUNDS_RE.search(bounds)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _clickables(xml: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        bounds = node.get("bounds", "")
        if _center(bounds) is None:
            continue
        out.append(
            {
                "text": node.get("text", ""),
                "resource_id": node.get("resource-id", ""),
                "content_desc": node.get("content-desc", ""),
                "bounds": bounds,
            }
        )
    return out


def _scrollable_bounds(xml: str) -> str | None:
    """Bounds of the first scrollable container, or None (for a centred swipe)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        if node.get("scrollable") == "true":
            bounds = node.get("bounds", "")
            if _center(bounds) is not None:
                return bounds
    return None


def _signature(elements: list[dict[str, str]]) -> str:
    key = "|".join(sorted(f"{e['resource_id']}:{e['text']}" for e in elements))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _tap(center: tuple[int, int]) -> None:
    emulator.adb("shell", "input", "tap", str(center[0]), str(center[1]))


def _press_back() -> None:
    emulator.adb("shell", "input", "keyevent", "4")


def _scroll_forward(bounds: str) -> None:
    center = _center(bounds)
    if center is None:
        return
    cx, cy = center
    emulator.adb("shell", "input", "swipe", str(cx), str(cy + 200), str(cx), str(cy - 200), "300")


def _dismiss_system_ui(elements: list[dict[str, str]], foreground: str) -> str | None:
    """Tap through an ANR / crash / permission dialog; return what was handled."""
    by_suffix: dict[str, str | None] = {"aerr_restart": None, "aerr_wait": None, "aerr_close": None}
    for element in elements:
        for suffix in by_suffix:
            if element["resource_id"].endswith(suffix):
                by_suffix[suffix] = element["bounds"]
    for kind in ("aerr_restart", "aerr_wait", "aerr_close"):
        bounds = by_suffix[kind]
        if bounds is not None:
            center = _center(bounds)
            if center is not None:
                _tap(center)
                return kind
    for element in elements:
        if "permission_allow" in element["resource_id"]:
            center = _center(element["bounds"])
            if center is not None:
                _tap(center)
                return "permission_allow"
    if foreground in _PERMISSION_PKGS:
        for element in elements:
            if element["text"].strip().lower() in _ALLOW_TEXTS:
                center = _center(element["bounds"])
                if center is not None:
                    _tap(center)
                    return "permission_text"
    return None


def _wait_for_package(package: str, timeout: float, poll: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _foreground_package() == package:
            return True
        time.sleep(poll)
    return False


class _State:
    __slots__ = ("id", "elements", "activity", "screenshot", "untried", "scrolled", "navigates_to")

    def __init__(self, sid: str, elements: list[dict[str, str]], activity: str) -> None:
        self.id = sid
        self.elements = elements
        self.activity = activity
        self.screenshot = f"screens/{sid}.png"
        self.untried = list(elements)
        self.scrolled = False
        self.navigates_to: list[dict[str, str]] = []


def walk(
    paths: RunPaths,
    package: str,
    max_screens: int,
    *,
    max_steps: int | None = None,
    settle_pause: float = 2.0,
    action_pause: float = 1.0,
    allow_scroll: bool = True,
) -> dict[str, object]:
    """Greedy state-graph crawl of ``package``; write and return screens.json."""
    if max_steps is None:
        max_steps = max(max_screens * 8, 60)

    _wait_for_package(package, timeout=20.0)

    graph: dict[str, _State] = {}
    order: list[str] = []
    sigs: dict[str, str] = {}
    from_id: dict[str, str] = {}
    tapped_into: dict[str, dict[str, str]] = {}
    next_id = 0
    pending: tuple[str, dict[str, str]] | None = None
    outside_streak = 0
    dead_backtracks = 0
    steps = 0

    while steps < max_steps and len(order) < max_screens:
        xml = _ui_xml()
        elements = _clickables(xml)
        pkg, act = _resumed()

        handled = _dismiss_system_ui(elements, pkg)
        if handled is not None:
            log.info("crawl.dialog", kind=handled)
            steps += 1
            pending = None
            time.sleep(action_pause)
            continue

        if pkg and pkg != package:
            outside_streak += 1
            log.info("crawl.left_app", package=pkg, streak=outside_streak)
            if outside_streak >= 3:
                emulator.launch(package)
                _wait_for_package(package, timeout=15.0)
                outside_streak = 0
            else:
                _press_back()
            pending = None
            steps += 1
            time.sleep(action_pause)
            continue
        outside_streak = 0

        activity = f"{pkg}/{act}" if pkg else "unknown"
        sig = _signature(elements)
        key = f"{activity}::{sig}"
        if key not in graph:
            sid = f"{next_id:04d}"
            next_id += 1
            png = paths.screens_dir / f"{sid}.png"
            png.write_bytes(emulator.adb_raw("exec-out", "screencap", "-p"))
            graph[key] = _State(sid, elements, activity)
            sigs[key] = sig
            order.append(key)
            log.info(
                "crawl.state",
                id=sid,
                activity=activity,
                clickables=len(elements),
                total=len(order),
            )
        node = graph[key]

        if pending is not None:
            prev_key, via = pending
            prev = graph[prev_key]
            if prev_key != key:
                edge = {"to": node.id, "resource_id": via["resource_id"], "text": via["text"]}
                if edge not in prev.navigates_to:
                    prev.navigates_to.append(edge)
                if node.id not in from_id:
                    from_id[node.id] = prev.id
                    tapped_into[node.id] = {"resource_id": via["resource_id"], "text": via["text"]}
            pending = None

        if node.untried:
            target = node.untried.pop(0)
            center = _center(target["bounds"])
            if center is None:
                continue
            _tap(center)
            pending = (key, target)
            dead_backtracks = 0
            steps += 1
            time.sleep(settle_pause)
            continue

        scroll_bounds = _scrollable_bounds(xml)
        if allow_scroll and not node.scrolled and scroll_bounds is not None:
            node.scrolled = True
            _scroll_forward(scroll_bounds)
            steps += 1
            time.sleep(action_pause)
            continue

        if not any(graph[k].untried for k in order):
            log.info("crawl.complete", states=len(order))
            break

        _press_back()
        dead_backtracks += 1
        steps += 1
        time.sleep(action_pause)
        if dead_backtracks >= 4:
            emulator.launch(package)
            _wait_for_package(package, timeout=15.0)
            dead_backtracks = 0
            pending = None

    screens: list[dict[str, object]] = []
    for key in order:
        node = graph[key]
        screens.append(
            {
                "id": node.id,
                "screenshot": node.screenshot,
                "activity": node.activity,
                "signature": sigs[key],
                "elements": node.elements,
                "from": from_id.get(node.id),
                "tapped": tapped_into.get(node.id),
                "navigates_to": node.navigates_to,
            }
        )

    result = {"package": package, "screen_count": len(screens), "screens": screens}
    paths.screens_json.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info("crawl.done", screens=len(screens), steps=steps)
    return result
