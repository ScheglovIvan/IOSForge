"""Thin, mockable wrapper over the Apple toolchain (XcodeGen, xcodebuild, simctl).

Every subprocess the SwiftUI codegen and its compile gate run goes through this
module, so Linux CI tests patch ``xcode.subprocess.run`` (or the helpers) and the
real toolchain is only exercised on the Mac worker. A missing toolchain makes
:func:`build_errors` a no-op.
"""

from __future__ import annotations

import os
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
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
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


_TEST_CASE = re.compile(r"Test Case '-\[[\w.]+ (test\w*)\]' (passed|failed)")
_TEST_FAILURE = re.compile(r"error: -\[[\w.]+ (test\w*)\] : (.+)")


@dataclass(frozen=True)
class UITestOutcome:
    """Result of an ``xcodebuild test`` run: which test methods passed or failed (and why)."""

    ok: bool
    passed: list[str]
    failed: dict[str, str]
    log: str


def parse_ui_tests(output: str) -> tuple[list[str], dict[str, str]]:
    """Passed test names and failed test name → first failure message, from xcodebuild output."""
    passed: list[str] = []
    failed: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for match in _TEST_FAILURE.finditer(output):
        reasons.setdefault(match.group(1), match.group(2).strip())
    for match in _TEST_CASE.finditer(output):
        name, verdict = match.group(1), match.group(2)
        if verdict == "passed":
            if name not in passed:
                passed.append(name)
        else:
            failed[name] = reasons.get(name, "test failed")
    return passed, failed


def run_ui_tests(
    app_dir: Path,
    scheme: str,
    *,
    udid: str,
    derived_data: Path,
    environment: dict[str, str] | None = None,
    only: list[str] | None = None,
    timeout: int = 1800,
) -> UITestOutcome:
    """``xcodebuild test`` of the scheme's UI tests on simulator ``udid``, signing off.

    ``environment`` values reach the test runner (``TEST_RUNNER_`` prefix), which forwards
    them to the app it launches; ``only`` limits the run to ``Target/Class/method`` ids.
    """
    project = next(app_dir.glob("*.xcodeproj"), None)
    if project is None:
        raise XcodeError(f"no .xcodeproj in {app_dir} (run generate_project first)")
    env = {**os.environ, **{f"TEST_RUNNER_{k}": v for k, v in (environment or {}).items()}}
    cmd = [
        XCODEBUILD_BIN,
        "test",
        "-project",
        project.name,
        "-scheme",
        scheme,
        "-destination",
        f"id={udid}",
        "-derivedDataPath",
        str(derived_data),
        "CODE_SIGNING_ALLOWED=NO",
        *[f"-only-testing:{test}" for test in only or []],
    ]
    try:
        res = _run(cmd, cwd=app_dir, timeout=timeout, env=env)
    except XcodeError as exc:
        return UITestOutcome(ok=False, passed=[], failed={"xcodebuild": str(exc)}, log="")
    output = f"{res.stdout}\n{res.stderr}"
    passed, failed = parse_ui_tests(output)
    if res.returncode != 0 and not failed:
        failed["xcodebuild"] = f"xcodebuild test exited {res.returncode}: {output.strip()[-1500:]}"
    return UITestOutcome(
        ok=res.returncode == 0 and not failed, passed=passed, failed=failed, log=output
    )


def app_data_container(udid: str, bundle_id: str) -> Path | None:
    """The app's data container on the simulator (None when the app is not installed)."""
    res = _run([XCRUN_BIN, "simctl", "get_app_container", udid, bundle_id, "data"], timeout=60)
    path = res.stdout.strip()
    return Path(path) if res.returncode == 0 and path else None


@dataclass(frozen=True)
class AuthKey:
    """App Store Connect API key used by xcodebuild for automatic signing."""

    key_path: Path
    key_id: str
    issuer_id: str

    def args(self) -> list[str]:
        return [
            "-allowProvisioningUpdates",
            "-authenticationKeyPath",
            str(self.key_path),
            "-authenticationKeyID",
            self.key_id,
            "-authenticationKeyIssuerID",
            self.issuer_id,
        ]


def _outcome(cmd: list[str], *, cwd: Path, timeout: int, app_dir: Path) -> BuildOutcome:
    try:
        res = _run(cmd, cwd=cwd, timeout=timeout)
    except XcodeError as exc:
        return BuildOutcome(ok=False, errors=[str(exc)], log="")
    output = f"{res.stdout}\n{res.stderr}"
    errors = parse_errors(output, app_dir)
    if res.returncode != 0 and not errors:
        errors = [f"{cmd[0]} {cmd[1]} exited {res.returncode}: {output.strip()[-1500:]}"]
    return BuildOutcome(ok=res.returncode == 0, errors=errors, log=output)


def archive(
    app_dir: Path,
    scheme: str,
    archive_path: Path,
    *,
    build_settings: dict[str, str],
    auth: AuthKey | None = None,
    timeout: int = 3600,
) -> BuildOutcome:
    """``xcodebuild archive`` for a generic iOS device with command-line build settings."""
    project = next(app_dir.glob("*.xcodeproj"), None)
    if project is None:
        raise XcodeError(f"no .xcodeproj in {app_dir} (run generate_project first)")
    cmd = [
        XCODEBUILD_BIN,
        "archive",
        "-project",
        project.name,
        "-scheme",
        scheme,
        "-destination",
        "generic/platform=iOS",
        "-archivePath",
        str(archive_path),
        *(auth.args() if auth else []),
        *(f"{key}={value}" for key, value in sorted(build_settings.items())),
    ]
    return _outcome(cmd, cwd=app_dir, timeout=timeout, app_dir=app_dir)


def export_archive(
    archive_path: Path,
    export_dir: Path,
    options_plist: Path,
    *,
    auth: AuthKey | None = None,
    timeout: int = 1800,
) -> BuildOutcome:
    """``xcodebuild -exportArchive`` into ``export_dir`` (signed IPA per ExportOptions)."""
    cmd = [
        XCODEBUILD_BIN,
        "-exportArchive",
        "-archivePath",
        str(archive_path),
        "-exportPath",
        str(export_dir),
        "-exportOptionsPlist",
        str(options_plist),
        *(auth.args() if auth else []),
    ]
    return _outcome(cmd, cwd=archive_path.parent, timeout=timeout, app_dir=archive_path.parent)


def upload_command(ipa: Path, auth: AuthKey) -> list[str]:
    """``altool`` upload of a signed IPA to App Store Connect (built, not executed here).

    altool finds the key as ``AuthKey_<key_id>.p8`` in ``API_PRIVATE_KEYS_DIR``, which
    :func:`iosforge.mvp.ios_delivery.upload` points at a private copy of ``key_path``.
    """
    return [
        XCRUN_BIN,
        "altool",
        "--upload-app",
        "--type",
        "ios",
        "--file",
        str(ipa),
        "--apiKey",
        auth.key_id,
        "--apiIssuer",
        auth.issuer_id,
    ]


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
