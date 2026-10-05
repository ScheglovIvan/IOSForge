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
from dataclasses import dataclass
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
        script: flutter create --platforms=ios .
      - name: Set iOS deployment target 15.0
        script: |
          # Apphud SDK 3.x requires iOS 15.0+ (pod install fails below it); the
          # flutter-create default is lower. Force 15.0 in the Podfile platform AND
          # inside post_install so EVERY pod target is bumped before build.
          sed -i '' "s/^# *platform :ios.*/platform :ios, '15.0'/" ios/Podfile || true
          sed -i '' "s/platform :ios, '[0-9.]*'/platform :ios, '15.0'/" ios/Podfile || true
          perl -0pi -e "s/flutter_additional_ios_build_settings\\(target\\)/flutter_additional_ios_build_settings(target)\\n      target.build_configurations.each { |c| c.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '15.0' }/g" ios/Podfile || true
          sed -i '' "s/IPHONEOS_DEPLOYMENT_TARGET = [0-9.]*/IPHONEOS_DEPLOYMENT_TARGET = 15.0/g" ios/Runner.xcodeproj/project.pbxproj || true
      - name: Set display name
        script: |
          # Display name (user-visible) is written from the build profile. The iOS
          # PRODUCT_BUNDLE_IDENTIFIER is intentionally left at the flutter-create default
          # (com.example.*): a custom iOS bundle id makes Xcode automatic-signing demand a
          # Development Team even for --no-codesign, so the real iOS bundle id is applied
          # together with signing (real profile, once Apple credentials are configured).
          # __APP_NAME__ / __BUNDLE_ID__ are per-job.
          /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName __APP_NAME__" ios/Runner/Info.plist \
            || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string __APP_NAME__" ios/Runner/Info.plist
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
              if isinstance(value, bool):
                  buddy(f"Delete :{key}")
                  buddy(f"Add :{key} bool {'true' if value else 'false'}")
              elif isinstance(value, list):
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
      - name: Pin SDK versions
        script: |
          # Force known-good constraints at build time so no regeneration can pin an
          # incompatible version of the subscription / attribution SDKs. No-op if unused.
          sed -i '' -E "s/^([[:space:]]*apphud:).*/\\1 3.1.2/" pubspec.yaml || true
          sed -i '' -E "s/^([[:space:]]*tenjin_plugin:).*/\\1 ^1.2.0/" pubspec.yaml || true
      - name: Merge SKAdNetwork ad network ids
        script: |
          # A network whose SKAdNetworkIdentifier is absent from Info.plist gets ZERO SKAN
          # attribution until the app ships again. The consolidated list is maintained in
          # the MMP dashboard and staged next to the app; no file => nothing to do.
          if [ -f skadnetwork_ids.plist ]; then
            python3 - <<'PY'
          import plistlib
          INFO = "ios/Runner/Info.plist"
          with open("skadnetwork_ids.plist", "rb") as f:
              items = plistlib.load(f).get("SKAdNetworkItems") or []
          if items:
              with open(INFO, "rb") as f:
                  info = plistlib.load(f)
              info["SKAdNetworkItems"] = items
              with open(INFO, "wb") as f:
                  plistlib.dump(info, f)
          print("merged", len(items), "SKAdNetwork id(s)")
          PY
          fi
      - name: Get Flutter packages
        script: flutter pub get
      - name: Generate launcher icons
        script: |
          # Builds every iOS/Android launcher size from assets/icon/app_icon.png.
          # A no-op when the app has no staged icon, so it never fails a build.
          if [ -f assets/icon/app_icon.png ]; then
            dart run flutter_launcher_icons || flutter pub run flutter_launcher_icons || true
          fi
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


