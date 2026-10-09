"""GitHub repositories for the legal pages (Privacy Policy / Support on GitHub Pages).

The generated SwiftUI app is never pushed anywhere; GitHub only hosts the job's legal
pages (:func:`iosforge.mvp.legal_pages.publish_pages`). This module reads the token —
from the gitignored env file referenced by ``settings.github_token_path``
(``GITHUB_TOKEN=...``) or the process environment, never committed — and creates the
pages repository. Errors raise :class:`GitHubPublishError` with a readable reason.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.github_publish")


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


def create_repo(settings: Settings, token: str, name: str, description: str) -> dict[str, Any]:
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
