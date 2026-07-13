"""CodeMagic Integration stage — connect the pushed GitHub repo to CodeMagic.

After GitHub Upload, this imports the repository as a CodeMagic application and
commits an iOS ``codemagic.yaml`` (if absent) so the project is ready for later
build stages. It performs NO build / signing / IPA / TestFlight — only setup.

Both tokens live in gitignored env files: the CodeMagic ``x-auth-token`` at
``settings.codemagic_token_path`` (``CODEMAGIC_TOKEN=...``) and the GitHub PAT at
``settings.github_token_path`` (reused for the codemagic.yaml commit). Returns the
saved identifiers; raises :class:`CodeMagicIntegrationError` with a reason on
permanent failure.
"""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp import github_publish

log = get_logger("mvp.codemagic")

# iOS Flutter config: scaffold platform folders, build WITHOUT signing, and
# package the .app into an unsigned .ipa for sideload / jailbreak install (no
# Apple account required). Later stages can switch to a signed App Store workflow.
_CODEMAGIC_YAML = """\
workflows:
  ios-unsigned:
    name: iOS Unsigned (sideload / jailbreak)
    instance_type: mac_mini_m2
    max_build_duration: 60
    environment:
      flutter: stable
      xcode: latest
      cocoapods: default
    scripts:
      - name: Scaffold platform folders
        script: flutter create --platforms=ios,android .
      - name: Set iOS deployment target 13.0
        script: |
          # RevenueCat / purchases_flutter use Swift concurrency (iOS 13+); the
          # flutter-create default (12.0) fails to compile. Force 13.0 in the Podfile
          # platform AND inside post_install so EVERY pod target is bumped before build.
          sed -i '' "s/^# *platform :ios.*/platform :ios, '13.0'/" ios/Podfile || true
          sed -i '' "s/platform :ios, '[0-9.]*'/platform :ios, '13.0'/" ios/Podfile || true
          perl -0pi -e "s/flutter_additional_ios_build_settings\\(target\\)/flutter_additional_ios_build_settings(target)\\n      target.build_configurations.each { |c| c.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '13.0' }/g" ios/Podfile || true
          sed -i '' "s/IPHONEOS_DEPLOYMENT_TARGET = [0-9.]*/IPHONEOS_DEPLOYMENT_TARGET = 13.0/g" ios/Runner.xcodeproj/project.pbxproj || true
      - name: Get Flutter packages
        script: flutter pub get
      - name: Install CocoaPods
        script: find . -name Podfile -execdir pod install \\; || true
      - name: Build unsigned iOS
        script: flutter build ios --release --no-codesign
      - name: Package unsigned IPA
        script: |
          cd build/ios/iphoneos
          mkdir -p Payload
          cp -r Runner.app Payload/
          zip -r app-unsigned.ipa Payload
    artifacts:
      - build/ios/iphoneos/*.ipa
      - build/ios/iphoneos/Runner.app
      - flutter_drive.log
"""


class CodeMagicIntegrationError(RuntimeError):
    """CodeMagic setup failed (message is the user-facing reason)."""


def load_token(settings: Settings) -> str:
    """Return the CodeMagic API token from the secrets file or the environment."""
    path = settings.codemagic_token_path
    if path and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith("CODEMAGIC_TOKEN") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("CODEMAGIC_TOKEN", "")


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _cm_headers(token: str) -> dict[str, str]:
    return {"x-auth-token": token, "Content-Type": "application/json"}


def github_repo(settings: Settings, gh_token: str, full_name: str) -> dict[str, Any]:
    """GET the GitHub repo (id / html_url / default_branch / ssh_url)."""
    url = f"{settings.github_api_base}/repos/{full_name}"
    resp = httpx.get(url, headers=_gh_headers(gh_token), timeout=30.0)
    if resp.status_code != 200:
        raise CodeMagicIntegrationError(f"GitHub repo lookup {resp.status_code}: {resp.text[:200]}")
    return dict(resp.json())


