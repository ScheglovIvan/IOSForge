"""The local Claude Code CLI runner shared by every model task.

:func:`run_task` writes ``TASK.md`` and runs ``claude -p`` non-interactively in a
workspace, keeping the tail of the transcript (:data:`TASK_LOG`) so a no-op run can be
explained (:func:`task_tail`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import structlog

from iosforge.common.logging import get_logger

log = get_logger("mvp.claude_gen")

CLAUDE_BIN = "claude"


def run_task(
    workspace: Path,
    prompt: str,
    *,
    timeout: int,
    tlog: structlog.stdlib.BoundLogger,
) -> int:
    """Write TASK.md and run one `claude -p` task invocation in ``workspace``.

    Shared by the SwiftUI sandboxed screen tasks, the Vision Judge, its corrective
    rounds and the store-asset tasks. Returns the CLI return code; a non-zero code is
    logged as a warning (best-effort, the caller decides whether the workspace result
    is still valid).
    """
    (workspace / "TASK.md").write_text(prompt)
    res = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    # The CLI sometimes exits 0 having written nothing. Without its transcript there is
    # no way to tell a refusal from a crash, so keep the tail next to the workspace.
    try:
        (workspace / TASK_LOG).write_text(
            f"{res.stdout[-8000:]}\n[stderr]\n{res.stderr[-2000:]}", encoding="utf-8"
        )
    except OSError:
        pass
    if res.returncode != 0:
        tlog.warning("codegen_tasks.task.cli_failed", code=res.returncode, stderr=res.stderr[-500:])
    return res.returncode


TASK_LOG = ".claude_task.log"


def task_tail(workspace: Path, limit: int = 400) -> str:
    """Last of the CLI transcript for ``workspace``, for explaining a no-op run."""
    try:
        return (workspace / TASK_LOG).read_text(encoding="utf-8").strip()[-limit:]
    except OSError:
        return ""