# Signed App Store workflow: signing is driven entirely by App Store Connect API key
# environment variables injected at build-trigger time (APP_STORE_CONNECT_ISSUER_ID /
# _KEY_IDENTIFIER / _PRIVATE_KEY + CERTIFICATE_PRIVATE_KEY), so the key is NEVER committed
# to this yaml and NO manual registration in the CodeMagic UI is needed. The
# app-store-connect CLI fetches/creates the distribution certificate + provisioning
# profile on the fly; the real bundle id is written into the binary; the .ipa is uploaded
# to App Store Connect (it appears under the app's builds; TestFlight groups / metadata /
# review submission stay manual). Placeholders: __BUNDLE_ID__ / __APP_NAME__ /
# __APP_STORE_APPLE_ID__.
_CODEMAGIC_YAML_SIGNED = """\
workflows:
  ios-store:
    name: iOS Store (signed - App Store Connect)
    instance_type: mac_mini_m2
    max_build_duration: 60
    environment:
      vars:
        BUNDLE_ID: "__BUNDLE_ID__"
        APP_STORE_APPLE_ID: __APP_STORE_APPLE_ID__
      flutter: 3.44.4
      xcode: latest
      cocoapods: default
    scripts:
      - name: Scaffold platform folders
        script: flutter create --platforms=ios .
      - name: Set iOS deployment target 15.0
        script: |
          sed -i '' "s/^# *platform :ios.*/platform :ios, '15.0'/" ios/Podfile || true
          sed -i '' "s/platform :ios, '[0-9.]*'/platform :ios, '15.0'/" ios/Podfile || true
          perl -0pi -e "s/flutter_additional_ios_build_settings\\(target\\)/flutter_additional_ios_build_settings(target)\\n      target.build_configurations.each { |c| c.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '15.0' }/g" ios/Podfile || true
          sed -i '' "s/IPHONEOS_DEPLOYMENT_TARGET = [0-9.]*/IPHONEOS_DEPLOYMENT_TARGET = 15.0/g" ios/Runner.xcodeproj/project.pbxproj || true
      - name: Set bundle id and display name
        script: |
          # A signed store build MUST carry the real bundle id in the binary (unlike the
          # unsigned sideload build, which stays at com.example.*). __APP_STORE_APPLE_ID__
          # is the numeric App Store id used for the auto build number and the upload.
          /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName __APP_NAME__" ios/Runner/Info.plist \
            || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string __APP_NAME__" ios/Runner/Info.plist
          sed -i '' "s/PRODUCT_BUNDLE_IDENTIFIER = [^;]*;/PRODUCT_BUNDLE_IDENTIFIER = __BUNDLE_ID__;/g" ios/Runner.xcodeproj/project.pbxproj || true
      - name: Inject iOS permission usage descriptions
        script: |
          if [ -f ios_permissions.json ]; then
            python3 - <<'PY'
          import json, subprocess
          PLIST = "ios/Runner/Info.plist"
          def buddy(cmd):
              return subprocess.run(["/usr/libexec/PlistBuddy", "-c", cmd, PLIST]).returncode
          data = json.load(open("ios_permissions.json"))
          for key, value in data.items():
              if isinstance(value, bool):
                  buddy(f"Delete :{key}")
                  buddy(f"Add :{key} bool {'true' if value else 'false'}")
              elif isinstance(value, list):
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
      - name: Merge SKAdNetwork ad network ids
        script: |
          if [ -f skadnetwork_ids.plist ]; then
            python3 - <<'PY'
          import plistlib
          INFO = "ios/Runner/Info.plist"
          with open("skadnetwork_ids.plist", "rb") as f:
              items = plistlib.load(f).get("SKAdNetworkItems") or []
          if items:
              with open(INFO, "rb") as f:
                  info = plistlib.load(f)
              info["SKAdNetworkItems"] = items
              with open(INFO, "wb") as f:
                  plistlib.dump(info, f)
          print("merged", len(items), "SKAdNetwork id(s)")
          PY
          fi
      - name: Pin SDK versions
        script: |
          sed -i '' -E "s/^([[:space:]]*apphud:).*/\\1 3.1.2/" pubspec.yaml || true
          sed -i '' -E "s/^([[:space:]]*tenjin_plugin:).*/\\1 ^1.2.0/" pubspec.yaml || true
      - name: Get Flutter packages
        script: flutter pub get
      - name: Generate launcher icons
        script: |
          # Builds every iOS/Android launcher size from assets/icon/app_icon.png.
          # A no-op when the app has no staged icon, so it never fails a build.
          if [ -f assets/icon/app_icon.png ]; then
            dart run flutter_launcher_icons || flutter pub run flutter_launcher_icons || true
          fi
      - name: Install CocoaPods
        script: find . -name Podfile -execdir pod install \\; || true
      - name: Set up code signing
        script: |
          # Env-var signing: the App Store Connect API key + certificate private key are
          # injected as build variables, so app-store-connect fetches (or, with --create,
          # mints once and re-uses) the distribution certificate + App Store profile, and
          # use-profiles writes export_options.plist. No key is stored in this repo.
          keychain initialize
          app-store-connect fetch-signing-files "$BUNDLE_ID" \
            --platform IOS \
            --type IOS_APP_STORE \
            --certificate-key="$CERTIFICATE_PRIVATE_KEY" \
            --create
          keychain add-certificates
          xcode-project use-profiles
      - name: Build signed IPA
        script: |
          # Auto-increment off the highest build number EVER uploaded (TestFlight +
          # processing included) so re-uploads never collide. get-latest-build-number
          # sees all builds; get-latest-app-store-build-number only sees a live App Store
          # version (absent until first release) and would keep returning 0 -> build 1.
          LATEST=$(app-store-connect get-latest-build-number "$APP_STORE_APPLE_ID" 2>/dev/null || echo 0)
          flutter build ipa --release \
            --build-number=$(($LATEST + 1)) \
            --export-options-plist=/Users/builder/export_options.plist
    artifacts:
      - build/ios/ipa/*.ipa
    publishing:
      app_store_connect:
        api_key: $APP_STORE_CONNECT_PRIVATE_KEY
        key_id: $APP_STORE_CONNECT_KEY_IDENTIFIER
        issuer_id: $APP_STORE_CONNECT_ISSUER_ID
        submit_to_testflight: false
"""


