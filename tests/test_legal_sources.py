"""Legal-page facts read from the stored SwiftUI app (SDKs and usage descriptions)."""

from __future__ import annotations

from pathlib import Path

import pytest

from iosforge.mvp import swiftui_integrations
from iosforge.worker import run_job


def test_sdks_follow_the_frozen_integrations(tmp_path: Path) -> None:
    assert run_job._app_sdks(tmp_path) == []
    swiftui_integrations.write(
        tmp_path, swiftui_integrations.Integrations(apphud_key="app_x", tenjin_key="T")
    )
    assert run_job._app_sdks(tmp_path) == ["apphud", "tenjin"]
    swiftui_integrations.write(tmp_path, swiftui_integrations.Integrations(tenjin_key="T"))
    assert run_job._app_sdks(tmp_path) == ["tenjin"]


def test_usage_descriptions_come_from_the_project(tmp_path: Path) -> None:
    assert run_job._usage_descriptions(tmp_path) == {}
    (tmp_path / "project.yml").write_text(
        "targets:\n  App:\n    info:\n      properties:\n"
        '        NSCameraUsageDescription: "Scan a document."\n'
        "        NSMicrophoneUsageDescription: Measure the noise level.\n"
        "        UILaunchScreen: {}\n"
    )
    assert run_job._usage_descriptions(tmp_path) == {
        "NSCameraUsageDescription": "Scan a document.",
        "NSMicrophoneUsageDescription": "Measure the noise level.",
    }


def test_vehicle_lookup_needs_the_word_vin() -> None:
    assert run_job._remote_endpoints({"screens": [{"name": "Driving tips"}]}) == []
    assert run_job._remote_endpoints({"screens": [{"name": "VIN decoder"}]})


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


def test_legal_facts_describe_the_app_as_it_ships(tmp_path: Path) -> None:
    import json
    import uuid

    from iosforge.common.config import Settings
    from iosforge.db.models import Job
    from iosforge.mvp import swiftui_gen
    from iosforge.mvp.paths import RunPaths
    from iosforge.worker import swiftui_tasks
    from tests.test_feasibility import _spec_three_screens

    spec = _spec_three_screens()
    meta = {"apphud_api_key": "app_abcdefgh", "tenjin_api_key": "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}
    job = Job(id=uuid.uuid4(), source_app_metadata=meta)
    storage = _Storage()
    app = tmp_path / "src"
    swiftui_gen.write_scaffold(app, spec, app_name="Demo", bundle_id="com.ex.d")
    assert not (app / swiftui_integrations.INTEGRATIONS_JSON).exists()
    swiftui_tasks.store_sources(storage, str(job.id), app)  # type: ignore[arg-type]
    storage.put(f"jobs/{job.id}/app_spec/app_spec.json", json.dumps(spec).encode())

    facts = run_job.legal_facts(
        job,
        storage,  # type: ignore[arg-type]
        RunPaths.at(tmp_path / "run"),
        Settings(),
        contact_email="help@example.com",
    )

    assert {p.title for p in facts.processors} == {"Apphud", "Tenjin"}
    assert "Tracking permission" in {p.title for p in facts.practices}
    assert facts.contact_email == "help@example.com" and facts.bundle_id


def test_publish_creates_the_pages_repo_once_and_skips_legacy_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    from iosforge.db.models import Job
    from iosforge.mvp import github_publish, legal_pages
    from iosforge.worker import swiftui_tasks

    job = Job(id=uuid.uuid4(), source_app_metadata={})
    storage = _Storage()

    class _Db:
        def __enter__(self) -> _Db:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get(self, model: type, pk: object) -> Job:
            return job

        def commit(self) -> None:
            return None

    created: list[str] = []
    monkeypatch.setattr(run_job, "get_sessionmaker", lambda: _Db)
    monkeypatch.setattr(run_job, "S3ArtifactStorage", lambda: storage)
    monkeypatch.setattr(github_publish, "load_token", lambda s: "tok")
    monkeypatch.setattr(
        github_publish,
        "create_repo",
        lambda s, t, name, d: created.append(name) or {"html_url": f"https://github.com/o/{name}"},
    )
    facts = legal_pages.AppLegalFacts(app_name="Demo", bundle_id="b", contact_email="c@x.io")
    monkeypatch.setattr(run_job, "legal_facts", lambda *a, **k: facts)
    monkeypatch.setattr(legal_pages, "publish_pages", lambda **k: "https://o.github.io/demo-legal")

    assert "no SwiftUI sources" in run_job.publish_legal_pages.run(str(job.id))
    assert created == []

    storage.put(swiftui_tasks.sources_key(str(job.id)), b"zip")
    assert run_job.publish_legal_pages.run(str(job.id)).endswith("/privacy")
    assert run_job.publish_legal_pages.run(str(job.id)).endswith("/privacy")
    assert created == ["demo-legal"]
    assert job.source_app_metadata["legal_repo_url"] == "https://github.com/o/demo-legal"