def _bundle_id(settings: Settings, full_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", full_name.split("/")[-1].lower()) or "app"
    return f"{settings.codemagic_bundle_prefix}.{slug}"


def _reference_yaml(settings: Settings, gh_token: str) -> str | None:
    """Fetch the proven codemagic.yaml from the reference project (settings)."""
    repo = settings.codemagic_template_repo
    if not repo:
        return None
    resp = httpx.get(
        f"{settings.github_api_base}/repos/{repo}/contents/codemagic.yaml",
        headers=_gh_headers(gh_token),
        timeout=30.0,
    )
    if resp.status_code != 200:
        log.warning("codemagic.reference_yaml_unavailable", repo=repo, status=resp.status_code)
        return None
    return base64.b64decode(resp.json()["content"]).decode()


def _render_yaml(reference: str | None, bundle_id: str) -> str:
    """Reuse the reference config, substituting only the per-app bundle id.

    Falls back to the built-in iOS template when no reference is available.
    """
    if reference is None:
        return _CODEMAGIC_YAML
    return re.sub(r"com\.batteam\.trimvo", bundle_id, reference)


def resolved_workflow_id(settings: Settings, gh_token: str, full_name: str) -> str:
    """The workflow id of the effective codemagic.yaml (reference repo or built-in).

    Rendered locally so it is reliable even right after a force-push (the GitHub
    contents API lags behind a fresh commit). Matches what ``ensure_codemagic_yaml``
    writes, so the triggered build always references an existing workflow.
    """
    from iosforge.mvp import codemagic_build

    content = _render_yaml(_reference_yaml(settings, gh_token), _bundle_id(settings, full_name))
    return codemagic_build.first_workflow_id(content) or "ios-unsigned"


def ensure_codemagic_yaml(settings: Settings, gh_token: str, full_name: str, branch: str) -> bool:
    """Commit an iOS ``codemagic.yaml`` if the repo has none. Returns True if created.

    The config mirrors the proven reference project (``codemagic_template_repo``);
    only the per-app bundle id is substituted. An existing file is never overwritten.
    """
    base = settings.github_api_base
    path = "codemagic.yaml"
    check = httpx.get(
        f"{base}/repos/{full_name}/contents/{path}",
        headers=_gh_headers(gh_token),
        params={"ref": branch},
        timeout=30.0,
    )
    if check.status_code == 200:
        return False  # already present — never overwrite
    if check.status_code != 404:
        raise CodeMagicIntegrationError(
            f"codemagic.yaml check {check.status_code}: {check.text[:200]}"
        )
    content = _render_yaml(_reference_yaml(settings, gh_token), _bundle_id(settings, full_name))
    put = httpx.put(
        f"{base}/repos/{full_name}/contents/{path}",
        headers=_gh_headers(gh_token),
        json={
            "message": "Add codemagic.yaml (iOS build config)",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        },
        timeout=30.0,
    )
    if put.status_code not in (200, 201):
        raise CodeMagicIntegrationError(
            f"codemagic.yaml commit {put.status_code}: {put.text[:200]}"
        )
    return True


def find_app(settings: Settings, cm_token: str, repo_html_url: str) -> dict[str, Any] | None:
    """Return an existing CodeMagic app for ``repo_html_url``, or None."""
    resp = httpx.get(
        f"{settings.codemagic_api_base}/apps", headers=_cm_headers(cm_token), timeout=30.0
    )
    if resp.status_code != 200:
        raise CodeMagicIntegrationError(f"CodeMagic /apps {resp.status_code}: {resp.text[:200]}")
    want = repo_html_url.rstrip("/").lower()
    for app in resp.json().get("applications", []):
        html = str(app.get("repository", {}).get("htmlUrl") or "").rstrip("/").lower()
        if html == want:
            return dict(app)
    return None


def create_app(settings: Settings, cm_token: str, repo_ssh_url: str) -> dict[str, Any]:
    """POST /apps to import a (public) repo; return the created app summary."""
    body: dict[str, Any] = {"repositoryUrl": repo_ssh_url}
    if settings.codemagic_team_id:
        body["teamId"] = settings.codemagic_team_id
    resp = httpx.post(
        f"{settings.codemagic_api_base}/apps",
        headers=_cm_headers(cm_token),
        json=body,
        timeout=60.0,
    )
    if resp.status_code not in (200, 201):
        raise CodeMagicIntegrationError(
            f"CodeMagic create app {resp.status_code}: {resp.text[:300]}"
        )
    return dict(resp.json())


def get_app(settings: Settings, cm_token: str, app_id: str) -> dict[str, Any]:
    """GET /apps/:id — full application (branches, repository)."""
    resp = httpx.get(
        f"{settings.codemagic_api_base}/apps/{app_id}", headers=_cm_headers(cm_token), timeout=30.0
    )
    if resp.status_code != 200:
        raise CodeMagicIntegrationError(f"CodeMagic /apps/{app_id} {resp.status_code}")
    return dict(resp.json().get("application", resp.json()))


def wait_for_sync(settings: Settings, cm_token: str, app_id: str) -> list[str]:
    """Poll the app until CodeMagic reports its branches; return them (best-effort)."""
    deadline = settings.codemagic_sync_timeout_s
    waited = 0
    while waited <= deadline:
        app = get_app(settings, cm_token, app_id)
        branches = app.get("branches") or []
        if branches:
            return [str(b) for b in branches]
        time.sleep(5)
        waited += 5
    return []


def integrate(settings: Settings, *, repo_full_name: str, repo_html_url: str) -> dict[str, Any]:
    """Connect ``repo_full_name`` to CodeMagic; return the saved identifiers."""
    cm_token = load_token(settings)
    if not cm_token:
        raise CodeMagicIntegrationError("no CodeMagic token (set codemagic_token_path)")
    gh_token = github_publish.load_token(settings)
    if not gh_token:
        raise CodeMagicIntegrationError("no GitHub token for codemagic.yaml commit")

    log.info("codemagic.connecting")
    if (
        httpx.get(
            f"{settings.codemagic_api_base}/apps", headers=_cm_headers(cm_token), timeout=30.0
        ).status_code
        != 200
    ):
        raise CodeMagicIntegrationError("CodeMagic authentication failed")
    log.info("codemagic.auth_ok")

    log.info("codemagic.checking_repo", repo=repo_full_name)
    repo = github_repo(settings, gh_token, repo_full_name)
    default_branch = str(repo.get("default_branch") or "main")
    # Public repos are added via their HTTPS clone URL so CodeMagic clones them
    # anonymously — an SSH URL without a deploy key fails to clone.
    clone_url = str(repo.get("clone_url") or f"https://github.com/{repo_full_name}.git")
    log.info("codemagic.repo_found", repo=repo_full_name, branch=default_branch)

    created_yaml = ensure_codemagic_yaml(settings, gh_token, repo_full_name, default_branch)
    log.info("codemagic.codemagic_yaml", created=created_yaml)

    app = find_app(settings, cm_token, repo_html_url)
    if app is None:
        log.info("codemagic.importing_repo", repo=repo_full_name)
        summary = create_app(settings, cm_token, clone_url)
        app_id = str(summary.get("_id"))
        app = get_app(settings, cm_token, app_id)
        log.info("codemagic.app_created", app_id=app_id)
    else:
        app_id = str(app.get("_id"))
        log.info("codemagic.app_exists", app_id=app_id)

    branches = wait_for_sync(settings, cm_token, app_id)
    log.info("codemagic.sync_done", app_id=app_id, branches=len(branches))

    # Real verification: the app must actually exist and its branches must have
    # synced — an API 200 alone is not "connected". Re-fetch and assert.
    verify = get_app(settings, cm_token, app_id)
    if not verify.get("_id"):
        raise CodeMagicIntegrationError("app not found in CodeMagic after import")
    if not branches:
        raise CodeMagicIntegrationError(
            f"repository not synced (no branches after {settings.codemagic_sync_timeout_s}s)"
        )

    repo_meta = app.get("repository", {}) if isinstance(app.get("repository"), dict) else {}
    result = {
        "application_id": app_id,
        "repository_id": repo_meta.get("id") or repo.get("id"),
        "repository_url": repo_html_url,
        "default_branch": str(repo_meta.get("defaultBranch") or default_branch),
        "project_name": str(app.get("appName") or repo_full_name.split("/")[-1]),
        "codemagic_yaml_created": created_yaml,
        "branches_synced": len(branches),
    }
    log.info("codemagic.done", **{k: result[k] for k in ("application_id", "project_name")})
    return result
