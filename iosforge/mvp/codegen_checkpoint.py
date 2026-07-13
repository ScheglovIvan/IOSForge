"""Per-task checkpoint for resumable frontend codegen (claude task-runner).

After each completed codegen task the accumulating workspace
(``claude_ws/flutter_app``) plus the completed-task set and the (stable)
``tasks.json`` are persisted to object storage under
``jobs/{id}/codegen_checkpoint/``. On a later attempt the workspace + task plan
are restored and already-done tasks are skipped, so a crash / rate-limit resumes
from the last finished task instead of restarting from zero.

Only the ``codegen_orchestrator == "claude"`` single-pass task runner is
resumable (hermes/cloud build via worktrees and never call the checkpoint hook).
All calls are best-effort: a checkpoint failure never fails the build.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp.paths import RunPaths
from iosforge.storage.client import build_key

log = get_logger("mvp.codegen_checkpoint")

_KIND = "codegen_checkpoint"


def _key(job_id: str, name: str) -> str:
    return build_key(job_id=job_id, kind=_KIND, name=name)


def _workspace(paths: RunPaths) -> Path:
    return paths.claude_ws / "flutter_app"


def _exclude_vcs(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Drop git metadata from the checkpoint archive (parallel screen builds init a repo)."""
    if ".git" in Path(info.name).parts:
        return None
    return info


def save(
    storage: Any, job_id: str, paths: RunPaths, completed: set[str], *, work_dir: Path
) -> None:
    """Persist progress + tasks.json + the accumulating workspace (best-effort)."""
    try:
        storage.put(
            _key(job_id, "progress.json"),
            json.dumps({"completed": sorted(completed)}).encode(),
            content_type="application/json",
        )
        if paths.tasks_json.exists():
            storage.put(
                _key(job_id, "tasks.json"),
                paths.tasks_json.read_bytes(),
                content_type="application/json",
            )
        ws = _workspace(paths)
        if ws.exists():
            arch = work_dir / "ckpt_ws.tar.gz"
            with tarfile.open(arch, "w:gz") as tf:
                tf.add(str(ws), arcname=".", filter=_exclude_vcs)
            storage.put(
                _key(job_id, "workspace.tar.gz"),
                arch.read_bytes(),
                content_type="application/gzip",
            )
    except Exception as exc:  # a checkpoint must never fail the build
        log.warning("codegen_checkpoint.save_failed", job_id=job_id, error=str(exc))


def load(storage: Any, job_id: str, paths: RunPaths, *, work_dir: Path) -> set[str] | None:
    """Restore tasks.json + workspace and return the completed set, or None.

    ``None`` means no checkpoint exists (fresh build). When a checkpoint is
    present, ``tasks.json`` is restored verbatim (the task plan MUST be stable —
    decompose is LLM-driven and non-deterministic).
    """
    try:
        progress = json.loads(storage.get(_key(job_id, "progress.json")))
    except Exception:
        return None
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    paths.tasks_json.write_bytes(storage.get(_key(job_id, "tasks.json")))
    ws = _workspace(paths)
    try:
        arch = work_dir / "ckpt_ws.tar.gz"
        arch.write_bytes(storage.get(_key(job_id, "workspace.tar.gz")))
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        with tarfile.open(arch, "r:gz") as tf:
            tf.extractall(ws, filter="data")
    except Exception as exc:  # progress exists but workspace missing/corrupt
        log.warning("codegen_checkpoint.workspace_restore_failed", job_id=job_id, error=str(exc))
    completed = {str(t) for t in progress.get("completed", [])}
    log.info("codegen_checkpoint.loaded", job_id=job_id, completed=len(completed))
    return completed


def clear(storage: Any, job_id: str) -> None:
    """Remove the checkpoint after a successful build (best-effort)."""
    for name in ("workspace.tar.gz", "progress.json", "tasks.json"):
        try:
            storage.delete(_key(job_id, name))
        except Exception:  # noqa: S110 — cleanup is optional, guarded by the GenerationResult
            pass
