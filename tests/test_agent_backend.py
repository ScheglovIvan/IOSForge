"""The coding-agent seam (mvp/agent_backend.py): claude as before, codex via a fake docker.

No real agent runs here: claude is replaced at ``agent_backend.subprocess.run``; codex runs
through a fake ``docker`` that answers ``info``/``image inspect``/``kill`` and, for ``run``,
plays the codex CLI inside the container: it records the docker and codex argv and stdin,
writes the result file into ``-C <ws>`` the way the real agent would, and prints chatter on
stdout that no caller may depend on. The real container is probed in
``test_agent_backend_isolation.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from iosforge.common.config import Settings
from iosforge.mvp import (
    ad_analysis,
    agent_backend,
    analyze,
    claude_gen,
    compliance,
    feasibility,
    screen_filter,
)
from iosforge.mvp.agent_backend import AgentResult
from tests.test_mvp_compliance import _WEIGHTS
from tests.test_mvp_compliance import _make_paths as _compliance_paths


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "CODEGEN_AGENT",
        "CLAUDE_BIN",
        "CODEX_BIN",
        "CODEX_MODEL",
        "CODEX_REASONING_EFFORT",
        "CODEX_IGNORE_USER_CONFIG",
        "CODEX_IMAGE",
        "DOCKER_BIN",
    ):
        monkeypatch.delenv(key, raising=False)


class _Recorder:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.stdout, self.stderr = stdout, stderr

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=self.stdout, stderr=self.stderr)


# --- claude: unchanged argv and subprocess.run call -------------------------------------


@pytest.mark.parametrize(
    ("tools", "extra"),
    [(None, []), ("Read Write Glob", ["--allowed-tools", "Read Write Glob"])],
)
def test_claude_runs_the_same_command_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: str | None, extra: list[str]
) -> None:
    rec = _Recorder(stdout="out", stderr="err")
    monkeypatch.setattr(agent_backend.subprocess, "run", rec)

    result = agent_backend.run_agent(
        tmp_path, "the prompt", timeout=42, allowed_tools=tools, settings=_settings()
    )

    assert rec.calls == [
        (
            ["claude", "-p", "the prompt", "--permission-mode", "acceptEdits", *extra],
            {"cwd": tmp_path, "capture_output": True, "text": True, "timeout": 42, "check": False},
        )
    ]
    assert result == AgentResult("claude", 0, "out", "err", None)


def test_run_task_writes_the_same_transcript_for_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_backend, "get_settings", lambda: _settings())
    monkeypatch.setattr(agent_backend.subprocess, "run", _Recorder(stdout="o" * 9000, stderr="e"))

    claude_gen.run_task(tmp_path, "p", timeout=5, tlog=claude_gen.get_logger("t"))

    assert (tmp_path / claude_gen.TASK_LOG).read_text() == f"{'o' * 8000}\n[stderr]\ne"
    assert (tmp_path / "TASK.md").read_text() == "p"


def test_claude_never_parsed_stdout_so_tasks_do_not_need_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_backend.subprocess, "run", _Recorder(stdout="not json {"))
    result = agent_backend.run_agent(tmp_path, "p", timeout=1, settings=_settings())
    assert result.returncode == 0 and result.last_message is None


# --- codex: command, stdin prompt, last message -----------------------------------------


def test_codex_command_for_this_cli(tmp_path: Path) -> None:
    """Pinned to Codex CLI 0.153, where exec no longer accepts --full-auto."""
    last = tmp_path / "last.txt"
    cmd = agent_backend.codex_command(_settings(), tmp_path, "Read Write Glob", last)
    assert cmd == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        'shell_environment_policy.inherit="core"',
        "-m",
        "gpt-6-astra",
        "--ignore-user-config",
        "-C",
        str(tmp_path),
        "-o",
        str(last),
        "-",
    ]
    assert "--full-auto" not in cmd


def test_codex_command_web_search_effort_and_user_config(tmp_path: Path) -> None:
    settings = _settings(codex_reasoning_effort="high", codex_ignore_user_config=False)
    cmd = agent_backend.codex_command(settings, tmp_path, analyze.ANALYZE_TOOLS, tmp_path / "m")
    assert 'web_search="live"' in cmd
    assert cmd[cmd.index('model_reasoning_effort="high"') - 1] == "-c"
    assert "--ignore-user-config" not in cmd
    empty = agent_backend.codex_command(
        _settings(codex_reasoning_effort=""), tmp_path, None, tmp_path / "m"
    )
    assert not any("model_reasoning_effort" in part for part in empty)


def test_run_task_transcript_falls_back_to_codex_last_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(*a: Any, **k: Any) -> AgentResult:
        return AgentResult("codex", 0, "", "warn", "final words")

    monkeypatch.setattr(agent_backend, "run_agent", fake)
    claude_gen.run_task(tmp_path, "p", timeout=5, tlog=claude_gen.get_logger("t"))
    assert claude_gen.task_tail(tmp_path).startswith("final words")


def test_every_model_task_goes_through_the_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def fake(ws: Path, prompt: str, **kwargs: Any) -> AgentResult:
        seen.append(kwargs.get("allowed_tools"))
        return AgentResult("claude", 0, "", "")

    monkeypatch.setattr(agent_backend, "run_agent", fake)
    claude_gen.run_task(tmp_path, "p", timeout=1, tlog=claude_gen.get_logger("t"))
    analyze._run_claude("p", tmp_path, 1, tools=analyze.ANALYZE_TOOLS)
    feasibility._run_claude("p", tmp_path, 1, tools=feasibility.SCOPE_TOOLS)
    ad_analysis._run_claude("p", tmp_path, 1)
    screen_filter._run_claude("p", tmp_path, 1)
    with pytest.raises(RuntimeError, match="judge.json"):
        compliance.evaluate(
            _compliance_paths(tmp_path / "c"), 1, weights=_WEIGHTS, threshold=0.9, soft_floor=0.8,
            history=[],
        )  # fmt: skip
    assert seen == [
        None,
        analyze.ANALYZE_TOOLS,
        feasibility.SCOPE_TOOLS,
        ad_analysis.AD_TOOLS,
        screen_filter.CLASSIFY_TOOLS,
        None,
    ]
    for module in (claude_gen, analyze, feasibility, ad_analysis, screen_filter, compliance):
        assert not hasattr(module, "CLAUDE_BIN") and not hasattr(module, "subprocess")


def test_a_failing_agent_names_its_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_backend, "run_agent", lambda *a, **k: AgentResult("codex", 2, "", "boom")
    )
    with pytest.raises(RuntimeError, match="codex CLI exited 2: boom"):
        analyze._run_claude("p", tmp_path, 1)


# --- choosing the backend ----------------------------------------------------------------


def test_default_backend_is_claude() -> None:
    assert _settings().codegen_agent == "claude"
    assert agent_backend.resolve_backend(settings=_settings()) == "claude"


def test_env_selects_codex_and_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEGEN_AGENT", "codex")
    assert agent_backend.resolve_backend(settings=_settings()) == "codex"
    monkeypatch.setenv("CODEGEN_AGENT", "astra")
    with pytest.raises(ValidationError):
        _settings()


def test_argument_beats_override_beats_settings() -> None:
    settings = _settings(codegen_agent="claude")
    with agent_backend.override_backend("codex"):
        assert agent_backend.resolve_backend(settings=settings) == "codex"
        assert agent_backend.resolve_backend("claude", settings) == "claude"
        with agent_backend.override_backend(None):
            assert agent_backend.resolve_backend(settings=settings) == "claude"
        assert agent_backend.resolve_backend(settings=settings) == "codex"
    assert agent_backend.resolve_backend(settings=settings) == "claude"


def test_reasoning_effort_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "")
    assert _settings().codex_reasoning_effort is None
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")
    assert _settings().codex_reasoning_effort == "xhigh"
    monkeypatch.setenv("CODEX_REASONING_EFFORT", 'high" -c x="y')
    with pytest.raises(ValidationError):
        _settings()
