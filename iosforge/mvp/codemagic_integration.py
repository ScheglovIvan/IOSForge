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
      flutter: 3.44.4
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
      - name: Set display name and Android identity
        script: |
          # Display name (user-visible) + Android applicationId/label are written from
          # the build profile. The iOS PRODUCT_BUNDLE_IDENTIFIER is intentionally left
          # at the flutter-create default (com.example.*): a custom iOS bundle id makes
          # Xcode automatic-signing demand a Development Team even for --no-codesign, so
          # the real iOS bundle id is applied together with signing (real profile, once
          # Apple credentials are configured). __APP_NAME__ / __BUNDLE_ID__ are per-job.
          /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName __APP_NAME__" ios/Runner/Info.plist \
            || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string __APP_NAME__" ios/Runner/Info.plist
          sed -i '' 's/applicationId "[^"]*"/applicationId "__BUNDLE_ID__"/' android/app/build.gradle || true
          sed -i '' 's/android:label="[^"]*"/android:label="__APP_NAME__"/' android/app/src/main/AndroidManifest.xml || true
      - name: Inject iOS permission usage descriptions
        script: |
          # Real iOS permissions crash at runtime without their NS*UsageDescription in
          # Info.plist. The generator emits ios_permissions.json (a flat {plist_key: reason}
          # object, plus optional array values like UIBackgroundModes); apply each here after
          # flutter create regenerated ios/. No file => nothing to do.
          if [ -f ios_permissions.json ]; then
            python3 - <<'PY'
          import json, subprocess
          PLIST = "ios/Runner/Info.plist"
          def buddy(cmd):
              return subprocess.run(["/usr/libexec/PlistBuddy", "-c", cmd, PLIST]).returncode
          data = json.load(open("ios_permissions.json"))
          for key, value in data.items():
              if isinstance(value, list):
                  buddy(f"Delete :{key}")
                  buddy(f"Add :{key} array")
                  for i, item in enumerate(value):
                      buddy(f"Add :{key}:{i} string {item}")
              else:
                  text = str(value)
                  if buddy(f"Set :{key} {text}") != 0:
                      buddy(f"Add :{key} string {text}")
          print("applied", len(data), "iOS permission key(s)")
          PY
          fi
      - name: Pin RevenueCat SDK
        script: |
          # purchases_flutter < 8 fails to compile on current Xcode with
          # "'SubscriptionPeriod' is ambiguous". Codegen sometimes writes an older
          # constraint from the source app; force a known-good one at build time so no
          # regeneration can reintroduce the break. No-op if the package isn't used.
          sed -i '' -E "s/^([[:space:]]*purchases_flutter:).*/\\1 ^8.0.0/" pubspec.yaml || true
      - name: Get Flutter packages
        script: flutter pub get
      - name: Install CocoaPods
        script: find . -name Podfile -execdir pod install \\; || true
      - name: Build unsigned iOS
        script: |
          # `flutter build ios --no-codesign` still fails on this CI ("requires a
          # Development Team") for release. Configure with flutter, then build via
          # xcodebuild with signing disabled on the command line (highest precedence,
          # overrides the project's automatic-signing settings) — a true unsigned .app.
          flutter build ios --release --no-codesign --config-only
          xcodebuild -workspace ios/Runner.xcworkspace -scheme Runner \
            -configuration Release -sdk iphoneos -derivedDataPath build/ios_dd \
            CODE_SIGN_IDENTITY="" CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO \
            CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM="" PROVISIONING_PROFILE_SPECIFIER="" \
            build
      - name: Package unsigned IPA
        script: |
          APP="$(find build/ios_dd -path '*/Release-iphoneos/Runner.app' -type d | head -1)"
          if [ -z "$APP" ]; then APP="$(find build -name Runner.app -type d | head -1)"; fi
          mkdir -p build/ios/iphoneos/Payload
          cp -R "$APP" build/ios/iphoneos/Payload/
          cd build/ios/iphoneos && zip -r app-unsigned.ipa Payload
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


