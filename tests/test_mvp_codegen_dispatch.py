"""Tests for the Stage 3 codegen dispatcher (orchestrator selection)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import codegen


def _settings(orchestrator: str, max_parallel: int = 4) -> Settings:
    return Settings(codegen_orchestrator=orchestrator, codegen_max_parallel=max_parallel)


def test_dispatch_default_claude_uses_task_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        codegen.claude_gen, "generate_from_tasks", lambda paths: calls.setdefault("task", paths)
    )
    codegen.generate(tmp_path, _settings("claude"))  # type: ignore[arg-type]
    assert "task" in calls


def test_dispatch_hermes_uses_hermes_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        codegen.hermes_codegen,
        "generate_via_hermes",
        lambda paths, max_parallel: calls.setdefault("hermes", max_parallel),
    )
    codegen.generate(tmp_path, _settings("hermes", max_parallel=7))  # type: ignore[arg-type]
    assert calls["hermes"] == 7


@pytest.mark.parametrize("value", ["cloud", "claude-orchestrator", "claude-agent"])
def test_dispatch_cloud_aliases_use_claude_orchestrator(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        codegen.hermes_codegen,
        "generate_via_claude_orchestrator",
        lambda paths, max_parallel: calls.setdefault("cloud", max_parallel),
    )
    codegen.generate(tmp_path, _settings(value, max_parallel=3))  # type: ignore[arg-type]
    assert calls["cloud"] == 3
