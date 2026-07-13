"""GitHub Upload stage — push a generated project to a new public repository.

Creates a repo via the GitHub REST API (Personal Access Token), then ``git init``
→ add → commit → push. The token lives in a gitignored env file referenced by
``settings.github_token_path`` (``GITHUB_TOKEN=...``) or the process environment,
never committed. Returns the repository URL; raises :class:`GitHubPublishError`
with a human-readable reason on failure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.github_publish")

_COMMITTER_NAME = "IOSForge"
_COMMITTER_EMAIL = "bot@iosforge.dev"

# Build/tool artifacts and any IOSForge orchestration leftovers that must never
# reach the published repo — only a clean, buildable Flutter project ships.
_PRUNE = (
    "build",
    ".dart_tool",
    ".flutter-plugins",
    ".flutter-plugins-dependencies",
    ".packages",
    ".claude",
    "claude_ws",
    "handoff",
    "TASK.md",
    "PROMPT.md",
    "ANALYZE_PROMPT.md",
    "DECOMPOSE_PROMPT.md",
    "CONSTITUTION.md",
    "AGENTS.md",
    "app_spec.json",
    "tasks.json",
    "screens.json",
    "network_index.json",
    "selftest_report.json",
    "corrective_tasks.json",
)

_GITIGNORE = """\
# Flutter / Dart
.dart_tool/
.packages
.pub-cache/
.pub/
build/
.flutter-plugins
.flutter-plugins-dependencies

# iOS / Xcode
ios/Pods/
ios/.symlinks/
ios/Flutter/Flutter.framework
ios/Flutter/Flutter.podspec
**/*.mode1v3
**/*.pbxuser
**/xcuserdata/

# Android
android/.gradle/
android/local.properties
**/*.keystore

# IDE / OS
.idea/
.vscode/
*.iml
.DS_Store
*.log
"""


def _clean_project(project_dir: Path) -> None:
    """Prune build artifacts + any orchestration leftovers; ensure a Flutter .gitignore.

    Only a clean, buildable Flutter project ships — build/, .dart_tool/, caches, logs
    and IOSForge internal files are removed before ``git add``. Legit ``assets/`` (fonts
    and media embedded into the app) are preserved.
    """
    for name in _PRUNE:
        target = project_dir / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
    (project_dir / ".gitignore").write_text(_GITIGNORE)


class GitHubPublishError(RuntimeError):
    """Repo creation or push failed (message is the user-facing reason)."""


def load_token(settings: Settings) -> str:
    """Return the GitHub PAT from the secrets file or the environment."""
    path = settings.github_token_path
    if path and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith("GITHUB_TOKEN") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("GITHUB_TOKEN", "")


def slugify_repo(name: str) -> str:
    """Turn an app name into a valid GitHub repo slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "app"


def _run_git(args: list[str], cwd: Path, *, token_url: str | None = None) -> None:
    """Run a git command; raise GitHubPublishError with a sanitized message."""
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        if token_url:
            detail = detail.replace(token_url, "<remote>")
        raise GitHubPublishError(f"git {args[0]} failed: {detail[:500]}")


def _create_repo(settings: Settings, token: str, name: str, description: str) -> dict[str, Any]:
    """POST /user/repos; on a name collision retry with a numeric suffix."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{settings.github_api_base}/user/repos"
    with httpx.Client(timeout=30.0) as client:
        for attempt in range(6):
            candidate = name if attempt == 0 else f"{name}-{attempt + 1}"
            body = {
                "name": candidate,
                "description": description[:350],
                "private": settings.github_repo_private,
                "auto_init": False,
            }
            resp = client.post(url, headers=headers, json=body)
            if resp.status_code == 201:
                return dict(resp.json())
            if resp.status_code == 422 and "already exists" in resp.text:
                continue  # name taken — try the next suffix
            raise GitHubPublishError(f"GitHub API {resp.status_code}: {resp.text[:300]}")
    raise GitHubPublishError(f"could not find a free repo name for {name!r}")


def publish(
    settings: Settings,
    project_dir: Path,
    *,
    app_name: str,
    description: str = "",
    fallback_slug: str = "app",
) -> dict[str, str]:
    """Create a repo and push ``project_dir`` to it; return {name, url, full_name}."""
    token = load_token(settings)
    if not token:
        raise GitHubPublishError("no GitHub token (set github_token_path / GITHUB_TOKEN)")
    if not project_dir.is_dir():
        raise GitHubPublishError(f"project dir not found: {project_dir}")

    repo_name = settings.github_repo_prefix + slugify_repo(app_name or fallback_slug)
    repo = _create_repo(
        settings, token, repo_name, description or f"{app_name} — generated by IOSForge"
    )
    clone_url: str = repo["clone_url"]
    html_url: str = repo["html_url"]
    full_name: str = repo["full_name"]
    push_url = clone_url.replace("https://", f"https://x-access-token:{token}@")

    _clean_project(project_dir)
    _run_git(["init", "-b", "main"], project_dir)
    _run_git(["config", "user.name", _COMMITTER_NAME], project_dir)
    _run_git(["config", "user.email", _COMMITTER_EMAIL], project_dir)
    _run_git(["add", "-A"], project_dir)
    _run_git(["commit", "-m", "Initial commit"], project_dir)
    _run_git(["push", push_url, "main"], project_dir, token_url=push_url)

    log.info("github_publish.done", repo=full_name, url=html_url)
    return {"name": repo["name"], "url": html_url, "full_name": full_name}


def push_existing(
    settings: Settings, project_dir: Path, *, full_name: str, message: str
) -> dict[str, str]:
    """Push a reworked project as a fresh snapshot commit to an EXISTING repo."""
    token = load_token(settings)
    if not token:
        raise GitHubPublishError("no GitHub token (set github_token_path / GITHUB_TOKEN)")
    if not project_dir.is_dir():
        raise GitHubPublishError(f"project dir not found: {project_dir}")
    push_url = f"https://x-access-token:{token}@github.com/{full_name}.git"

    _clean_project(project_dir)
    _run_git(["init", "-b", "main"], project_dir)
    _run_git(["config", "user.name", _COMMITTER_NAME], project_dir)
    _run_git(["config", "user.email", _COMMITTER_EMAIL], project_dir)
    _run_git(["add", "-A"], project_dir)
    _run_git(["commit", "-m", message[:200] or "Rework"], project_dir)
    _run_git(["push", "--force", push_url, "main"], project_dir, token_url=push_url)

    url = f"https://github.com/{full_name}"
    log.info("github_publish.pushed_existing", repo=full_name, url=url)
    return {"url": url, "full_name": full_name}
