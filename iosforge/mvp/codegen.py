"""Stage 3 codegen dispatcher: pick the orchestrator backend from settings.

``settings.codegen_orchestrator`` selects how ``flutter_app/`` is built, all from
the same staged contract (app_spec.json + tasks.json + handoff/ + screens/):

- ``"claude"`` (default): single-pass task runner — a Python loop drives the local
  Claude CLI task-by-task (:func:`iosforge.mvp.claude_gen.generate_from_tasks`).
- ``"hermes"``: Hermes Agent orchestrates, the local Claude CLI executes
  (:func:`iosforge.mvp.hermes_codegen.generate_via_hermes`).
- ``"cloud"`` (aliases ``"claude-orchestrator"`` / ``"claude-agent"``): the local
  Claude Code agent orchestrates AND builds directly
  (:func:`iosforge.mvp.hermes_codegen.generate_via_claude_orchestrator`).

Keeping the dispatch in one place lets the orchestrator be flipped via config/env
(e.g. back to ``"hermes"``) without touching the call sites.
"""

from __future__ import annotations

from pathlib import Path

from iosforge.common.config import Settings, get_settings
from iosforge.common.logging import get_logger
from iosforge.mvp import claude_gen, hermes_codegen
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.codegen")

_CLAUDE_ORCHESTRATOR_ALIASES = frozenset({"cloud", "claude-orchestrator", "claude-agent"})


def generate(paths: RunPaths, settings: Settings | None = None) -> Path:
    """Run Stage 3 codegen with the orchestrator selected in settings."""
    settings = settings or get_settings()
    orchestrator = settings.codegen_orchestrator
    log.info("codegen.dispatch", orchestrator=orchestrator)
    if orchestrator == "hermes":
        return hermes_codegen.generate_via_hermes(paths, max_parallel=settings.codegen_max_parallel)
    if orchestrator in _CLAUDE_ORCHESTRATOR_ALIASES:
        return hermes_codegen.generate_via_claude_orchestrator(
            paths, max_parallel=settings.codegen_max_parallel
        )
    return claude_gen.generate_from_tasks(paths)
