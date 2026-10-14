"""The one place that starts the coding agent for a model task.

:func:`run_agent` runs the agent non-interactively in a workspace ``ws`` with a prompt and
lets it edit files right there. Every caller reads its result from files in ``ws``
(``app_spec.json``, ``judge.json``, the Swift sources ...), never from stdout, so both
backends return the same :class:`AgentResult` and stdout is kept only for diagnostics.

``claude`` runs the Claude Code CLI exactly as the pipeline always has. ``codex`` runs the
Codex CLI (OpenAI models such as ``gpt-6-astra``) in its ``workspace-write`` sandbox. The
backend comes from the ``backend`` argument, then :func:`override_backend`, then
``Settings.codegen_agent``. Prompts are passed through unchanged.

codex has no tool whitelist. Its sandbox lets it write only inside ``ws``; it can still read
the filesystem, so it gets only the core environment variables (no database URL or admin
password from the worker). WebSearch/WebFetch map to its live web search; fetching an
arbitrary URL is not possible because the sandbox has no network.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, get_args

import structlog

from iosforge.common.config import CodegenAgent, Settings, get_settings

AgentBackend = CodegenAgent
AGENT_BACKENDS: Final[tuple[AgentBackend, ...]] = get_args(AgentBackend)
WEB_TOOLS: Final = frozenset({"WebSearch", "WebFetch"})

_override: AgentBackend | None = None


@dataclass(frozen=True, slots=True)
class AgentResult:
    backend: AgentBackend
    returncode: int
    stdout: str
    stderr: str
    last_message: str | None = None


@contextmanager
def override_backend(backend: AgentBackend | None) -> Iterator[None]:
    """Run the enclosed tasks on ``backend`` (``None`` keeps the configured one).

    A process-wide value rather than a ContextVar, so the screen tasks that swiftui_gen
    runs in a ThreadPoolExecutor see it too. Correct for prefork workers (one task per
    process); not for a threaded Celery pool.
    """
    global _override
    previous = _override
    _override = backend
    try:
        yield
    finally:
        _override = previous


def resolve_backend(
    backend: AgentBackend | None = None, settings: Settings | None = None
) -> AgentBackend:
    if backend is not None:
        return backend
    if _override is not None:
        return _override
    return (settings or get_settings()).codegen_agent


def claude_command(claude_bin: str, prompt: str, allowed_tools: str | None) -> list[str]:
    cmd = [claude_bin, "-p", prompt, "--permission-mode", "acceptEdits"]
    if allowed_tools:
        cmd += ["--allowed-tools", allowed_tools]
    return cmd


def codex_command(
    settings: Settings, ws: Path, allowed_tools: str | None, last_message_path: Path
) -> list[str]:
    """``codex exec`` reading the prompt from stdin (``-``).

    Web search is on only when the caller would have allowed WebSearch/WebFetch.
    """
    web = "live" if WEB_TOOLS & set((allowed_tools or "").split()) else "disabled"
    cmd = [
        settings.codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "-c",
        f'web_search="{web}"',
        "-c",
        'shell_environment_policy.inherit="core"',
        "-m",
        settings.codex_model,
    ]
    if settings.codex_reasoning_effort:
        cmd += ["-c", f'model_reasoning_effort="{settings.codex_reasoning_effort}"']
    if settings.codex_ignore_user_config:
        cmd.append("--ignore-user-config")
    cmd += ["-C", str(ws), "-o", str(last_message_path), "-"]
    return cmd


def run_agent(
    ws: Path,
    prompt: str,
    *,
    timeout: int,
    allowed_tools: str | None = None,
    backend: AgentBackend | None = None,
    tlog: structlog.stdlib.BoundLogger | None = None,
    settings: Settings | None = None,
) -> AgentResult:
    """Run one agent task in ``ws``; never raises on a non-zero exit (callers decide).

    ``allowed_tools`` is the Claude ``--allowed-tools`` value; codex maps it to web search
    on/off. ``subprocess.TimeoutExpired`` propagates as before. codex's last message is
    written by the CLI itself (not the sandboxed agent) into a temporary directory outside
    ``ws``, so nothing extra lands in workspaces that get copied into sandboxes or artifacts.
    """
    settings = settings or get_settings()
    chosen = resolve_backend(backend, settings)
    if chosen == "claude":
        res = subprocess.run(
            claude_command(settings.claude_bin, prompt, allowed_tools),
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        result = AgentResult("claude", res.returncode, res.stdout, res.stderr)
    else:
        with tempfile.TemporaryDirectory(prefix="iosforge-codex-") as tmp:
            last = Path(tmp) / "last_message.txt"
            res = subprocess.run(
                codex_command(settings, ws, allowed_tools, last),
                cwd=ws,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            message = last.read_text(encoding="utf-8") if last.is_file() else None
        result = AgentResult("codex", res.returncode, res.stdout, res.stderr, message)
    if tlog is not None:
        tlog.debug("agent.run", backend=chosen, code=result.returncode)
    return result
