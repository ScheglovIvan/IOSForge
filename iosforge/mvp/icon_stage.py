"""Put the chosen app icon into the Flutter project so the build picks it up.

The icon is generated and chosen separately from the app (see
:mod:`iosforge.mvp.app_icon`), so something has to carry it into the project the
build compiles. That is this module, and it does two things:

* writes the PNG to ``assets/icon/app_icon.png`` and registers ``assets/icon/``
  in the pubspec, which is what lets Dart code show the mark on the splash, the
  paywall header or an about screen;
* adds the ``flutter_launcher_icons`` dev dependency and its configuration, which
  the CI step runs to produce every iOS/Android launcher size from that one file.

Both are idempotent: staging the same project twice leaves it unchanged, so a
rebuild after an icon re-roll simply swaps the PNG.
"""

from __future__ import annotations

from pathlib import Path

from iosforge.common.logging import get_logger

log = get_logger("mvp.icon_stage")

ICON_PATH = "assets/icon/app_icon.png"
ASSET_DIR = "assets/icon/"
LAUNCHER_PACKAGE = "flutter_launcher_icons"
LAUNCHER_VERSION = "^0.14.1"

# `remove_alpha_ios` matters: App Store Connect rejects an icon with an alpha
# channel, and the tool copies whatever it is given unless told to flatten.
LAUNCHER_CONFIG = f"""
{LAUNCHER_PACKAGE}:
  image_path: "{ICON_PATH}"
  remove_alpha_ios: true
  ios: true
  android: true
  min_sdk_android: 21
"""


def write_icon(flutter_app: Path, data: bytes) -> Path:
    """Write the icon into the project and return its path."""
    target = flutter_app / ICON_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def register_asset(pubspec: str) -> str:
    """Ensure ``assets/icon/`` is listed under the flutter assets block."""
    if ASSET_DIR in pubspec:
        return pubspec
    lines = pubspec.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "assets:":
            indent = " " * (len(line) - len(line.lstrip()) + 2)
            lines.insert(index + 1, f"{indent}- {ASSET_DIR}")
            return "\n".join(lines) + "\n"
    return pubspec


def register_launcher(pubspec: str) -> str:
    """Ensure the launcher-icon tool is a dev dependency and is configured."""
    text = pubspec
    if f"  {LAUNCHER_PACKAGE}:" not in text:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.strip() == "dev_dependencies:":
                lines.insert(index + 1, f"  {LAUNCHER_PACKAGE}: {LAUNCHER_VERSION}")
                text = "\n".join(lines) + "\n"
                break
    if f"\n{LAUNCHER_PACKAGE}:" not in text:
        text = text.rstrip() + "\n" + LAUNCHER_CONFIG
    return text


def stage(flutter_app: Path, data: bytes) -> bool:
    """Stage the icon and wire the project up for it; ``False`` when not applicable."""
    if not data:
        return False
    pubspec = flutter_app / "pubspec.yaml"
    if not pubspec.is_file():
        log.warning("icon_stage.no_pubspec", app=str(flutter_app))
        return False

    write_icon(flutter_app, data)
    original = pubspec.read_text(encoding="utf-8")
    updated = register_launcher(register_asset(original))
    if updated != original:
        pubspec.write_text(updated, encoding="utf-8")
    log.info("icon_stage.staged", app=str(flutter_app), bytes=len(data))
    return True