@dataclass(frozen=True)
class StoreSigning:
    """Per-app inputs for a signed App Store build.

    ``apple_id`` is the app's numeric App Store id (per job). ``asc_api_key_name`` is the
    optional human label of the App Store Connect API key; signing itself is driven by the
    uploaded credential's environment variables (see ``mvp.asc_credentials``), not by a
    key registered in the CodeMagic UI, so the name is informational only.
    """

    asc_api_key_name: str
    apple_id: str


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


def _render_yaml(
    reference: str | None,
    bundle_id: str,
    app_name: str = "App",
    signing: StoreSigning | None = None,
) -> str:
    """Render the effective config, substituting the per-job bundle id + display name.

    With ``signing`` the signed App Store template is used (real bundle id, automatic
    signing, upload to ASC) and the API-key name + Apple id are filled in. Without it,
    the reference repo's config or the built-in unsigned sideload template is used. Both
    the built-in placeholders and the reference (``com.batteam.trimvo``) are filled so
    the built binary gets the job's identity.
    """
    if signing is not None:
        content = _CODEMAGIC_YAML_SIGNED
        content = content.replace("__APP_STORE_APPLE_ID__", signing.apple_id)
    else:
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
    signing: StoreSigning | None = None,
) -> str:
    """The workflow id of the effective codemagic.yaml (reference repo or built-in).

    Rendered locally so it is reliable even right after a force-push (the GitHub
    contents API lags behind a fresh commit). Matches what ``ensure_codemagic_yaml``
    writes, so the triggered build always references an existing workflow.
    """
    from iosforge.mvp import codemagic_build

    bid = bundle_id or _bundle_id(settings, full_name)
    reference = None if signing is not None else _reference_yaml(settings, gh_token)
    content = _render_yaml(reference, bid, app_name, signing)
    return codemagic_build.first_workflow_id(content) or "ios-unsigned"


def ensure_codemagic_yaml(
    settings: Settings,
    gh_token: str,
    full_name: str,
    branch: str,
    *,
    bundle_id: str | None = None,
    app_name: str = "App",
    signing: StoreSigning | None = None,
) -> bool:
    """Upsert the iOS ``codemagic.yaml`` (bundle id + display name for this job).

    Creates the file when absent and UPDATES it when the rendered content differs
    (e.g. the build profile / bundle id / name changed) so identity changes reach the
    build; a matching file is left untouched. Returns True when it wrote the file.
    With ``signing`` the signed App Store workflow is written instead of the unsigned one.
    """
    base = settings.github_api_base
    path = "codemagic.yaml"
    bid = bundle_id or _bundle_id(settings, full_name)
    reference = None if signing is not None else _reference_yaml(settings, gh_token)
    content = _render_yaml(reference, bid, app_name, signing)
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
    signing: StoreSigning | None = None,
) -> dict[str, Any]:
    """Connect ``repo_full_name`` to CodeMagic; return the saved identifiers.

    ``bundle_id`` / ``app_name`` (from the build profile) are written into the
    committed codemagic.yaml so the built binary gets the job's identity. With
    ``signing`` the signed App Store workflow is committed instead of the unsigned one.
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
        signing=signing,
    )
    log.info(
        "codemagic.codemagic_yaml",
        created=created_yaml,
        bundle_id=bundle_id,
        signed=signing is not None,
    )

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
