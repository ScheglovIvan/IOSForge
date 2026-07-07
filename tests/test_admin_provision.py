"""Firebase provisioning orchestration — tested with a mocked client (no creds)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import admin_provision, content_seed
from iosforge.mvp.admin_provision import ProvisionError, make_project_id


def _admin_dir(tmp_path: Path, *, collections: dict | None = None) -> Path:
    d = tmp_path / "admin"
    (d / "firestore").mkdir(parents=True)
    cols = collections or {"User": {"uid": "string"}, "Series": {"id": "string"}}
    (d / "firestore" / "collections.schema.json").write_text(json.dumps({"collections": cols}))
    (d / "firestore" / "firestore.rules").write_text("rules_version = '2';")
    return d


class _FakeClient:
    def __init__(self, *, billing: bool = False) -> None:
        self.calls: list[object] = []
        self._billing = billing

    def ensure_project(self, app_name: str = "app") -> str:
        self.calls.append(("ensure_project", app_name))
        return "demo-proj"

    def ensure_billing(self, project_id: str) -> bool:
        self.calls.append(("ensure_billing", project_id))
        return self._billing

    def ensure_firestore(self, project_id: str) -> None:
        self.calls.append(("ensure_firestore", project_id))

    def deploy_rules(self, project_id: str, rules_text: str) -> bool:
        self.calls.append(("deploy_rules", bool(rules_text)))
        return True

    def seed(self, project_id: str, collections: dict, per: int = 3) -> dict:
        self.calls.append(("seed", sorted(collections)))
        return {name: per for name in collections}

    def ensure_web_app(self, project_id: str, display_name: str) -> dict[str, str]:
        self.calls.append(("ensure_web_app", project_id))
        return {"apiKey": "AIza-demo", "appId": "1:demo:web", "projectId": project_id}


def test_provision_orchestration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _admin_dir(tmp_path)
    fake = _FakeClient()
    monkeypatch.setattr(admin_provision, "_build_client", lambda settings: fake)

    result = admin_provision.provision(d, Settings())

    assert result["project_id"] == "demo-proj"
    assert result["console_url"].endswith("/project/demo-proj")
    assert result["rules_deployed"] is True
    assert result["seeded"] == {"User": 3, "Series": 3}
    assert result["billing_enabled"] is False
    assert result["web_app_id"] == "1:demo:web"
    assert result["firebase_config"]["appId"] == "1:demo:web"
    assert ("ensure_project", "App") in fake.calls
    assert ("ensure_billing", "demo-proj") in fake.calls
    assert ("ensure_web_app", "demo-proj") in fake.calls
    assert ("seed", ["Series", "User"]) in fake.calls
    assert (d / "provision_result.json").exists()


def test_content_seed_skipped_without_billing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _admin_dir(tmp_path, collections={"Series": {"id": "string"}, "Episode": {"id": "string"}})
    monkeypatch.setattr(
        admin_provision, "_build_client", lambda settings: _FakeClient(billing=False)
    )

    def _boom(*a: object, **k: object) -> dict:
        raise AssertionError("content_seed must not run without billing")

    monkeypatch.setattr(content_seed, "seed_content", _boom)

    result = admin_provision.provision(d, Settings())
    assert result["content"] is None


def test_content_seed_runs_with_billing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _admin_dir(tmp_path, collections={"Series": {"id": "string"}, "Episode": {"id": "string"}})
    monkeypatch.setattr(
        admin_provision, "_build_client", lambda settings: _FakeClient(billing=True)
    )
    monkeypatch.setattr(
        content_seed, "seed_content", lambda *a, **k: {"bucket": "b", "counts": {"series": 1}}
    )

    result = admin_provision.provision(d, Settings())
    assert result["content"] == {"bucket": "b", "counts": {"series": 1}}


def test_provision_missing_schema_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_provision, "_build_client", lambda settings: _FakeClient())
    with pytest.raises(ProvisionError):
        admin_provision.provision(tmp_path / "empty", Settings())


def test_from_settings_requires_credentials() -> None:
    with pytest.raises(ProvisionError):
        admin_provision._FirebaseClient.from_settings(Settings(firebase_sa_path=""))


def test_make_project_id_conforms_to_gcp_rules() -> None:
    pid = make_project_id("My Cool App!", "iosforge")
    assert re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", pid)
    assert 6 <= len(pid) <= 30
    assert pid.startswith("iosforge-my-cool-app-")
    # Two calls differ (random suffix) — global uniqueness.
    assert make_project_id("My Cool App!", "iosforge") != pid


def test_make_project_id_handles_empty_name() -> None:
    pid = make_project_id("", "iosforge")
    assert re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", pid)


def test_make_project_id_long_name_keeps_random_suffix() -> None:
    long_name = "super long streaming application name here now"
    a = make_project_id(long_name, "iosforge")
    b = make_project_id(long_name, "iosforge")
    assert re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", a)
    assert len(a) <= 30
    # The 6-char random suffix must survive truncation → no collisions.
    assert re.fullmatch(r"[0-9a-f]{6}", a.rsplit("-", 1)[-1])
    assert a != b
