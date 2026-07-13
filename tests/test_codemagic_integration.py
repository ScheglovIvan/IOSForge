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
        lambda settings, tok, full, branch: created.append(True) or True,
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


def test_render_yaml_substitutes_bundle_and_falls_back() -> None:
    ref = "  bundle_identifier: com.batteam.trimvo\n  BUNDLE_ID: com.batteam.trimvo\n"
    out = cm._render_yaml(ref, "com.acme.app")
    assert "com.batteam.trimvo" not in out
    assert out.count("com.acme.app") == 2
    # no reference -> built-in template
    assert "ios-unsigned" in cm._render_yaml(None, "com.acme.app")
