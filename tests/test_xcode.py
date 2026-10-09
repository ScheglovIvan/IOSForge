"""Apple toolchain wrapper: error parsing and command shapes (subprocess mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import xcode


class _Recorder:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def test_parse_errors_relativises_and_dedupes(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    out = (
        f"{root}/App/Features/0011/Screen0011View.swift:12:5: error: cannot find 'Foo' in scope\n"
        f"{root}/App/Features/0011/Screen0011View.swift:12:5: error: cannot find 'Foo' in scope\n"
        f"{root}/App/Theme/Theme.swift:3:1: warning: unused\n"
        "xcodebuild: error: Unable to find a destination\n"
        "** BUILD FAILED **\n"
    )
    assert xcode.parse_errors(out, tmp_path) == [
        "App/Features/0011/Screen0011View.swift:12:5: error: cannot find 'Foo' in scope",
        "xcodebuild: error: Unable to find a destination",
    ]


def test_build_uses_generic_simulator_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Demo.xcodeproj").mkdir()
    rec = _Recorder(stdout="** BUILD SUCCEEDED **")
    monkeypatch.setattr(xcode.subprocess, "run", rec)

    outcome = xcode.build(tmp_path, "Demo", derived_data=tmp_path / "dd")

    assert outcome.ok and outcome.errors == []
    cmd, kw = rec.calls[0]
    assert cmd[:2] == ["xcodebuild", "build"]
    assert cmd[cmd.index("-destination") + 1] == "generic/platform=iOS Simulator"
    assert cmd[cmd.index("-scheme") + 1] == "Demo"
    assert "CODE_SIGNING_ALLOWED=NO" in cmd
    assert kw["cwd"] == tmp_path


def test_build_failure_without_parsable_errors_still_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Demo.xcodeproj").mkdir()
    monkeypatch.setattr(xcode.subprocess, "run", _Recorder(returncode=65, stdout="boom"))
    outcome = xcode.build(tmp_path, "Demo", derived_data=tmp_path / "dd")
    assert not outcome.ok
    assert outcome.errors and "exited 65" in outcome.errors[0]


def test_build_requires_generated_project(tmp_path: Path) -> None:
    with pytest.raises(xcode.XcodeError, match="no .xcodeproj"):
        xcode.build(tmp_path, "Demo", derived_data=tmp_path / "dd")


def test_generate_project_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xcode.subprocess, "run", _Recorder(returncode=1, stderr="bad yaml"))
    with pytest.raises(xcode.XcodeError, match="bad yaml"):
        xcode.generate_project(tmp_path)


def test_launch_screen_is_the_contract_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(xcode.subprocess, "run", rec)
    xcode.launch_screen("UDID", "com.example.app", "0011")
    assert rec.calls[0][0] == [
        "xcrun", "simctl", "launch", "--terminate-running-process",
        "UDID", "com.example.app", "-screen-id", "0011",
    ]  # fmt: skip


def test_simctl_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xcode.subprocess, "run", _Recorder(returncode=149, stderr="not booted"))
    with pytest.raises(xcode.XcodeError, match="not booted"):
        xcode.install("UDID", Path("/tmp/App.app"))


def test_toolchain_available_needs_every_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    for missing in ("xcodegen", "xcodebuild", "xcrun"):
        monkeypatch.setattr(
            xcode.shutil, "which", lambda name, m=missing: None if name == m else "/x"
        )
        assert not xcode.toolchain_available()
    monkeypatch.setattr(xcode.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert xcode.toolchain_available()


def test_build_timeout_becomes_a_gate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Demo.xcodeproj").mkdir()

    def slow(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(xcode.subprocess, "run", slow)
    outcome = xcode.build(tmp_path, "Demo", derived_data=tmp_path / "dd", timeout=5)
    assert not outcome.ok and outcome.errors == ["xcodebuild timed out after 5s"]


def test_missing_binary_raises_xcode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(xcode.subprocess, "run", missing)
    with pytest.raises(xcode.XcodeError, match="xcrun not found"):
        xcode.boot("UDID")
