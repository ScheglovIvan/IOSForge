"""CodeMagic build API — trigger an iOS build and follow its status/logs/artifacts.

Wraps the (preview) CodeMagic builds REST API: ``POST /builds`` to start a build,
``GET /builds/:id`` to poll status. The build object carries ``index`` (build
number), ``status``, ``startedAt``/``finishedAt``, ``message`` (failure reason),
``artefacts`` (name/url/type) and ``buildActions`` (per-step logs). Artifact and
log bytes are fetched with the ``x-auth-token`` header and proxied to the admin.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp.codemagic_integration import CodeMagicIntegrationError, load_token

log = get_logger("mvp.codemagic_build")

TERMINAL = {"finished", "failed", "canceled", "cancelled", "timeout", "skipped"}


def _headers(token: str) -> dict[str, str]:
    return {"x-auth-token": token, "Content-Type": "application/json"}


def start_build(
    settings: Settings, token: str, *, app_id: str, workflow_id: str, branch: str
) -> str:
    """POST /builds; return the new buildId."""
    resp = httpx.post(
        f"{settings.codemagic_api_base}/builds",
        headers=_headers(token),
        json={"appId": app_id, "workflowId": workflow_id, "branch": branch},
        timeout=60.0,
    )
    if resp.status_code not in (200, 201):
        raise CodeMagicIntegrationError(f"start build {resp.status_code}: {resp.text[:300]}")
    build_id = str(resp.json().get("buildId") or "")
    if not build_id:
        raise CodeMagicIntegrationError(f"start build returned no buildId: {resp.text[:200]}")
    return build_id


def get_build(settings: Settings, token: str, build_id: str) -> dict[str, Any]:
    """GET /builds/:id — the full build object."""
    resp = httpx.get(
        f"{settings.codemagic_api_base}/builds/{build_id}", headers=_headers(token), timeout=30.0
    )
    if resp.status_code != 200:
        raise CodeMagicIntegrationError(f"get build {resp.status_code}: {resp.text[:200]}")
    return dict(resp.json().get("build", resp.json()))


def build_logs(build: dict[str, Any]) -> str:
    """Concatenate per-step logs from ``buildActions`` (+ the failure message)."""
    parts: list[str] = []
    for action in build.get("buildActions") or []:
        name = action.get("name") or action.get("type") or "step"
        status = action.get("status") or ""
        parts.append(f"=== {name} [{status}] ===")
        logs = action.get("logs")
        if logs:
            parts.append(str(logs))
    message = build.get("message")
    if message:
        parts.append(f"=== result ===\n{message}")
    return "\n".join(parts).strip()


def artifacts(build: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the build's artefacts to {name, type, url, size, md5}."""
    out: list[dict[str, Any]] = []
    for a in build.get("artefacts") or []:
        out.append(
            {
                "name": a.get("name"),
                "type": a.get("type"),
                "url": a.get("url"),
                "size": a.get("size"),
                "md5": a.get("md5"),
            }
        )
    return out


def summarize(build: dict[str, Any]) -> dict[str, Any]:
    """Flatten a build into the fields the admin persists/shows."""
    return {
        "build_id": str(build.get("_id") or ""),
        "build_number": build.get("index"),
        "workflow_id": build.get("workflowId") or build.get("fileWorkflowId"),
        "status": str(build.get("status") or ""),
        "branch": build.get("branch"),
        "started_at": build.get("startedAt") or build.get("createdAt"),
        "finished_at": build.get("finishedAt"),
        "message": build.get("message"),
        "artifacts": artifacts(build),
    }


def download_artifact(settings: Settings, token: str, url: str) -> httpx.Response:
    """Fetch an artifact URL with auth; caller streams the bytes to the client."""
    return httpx.get(url, headers={"x-auth-token": token}, timeout=120.0, follow_redirects=True)


def resolve_token(settings: Settings) -> str:
    token = load_token(settings)
    if not token:
        raise CodeMagicIntegrationError("no CodeMagic token (set codemagic_token_path)")
    return token


def first_workflow_id(codemagic_yaml: str) -> str | None:
    """Return the first workflow key under ``workflows:`` (the id to build)."""
    in_workflows = False
    for line in codemagic_yaml.splitlines():
        if re.match(r"^workflows:\s*$", line):
            in_workflows = True
            continue
        if in_workflows:
            m = re.match(r"^  (\S+):\s*$", line)
            if m:
                return m.group(1)
            if line and not line.startswith(" "):
                break
    return None