def _render_yaml(reference: str | None, bundle_id: str, app_name: str = "App") -> str:
    """Render the effective config, substituting the per-job bundle id + display name.

    Falls back to the built-in iOS template when no reference is available. Both the
    built-in (``__BUNDLE_ID__`` / ``__APP_NAME__``) and reference (``com.batteam.trimvo``)
    placeholders are filled so the built binary gets the job's identity.
    """
    content = _CODEMAGIC_YAML if reference is None else reference
    content = re.sub(r"com\.batteam\.trimvo", bundle_id, content)
    content = content.replace("__BUNDLE_ID__", bundle_id).replace("__APP_NAME__", app_name)
    return content


def resolved_workflow_id(
    settings: Settings,
    gh_token: str,
    full_name: str,
    *,
    bundle_id: str | None = None,
    app_name: str = "App",
) -> str:
    """The workflow id of the effective codemagic.yaml (reference repo or built-in).

    Rendered locally so it is reliable even right after a force-push (the GitHub
    contents API lags behind a fresh commit). Matches what ``ensure_codemagic_yaml``
    writes, so the triggered build always references an existing workflow.
    """
    from iosforge.mvp import codemagic_build

    bid = bundle_id or _bundle_id(settings, full_name)
    content = _render_yaml(_reference_yaml(settings, gh_token), bid, app_name)
    return codemagic_build.first_workflow_id(content) or "ios-unsigned"


def ensure_codemagic_yaml(
    settings: Settings,
    gh_token: str,
    full_name: str,
    branch: str,
    *,
    bundle_id: str | None = None,
    app_name: str = "App",
) -> bool:
    """Upsert the iOS ``codemagic.yaml`` (bundle id + display name for this job).

    Creates the file when absent and UPDATES it when the rendered content differs
    (e.g. the build profile / bundle id / name changed) so identity changes reach the
    build; a matching file is left untouched. Returns True when it wrote the file.
    """
    base = settings.github_api_base
    path = "codemagic.yaml"
    bid = bundle_id or _bundle_id(settings, full_name)
    content = _render_yaml(_reference_yaml(settings, gh_token), bid, app_name)
    encoded = base64.b64encode(content.encode()).decode()

    check = httpx.get(
        f"{base}/repos/{full_name}/contents/{path}",
        headers=_gh_headers(gh_token),
        params={"ref": branch},
        timeout=30.0,
    )
    sha: str | None = None
    if check.status_code == 200:
        body = check.json()
        existing = base64.b64decode(body["content"]).decode()
        if existing == content:
            return False  # already up to date
        sha = body["sha"]
    elif check.status_code != 404:
        raise CodeMagicIntegrationError(
            f"codemagic.yaml check {check.status_code}: {check.text[:200]}"
        )

    payload: dict[str, Any] = {
        "message": "Set codemagic.yaml (iOS build config)",
        "content": encoded,
        "branch": branch,
    }
    if sha is not None:
        payload["sha"] = sha
    put = httpx.put(
        f"{base}/repos/{full_name}/contents/{path}",
        headers=_gh_headers(gh_token),
        json=payload,
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


def integrate(
    settings: Settings,
    *,
    repo_full_name: str,
    repo_html_url: str,
    bundle_id: str | None = None,
    app_name: str = "App",
) -> dict[str, Any]:
    """Connect ``repo_full_name`` to CodeMagic; return the saved identifiers.

    ``bundle_id`` / ``app_name`` (from the build profile) are written into the
    committed codemagic.yaml so the built binary gets the job's identity.
    """
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

    created_yaml = ensure_codemagic_yaml(
        settings,
        gh_token,
        repo_full_name,
        default_branch,
        bundle_id=bundle_id,
        app_name=app_name,
    )
    log.info("codemagic.codemagic_yaml", created=created_yaml, bundle_id=bundle_id)

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
