"""CodeMagic Integration stage: token loading + integrate() orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import codemagic_integration as cm
from iosforge.mvp import github_publish


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


def _settings(tmp_path: Path) -> Settings:
    cmf = tmp_path / "cm.env"
    cmf.write_text("CODEMAGIC_TOKEN=cm_tok\n")
    ghf = tmp_path / "gh.env"
    ghf.write_text("GITHUB_TOKEN=gh_tok\n")
    return Settings(codemagic_token_path=str(cmf), github_token_path=str(ghf))


def test_load_token_from_file(tmp_path: Path) -> None:
    assert cm.load_token(_settings(tmp_path)) == "cm_tok"


def test_integrate_requires_token(tmp_path: Path) -> None:
    s = Settings(codemagic_token_path="")  # no token
    with pytest.raises(cm.CodeMagicIntegrationError, match="no CodeMagic token"):
        cm.integrate(s, repo_full_name="me/app", repo_html_url="https://github.com/me/app")


def test_integrate_orchestrates_and_saves_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    monkeypatch.setattr(cm.httpx, "get", lambda *a, **k: _Resp(200))  # auth check
    monkeypatch.setattr(
        cm,
        "github_repo",
        lambda settings, tok, full: {
            "id": 999,
            "default_branch": "main",
            "ssh_url": f"git@github.com:{full}.git",
        },
    )
    created: list[bool] = []
    monkeypatch.setattr(
        cm,
        "ensure_codemagic_yaml",
        lambda settings, tok, full, branch, **k: created.append(True) or True,
    )
    monkeypatch.setattr(cm, "find_app", lambda settings, tok, url: None)  # no existing app
    monkeypatch.setattr(
        cm, "create_app", lambda settings, tok, ssh: {"_id": "app123", "appName": "app"}
    )
    monkeypatch.setattr(
        cm,
        "get_app",
        lambda settings, tok, app_id: {
            "_id": "app123",
            "appName": "app",
            "branches": ["main"],
            "repository": {"id": "repo-uuid", "defaultBranch": "main"},
        },
    )
    monkeypatch.setattr(cm, "wait_for_sync", lambda settings, tok, app_id: ["main"])

    res = cm.integrate(s, repo_full_name="me/app", repo_html_url="https://github.com/me/app")

    assert res["application_id"] == "app123"
    assert res["repository_id"] == "repo-uuid"
    assert res["repository_url"] == "https://github.com/me/app"
    assert res["default_branch"] == "main"
    assert res["project_name"] == "app"
    assert res["codemagic_yaml_created"] is True
    assert created == [True]


def test_integrate_reuses_existing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    monkeypatch.setattr(cm.httpx, "get", lambda *a, **k: _Resp(200))
    monkeypatch.setattr(
        cm, "github_repo", lambda *a, **k: {"id": 1, "default_branch": "main", "ssh_url": "x"}
    )
    monkeypatch.setattr(cm, "ensure_codemagic_yaml", lambda *a, **k: False)  # already present
    existing: dict[str, Any] = {
        "_id": "existing1",
        "appName": "app",
        "branches": ["main"],
        "repository": {"id": "r", "defaultBranch": "main"},
    }
    monkeypatch.setattr(cm, "find_app", lambda *a, **k: existing)

    def _no_create(*a: object, **k: object) -> dict[str, Any]:
        raise AssertionError("create_app must not be called when the app already exists")

    monkeypatch.setattr(cm, "create_app", _no_create)
    monkeypatch.setattr(cm, "get_app", lambda *a, **k: existing)
    monkeypatch.setattr(cm, "wait_for_sync", lambda *a, **k: ["main"])

    res = cm.integrate(s, repo_full_name="me/app", repo_html_url="https://github.com/me/app")
    assert res["application_id"] == "existing1"
    assert res["codemagic_yaml_created"] is False


def test_github_token_shared_with_publish(tmp_path: Path) -> None:
    # the codemagic.yaml commit reuses the GitHub PAT from github_publish
    s = _settings(tmp_path)
    assert github_publish.load_token(s) == "gh_tok"


def test_bundle_id_derivation(tmp_path: Path) -> None:
    s = Settings(codemagic_bundle_prefix="com.acme")
    assert cm._bundle_id(s, "owner/Silly-Fun-Smile") == "com.acme.sillyfunsmile"


def test_resolved_workflow_id_builtin_template() -> None:
    # the built-in template's workflow must match what is committed, so a re-triggered
    # build (after a rework/augment force-push) never references a non-existent workflow
    wf = cm.resolved_workflow_id(Settings(codemagic_template_repo=""), "gh", "o/r")
    assert wf == "ios-unsigned"


def test_builtin_template_targets_ios_13() -> None:
    # RevenueCat/purchases_flutter need iOS 13+ (Swift concurrency) — the template must
    # bump the deployment target before pod install or the iOS build fails to compile
    assert "IPHONEOS_DEPLOYMENT_TARGET = 13.0" in cm._CODEMAGIC_YAML
    assert "platform :ios, '13.0'" in cm._CODEMAGIC_YAML


def test_rendered_codemagic_yaml_is_valid_yaml() -> None:
    # the identity/signing steps embed shell — guard against a broken block scalar
    import yaml

    rendered = cm._render_yaml(None, "com.acme.demo", "Demo App")
    doc = yaml.safe_load(rendered)
    assert "ios-unsigned" in doc["workflows"]


def test_builtin_template_injects_ios_permissions() -> None:
    # real iOS permissions crash without NS*UsageDescription in Info.plist; the build must
    # apply the generator's ios_permissions.json after flutter create regenerated ios/.
    import ast

    import yaml

    doc = yaml.safe_load(cm._render_yaml(None, "com.acme.demo", "Demo App"))
    steps = doc["workflows"]["ios-unsigned"]["scripts"]
    names = [s["name"] for s in steps]
    assert "Inject iOS permission usage descriptions" in names
    # runs after flutter create (Scaffold) and before pub get so Info.plist exists
    assert names.index("Inject iOS permission usage descriptions") > names.index(
        "Scaffold platform folders"
    )
    assert names.index("Inject iOS permission usage descriptions") < names.index(
        "Get Flutter packages"
    )
    inject = next(s for s in steps if s["name"].startswith("Inject"))["script"]
    assert "ios_permissions.json" in inject
    body = inject.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    ast.parse(body)  # embedded PlistBuddy driver must be valid python


def test_builtin_template_has_identity_step_with_placeholders() -> None:
    y = cm._CODEMAGIC_YAML
    assert "Set display name and Android identity" in y
    assert "__BUNDLE_ID__" in y and "__APP_NAME__" in y
    # display name (iOS) + applicationId/label (Android); iOS bundle id stays default
    assert "CFBundleDisplayName" in y and "applicationId" in y and "android:label" in y
    assert "PRODUCT_BUNDLE_IDENTIFIER =" not in y  # not changed for the unsigned build


def test_render_yaml_fills_bundle_id_and_app_name() -> None:
    out = cm._render_yaml(None, "com.acme.demo", "Demo App")
    assert "__BUNDLE_ID__" not in out and "__APP_NAME__" not in out
    assert "com.acme.demo" in out
    assert "Demo App" in out


class _R:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._p = payload or {}

    def json(self) -> Any:
        return self._p


def test_ensure_codemagic_yaml_upserts_when_content_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    puts: list[dict[str, Any]] = []
    # repo already has a codemagic.yaml with a DIFFERENT (old) bundle id
    old = base64.b64encode(cm._render_yaml(None, "com.old.id", "Old").encode()).decode()
    monkeypatch.setattr(cm.httpx, "get", lambda *a, **k: _R(200, {"content": old, "sha": "sha1"}))

    def _put(url: str, **k: Any) -> _R:
        puts.append(k["json"])
        return _R(200)

    monkeypatch.setattr(cm.httpx, "put", _put)
    wrote = cm.ensure_codemagic_yaml(
        Settings(codemagic_template_repo=""),
        "gh",
        "o/r",
        "main",
        bundle_id="com.new.id",
        app_name="New",
    )
    assert wrote is True
    assert puts and puts[0]["sha"] == "sha1"  # updates in place with the existing sha
    assert "com.new.id" in base64.b64decode(puts[0]["content"]).decode()


def test_ensure_codemagic_yaml_skips_when_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    same = base64.b64encode(cm._render_yaml(None, "com.same.id", "Same").encode()).decode()
    monkeypatch.setattr(cm.httpx, "get", lambda *a, **k: _R(200, {"content": same, "sha": "s"}))
    monkeypatch.setattr(
        cm.httpx, "put", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not PUT"))
    )
    wrote = cm.ensure_codemagic_yaml(
        Settings(codemagic_template_repo=""),
        "gh",
        "o/r",
        "main",
        bundle_id="com.same.id",
        app_name="Same",
    )
    assert wrote is False


def test_render_yaml_substitutes_bundle_and_falls_back() -> None:
    ref = "  bundle_identifier: com.batteam.trimvo\n  BUNDLE_ID: com.batteam.trimvo\n"
    out = cm._render_yaml(ref, "com.acme.app")
    assert "com.batteam.trimvo" not in out
    assert out.count("com.acme.app") == 2
    # no reference -> built-in template
    assert "ios-unsigned" in cm._render_yaml(None, "com.acme.app")
