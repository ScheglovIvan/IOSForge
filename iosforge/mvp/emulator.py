"""Thin adb/emulator wrapper: boot a local AVD, install an APK, launch it.

Hardcoded to the local SDK at $ANDROID_SDK_ROOT (default /opt/android-sdk) and the
AVD named "mvp". No abstraction — phase-1 vertical only.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from iosforge.common.logging import get_logger

log = get_logger("mvp.emulator")

SDK_ROOT = Path(os.environ.get("ANDROID_SDK_ROOT", "/opt/android-sdk"))
ADB = str(SDK_ROOT / "platform-tools" / "adb")
EMULATOR = str(SDK_ROOT / "emulator" / "emulator")


def adb(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ADB, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def adb_raw(*args: str, timeout: int = 120) -> bytes:
    """Binary adb call (for screencap / file pulls that must not be text-decoded)."""
    return subprocess.run(
        [ADB, *args], capture_output=True, timeout=timeout, check=False
    ).stdout


def _emulator_running() -> bool:
    out = adb("devices").stdout
    return any(line.strip().endswith("device") for line in out.splitlines()[1:])


def start_emulator(avd: str = "mvp") -> subprocess.Popen[bytes] | None:
    """Launch the AVD headless if no device is already attached."""
    if _emulator_running():
        log.info("emulator.already_running")
        return None
    log.info("emulator.starting", avd=avd)
    proc = subprocess.Popen(
        [EMULATOR, "-avd", avd, "-no-window", "-no-audio", "-no-boot-anim",
         "-gpu", "swiftshader_indirect", "-no-snapshot"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


def wait_for_boot(timeout: int = 300) -> None:
    """Block until the device reports sys.boot_completed=1."""
    adb("wait-for-device", timeout=timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if adb("shell", "getprop", "sys.boot_completed").stdout.strip() == "1":
            adb("shell", "input", "keyevent", "82")  # dismiss keyguard
            log.info("emulator.booted")
            return
        time.sleep(3)
    raise TimeoutError("emulator did not finish booting in time")


def apk_package_name(apk: Path) -> str:
    """Read the package id straight from the APK manifest.

    Robust to re-installs (an already-present package yields no "new package"
    when diffing `pm list packages`, which previously broke detection).
    """
    from pyaxmlparser import APK as _APK  # type: ignore[import-untyped]

    return str(_APK(str(apk)).package)


def _is_xapk(path: Path) -> bool:
    """True if the file is an XAPK bundle (a ZIP that contains .apk parts)."""
    try:
        with zipfile.ZipFile(path) as zf:
            return any(n.lower().endswith(".apk") for n in zf.namelist())
    except zipfile.BadZipFile:
        return False


def install_apk(apk: Path) -> str:
    """Install an APK or XAPK bundle; return the package id.

    Detection is by content (not filename): a ZIP containing ``.apk`` members is
    an XAPK (base + splits) installed via ``adb install-multiple``; otherwise a
    single APK via ``adb install``.
    """
    if _is_xapk(apk):
        return _install_xapk(apk)
    package = apk_package_name(apk)
    res = adb("install", "-r", "-g", str(apk), timeout=300)
    if "Success" not in res.stdout:
        raise RuntimeError(f"adb install failed: {res.stdout}{res.stderr}")
    log.info("emulator.installed", package=package)
    return package


def _install_xapk(xapk: Path) -> str:
    """Extract an XAPK's APK parts and install them together (install-multiple)."""
    tmp = Path(tempfile.mkdtemp(prefix="xapk-"))
    apks: list[Path] = []
    package: str | None = None
    with zipfile.ZipFile(xapk) as zf:
        names = zf.namelist()
        if "manifest.json" in names:
            try:
                package = str(json.loads(zf.read("manifest.json"))["package_name"])
            except Exception:
                package = None
        for name in names:
            if name.lower().endswith(".apk"):
                dest = tmp / Path(name).name
                dest.write_bytes(zf.read(name))
                apks.append(dest)
    if not apks:
        raise RuntimeError("xapk bundle contains no .apk parts")
    if package is None:  # fall back to reading the base (largest) apk's manifest
        package = apk_package_name(max(apks, key=lambda p: p.stat().st_size))
    res = adb("install-multiple", "-r", "-g", *[str(a) for a in apks], timeout=600)
    if "Success" not in res.stdout:
        raise RuntimeError(f"adb install-multiple failed: {res.stdout}{res.stderr}")
    log.info("emulator.installed_xapk", package=package, parts=len(apks))
    return package


def launch(package: str) -> None:
    adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(3)
    log.info("emulator.launched", package=package)
