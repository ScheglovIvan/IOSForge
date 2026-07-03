"""Firebase provisioning orchestration — tested with a mocked client (no creds)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import admin_provision
from iosforge.mvp.admin_provision import ProvisionError


def _admin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "admin"
    (d / "firestore").mkdir(parents=True)
    (d / "firestore" / "collections.schema.json").write_text(
        json.dumps({"collections": {"User": {"uid": "string"}, "Series": {"id": "string"}}})
    )
    (d / "firestore" / "firestore.rules").write_text("rules_version = '2';")
    return d


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def ensure_project(self) -> str:
        self.calls.append("ensure_project")
        return "demo-proj"

    def ensure_firestore(self, project_id: str) -> None:
        self.calls.append(("ensure_firestore", project_id))

    def deploy_rules(self, project_id: str, rules_text: str) -> bool:
        self.calls.append(("deploy_rules", bool(rules_text)))
        return True

    def seed(self, project_id: str, collections: dict, per: int = 3) -> dict:
        self.calls.append(("seed", sorted(collections)))
        return {name: per for name in collections}


def test_provision_orchestration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _admin_dir(tmp_path)
    fake = _FakeClient()
    monkeypatch.setattr(admin_provision, "_build_client", lambda settings: fake)

    result = admin_provision.provision(d, Settings())

    assert result["project_id"] == "demo-proj"
    assert result["console_url"].endswith("/project/demo-proj")
    assert result["rules_deployed"] is True
    assert result["seeded"] == {"User": 3, "Series": 3}
    assert "ensure_project" in fake.calls
    assert ("seed", ["Series", "User"]) in fake.calls
    assert (d / "provision_result.json").exists()


def test_provision_missing_schema_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_provision, "_build_client", lambda settings: _FakeClient())
    with pytest.raises(ProvisionError):
        admin_provision.provision(tmp_path / "empty", Settings())


def test_from_settings_requires_credentials() -> None:
    with pytest.raises(ProvisionError):
        admin_provision._FirebaseClient.from_settings(Settings())
