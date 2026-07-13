"""Compile gate: flutter analyze error parsing + bounded rework fix loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import claude_gen


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_analyze_errors_extracts_only_error_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "flutter_app"
    app.mkdir()
    out = (
        "Analyzing flutter_app...\n"
        "   error • Method not found: 'CupertinoPageTransitionsBuilder' "
        "• lib/ui/theme/app_theme.dart:67:35 • undefined_method\n"
        "   warning • unused import • lib/x.dart:1:1 • unused_import\n"
        "   info • 'activeColor' is deprecated • lib/y.dart:3:3 • deprecated_member_use\n"
    )
    monkeypatch.setattr(claude_gen.Path, "exists", lambda self: True)
    monkeypatch.setattr(claude_gen.subprocess, "run", lambda *a, **k: _Proc(out))
    errors = claude_gen.analyze_errors(app)
    assert len(errors) == 1
    assert "CupertinoPageTransitionsBuilder" in errors[0]


def test_ensure_compiles_runs_fix_loop_until_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = type("P", (), {"flutter_app": tmp_path / "flutter_app", "run_dir": tmp_path})()
    paths.flutter_app.mkdir()
    calls: dict[str, int] = {"analyze": 0, "rework": 0}

    def _analyze(_app: Path) -> list[str]:
        calls["analyze"] += 1
        return ["   error • boom • lib/a.dart:1:1 • x"] if calls["analyze"] == 1 else []

    def _rework(_p: Any, instructions: str, *, timeout: int = 1800) -> Path:
        calls["rework"] += 1
        assert "flutter analyze errors" in instructions
        return paths.flutter_app

    monkeypatch.setattr(claude_gen, "analyze_errors", _analyze)
    monkeypatch.setattr(claude_gen, "rework", _rework)
    remaining = claude_gen.ensure_compiles(paths, attempts=2)
    assert remaining == []
    assert calls["rework"] == 1


def test_ensure_compiles_stops_after_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = type("P", (), {"flutter_app": tmp_path / "flutter_app", "run_dir": tmp_path})()
    paths.flutter_app.mkdir()
    reworks = {"n": 0}
    monkeypatch.setattr(
        claude_gen, "analyze_errors", lambda _a: ["   error • still broken • lib/a.dart:1:1 • x"]
    )
    monkeypatch.setattr(
        claude_gen,
        "rework",
        lambda *a, **k: reworks.__setitem__("n", reworks["n"] + 1) or paths.flutter_app,
    )
    remaining = claude_gen.ensure_compiles(paths, attempts=2)
    assert len(remaining) == 1
    assert reworks["n"] == 2


def test_ensure_compiles_noop_without_flutter_app(tmp_path: Path) -> None:
    paths = type("P", (), {"flutter_app": tmp_path / "missing", "run_dir": tmp_path})()
    assert claude_gen.ensure_compiles(paths) == []
