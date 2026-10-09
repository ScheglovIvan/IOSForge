"""Thin, mockable wrapper over the Apple toolchain (XcodeGen, xcodebuild, simctl).

Every subprocess the SwiftUI codegen and its compile gate run goes through this
module, so Linux CI tests patch ``xcode.subprocess.run`` (or the helpers) and the
real toolchain is only exercised on the Mac worker. A missing toolchain makes
:func:`build_errors` a no-op, mirroring ``claude_gen.analyze_errors`` for Flutter.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from iosforge.common.logging import get_logger

log = get_logger("mvp.xcode")

XCODEGEN_BIN = "xcodegen"
XCODEBUILD_BIN = "xcodebuild"
XCRUN_BIN = "xcrun"
SIMULATOR_DESTINATION = "generic/platform=iOS Simulator"

_COMPILER_ERROR = re.compile(r"^(?P<loc>/[^:\n]+:\d+(?::\d+)?): error: (?P<msg>.+)$")
_TOOL_ERROR = re.compile(r"^(?:xcodebuild|error|ld|clang): (?:error: )?(?P<msg>.+)$")


class XcodeError(RuntimeError):
    """A toolchain step (project generation, install, launch) failed."""


@dataclass(frozen=True)
class BuildOutcome:
    """Result of one ``xcodebuild build``: success flag, parsed errors, full log."""

    ok: bool
    errors: list[str]
    log: str


def toolchain_available() -> bool:
    """True when XcodeGen, xcodebuild and xcrun are on PATH (i.e. running on a Mac worker)."""
    return all(shutil.which(tool) is not None for tool in (XCODEGEN_BIN, XCODEBUILD_BIN, XCRUN_BIN))


def _run(
    cmd: list[str], *, cwd: Path | None = None, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise XcodeError(f"{cmd[0]} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise XcodeError(f"{cmd[0]} not found on PATH") from exc


def generate_project(app_dir: Path) -> None:
    """Run XcodeGen on ``app_dir/project.yml``; raise :class:`XcodeError` on failure."""
    res = _run([XCODEGEN_BIN, "generate", "--quiet"], cwd=app_dir, timeout=300)
    if res.returncode != 0:
        raise XcodeError(f"xcodegen failed: {(res.stderr or res.stdout)[-2000:]}")


def parse_errors(output: str, app_dir: Path | None = None) -> list[str]:
    """Error lines from xcodebuild output, de-duplicated, paths relative to ``app_dir``."""
    root = f"{app_dir.resolve()}/" if app_dir is not None else None
    seen: dict[str, None] = {}
    for raw in output.splitlines():
        line = raw.strip()
        match = _COMPILER_ERROR.match(line)
        if match:
            loc = match.group("loc")
            if root and loc.startswith(root):
                loc = loc[len(root) :]
            seen.setdefault(f"{loc}: error: {match.group('msg')}", None)
        elif "error:" in line and _TOOL_ERROR.match(line) and "BUILD FAILED" not in line:
            seen.setdefault(line, None)
    return list(seen)


def build(
    app_dir: Path,
    scheme: str,
    *,
    derived_data: Path,
    timeout: int = 1800,
) -> BuildOutcome:
    """``xcodebuild build`` for the generic iOS Simulator destination, signing off."""
    project = next(app_dir.glob("*.xcodeproj"), None)
    if project is None:
        raise XcodeError(f"no .xcodeproj in {app_dir} (run generate_project first)")
    try:
        res = _run(
            [
                XCODEBUILD_BIN,
                "build",
                "-project",
                project.name,
                "-scheme",
                scheme,
                "-destination",
                SIMULATOR_DESTINATION,
                "-derivedDataPath",
                str(derived_data),
                "CODE_SIGNING_ALLOWED=NO",
            ],
            cwd=app_dir,
            timeout=timeout,
        )
    except XcodeError as exc:
        return BuildOutcome(ok=False, errors=[str(exc)], log="")
    output = f"{res.stdout}\n{res.stderr}"
    errors = parse_errors(output, app_dir)
    if res.returncode != 0 and not errors:
        errors = [f"xcodebuild exited {res.returncode}: {output.strip()[-1500:]}"]
    return BuildOutcome(ok=res.returncode == 0, errors=errors, log=output)


def built_app(derived_data: Path, scheme: str) -> Path:
    """Path of the simulator ``.app`` produced by :func:`build`."""
    return derived_data / "Build" / "Products" / "Debug-iphonesimulator" / f"{scheme}.app"


def simctl(*args: str, timeout: int = 120) -> str:
    """Run ``xcrun simctl <args>``; raise :class:`XcodeError` on a non-zero exit."""
    res = _run([XCRUN_BIN, "simctl", *args], timeout=timeout)
    if res.returncode != 0:
        raise XcodeError(f"simctl {args[0]} failed: {(res.stderr or res.stdout)[-1000:]}")
    return res.stdout


def boot(udid: str) -> None:
    """Boot ``udid`` (idempotent) and wait until it is fully booted."""
    simctl("bootstatus", udid, "-b", timeout=300)


def install(udid: str, app: Path) -> None:
    """Install the simulator ``.app`` bundle on ``udid``."""
    simctl("install", udid, str(app))


def launch_screen(
    udid: str, bundle_id: str, screen_id: str, *, extra_args: tuple[str, ...] = ()
) -> None:
    """Relaunch the app headlessly on ``screen_id`` (SCREEN_NAV_CONTRACT entry point).

    ``extra_args`` are appended app launch arguments (e.g. ``-AppleLanguages (en)``).
    """
    simctl(
        "launch",
        "--terminate-running-process",
        udid,
        bundle_id,
        "-screen-id",
        screen_id,
        *extra_args,
    )


def set_appearance(udid: str, appearance: str) -> None:
    """Pin the simulator's light/dark appearance (SIMULATOR_ENV_CONTRACT)."""
    simctl("ui", udid, "appearance", appearance)


def set_content_size(udid: str, size: str) -> None:
    """Pin the Dynamic Type content size category (e.g. ``large``, the iOS default)."""
    simctl("ui", udid, "content_size", size)


def screenshot(udid: str, out: Path) -> Path:
    """Capture the current simulator frame of ``udid`` as a PNG at ``out``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    simctl("io", udid, "screenshot", str(out))
    return out


def pin_status_bar(udid: str) -> None:
    """Fixed status bar so screenshots are comparable (SIMULATOR_ENV_CONTRACT)."""
    flags = {
        "--time": "9:41",
        "--batteryState": "charged",
        "--batteryLevel": "100",
        "--cellularBars": "4",
        "--wifiBars": "3",
        "--dataNetwork": "wifi",
    }
    simctl("status_bar", udid, "override", *(part for kv in flags.items() for part in kv))
