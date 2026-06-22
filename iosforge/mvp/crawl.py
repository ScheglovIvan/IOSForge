"""Minimal adb-based UI crawler: screenshot + uiautomator dump, tap, repeat.

Produces screens/NNNN.png and screens.json (a flat screen list with "next" links).
Deliberately simple (no state graph): dedup screens by UI signature, tap the first
not-yet-tapped clickable element, follow where it leads.
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


def _ui_xml() -> str:
    emulator.adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    return emulator.adb("exec-out", "cat", "/sdcard/ui.xml").stdout


def _current_activity() -> str:
    out = emulator.adb("shell", "dumpsys", "activity", "activities").stdout
    m = re.search(r"mResumedActivity.*\{[^}]*\s(\S+/\S+)", out)
    return m.group(1) if m else "unknown"


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
        out.append({
            "text": node.get("text", ""),
            "resource_id": node.get("resource-id", ""),
            "content_desc": node.get("content-desc", ""),
            "bounds": bounds,
        })
    return out


def _signature(elements: list[dict[str, str]]) -> str:
    key = "|".join(sorted(f"{e['resource_id']}:{e['text']}" for e in elements))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def walk(paths: RunPaths, package: str, max_screens: int) -> dict[str, object]:
    screens: list[dict[str, object]] = []
    tapped: set[str] = set()  # signature::element-key already tapped
    prev_id: str | None = None

    for i in range(max_screens):
        sid = f"{i:04d}"
        png = paths.screens_dir / f"{sid}.png"
        png.write_bytes(emulator.adb_raw("exec-out", "screencap", "-p"))
        xml = _ui_xml()
        elements = _clickables(xml)
        sig = _signature(elements)
        entry: dict[str, object] = {
            "id": sid,
            "screenshot": f"screens/{sid}.png",
            "activity": _current_activity(),
            "signature": sig,
            "elements": elements,
            "from": prev_id,
        }
        screens.append(entry)
        log.info("crawl.screen", id=sid, activity=entry["activity"], clickables=len(elements))

        target = None
        for e in elements:
            ekey = f"{sig}::{e['resource_id']}:{e['text']}:{e['bounds']}"
            if ekey not in tapped:
                tapped.add(ekey)
                target = e
                break
        if target is None:
            log.info("crawl.exhausted", id=sid)
            break
        center = _center(target["bounds"])
        assert center is not None
        entry["tapped"] = {"resource_id": target["resource_id"], "text": target["text"]}
        emulator.adb("shell", "input", "tap", str(center[0]), str(center[1]))
        time.sleep(2)
        prev_id = sid

    result = {"package": package, "screen_count": len(screens), "screens": screens}
    paths.screens_json.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info("crawl.done", screens=len(screens))
    return result
