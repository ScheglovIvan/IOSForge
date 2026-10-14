"""Artifacts tab: original vs generated screens, judge scores and functional checks."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iosforge.admin import artifacts_view
from iosforge.admin.app import create_app
from iosforge.admin.deps import get_storage, require_user
from iosforge.admin.templating import templates

JOB = "5b0c1d2e-0000-4000-8000-000000000001"
OTHER = "5b0c1d2e-0000-4000-8000-000000000002"
_USER = NS(username="admin", csrf_token="csrf-tok")


@pytest.fixture(autouse=True)
def _fresh_name_cache() -> Iterator[None]:
    artifacts_view._NAMES.clear()
    yield
    artifacts_view._NAMES.clear()


def _screen_map() -> dict[str, Any]:
    return {
        "screens": [
            {"id": "0001", "screenshot": "screens/0001.png"},
            {"id": "0002", "screenshot": "0002.png"},
        ]
    }


def _keys() -> list[str]:
    return [f"jobs/{JOB}/screenshots/0001.png", f"jobs/{JOB}/screenshots/0002.png"]


def _report() -> dict[str, Any]:
    return {
        "compliance_score": 0.8125,
        "structure": 0.9,
        "coverage": 0.5,
        "flows": 0.75,
        "divergence": 0.4,
        "status": "soft_pass",
        "stop_reason": "max_iterations",
        "iteration": 3,
        "threshold": 0.95,
        "version": 2,
        "screens": [
            {
                "id": "0001",
                "generated": "generated_screens/0001.png",
                "score": 0.9,
                "divergence": 0.4,
                "diffs": ["title font is lighter", "<script>alert(1)</script>"],
            },
            {
                "id": "0002",
                "generated": None,
                "score": 0.0,
                "divergence": 0.0,
                "diffs": ["screen not rendered in generated app"],
            },
        ],
        "structural": {
            "ok": False,
            "functional": {
                "ok": False,
                "infra_error": False,
                "checks": [
                    {
                        "name": "audio_playback",
                        "screen_id": "0001",
                        "passed": False,
                        "ui_passed": True,
                        "infra_error": False,
                        "events": ["play"],
                        "missing_events": ["stop"],
                        "flow": "opens screen 0001, taps Play",
                        "message": "the module never recorded: stop",
                    },
                    {
                        "name": "paywall_purchase",
                        "screen_id": "0002",
                        "passed": True,
                        "ui_passed": True,
                        "infra_error": False,
                        "events": ["purchase"],
                        "missing_events": [],
                        "flow": "opens screen 0002",
                        "message": "",
                    },
                ],
            },
        },
    }


def _view(report: dict[str, Any] | None = None) -> artifacts_view.ArtifactsView:
    return artifacts_view.build(
        JOB,
        screen_map=_screen_map(),
        screenshot_keys=_keys(),
        report=report,
        names={"0001": "Home", "0002": "Paywall"},
    )


def test_grid_pairs_original_and_generated_per_screen() -> None:
    view = _view(_report())
    assert view.built
    first, second = view.screens
    assert (first.screen_id, first.name) == ("0001", "Home")
    assert first.original_key == f"jobs/{JOB}/screenshots/0001.png"
    assert first.generated_key == f"jobs/{JOB}/generated_screenshots/0001.png"
    assert first.score == pytest.approx(0.9)
    assert "title font is lighter" in first.diffs
    assert second.original_key == f"jobs/{JOB}/screenshots/0002.png"
    assert second.generated_key is None


def test_judge_summary_and_functional_log_come_from_the_report() -> None:
    view = _view(_report())
    assert view.judge is not None
    assert view.judge.compliance_score == pytest.approx(0.8125)
    assert (view.judge.status, view.judge.tone) == ("soft_pass", "warning")
    assert view.judge.version == 2
    assert view.functional is not None
    audio, paywall = view.functional.checks
    assert (audio.status, audio.missing_events) == ("failed", ["stop"])
    assert paywall.status == "passed"


def test_functional_log_of_a_rework_round_is_shown() -> None:
    report = _report()
    functional = report.pop("structural")["functional"]
    rework = {**report, "functional": functional, "round": "rework", "version": 3}
    view = _view(rework)
    assert view.functional is not None
    assert [c.name for c in view.functional.checks] == ["audio_playback", "paywall_purchase"]


def test_before_a_build_the_grid_lists_the_crawl_without_renders() -> None:
    view = _view(None)
    assert not view.built
    assert [s.screen_id for s in view.screens] == ["0001", "0002"]
    assert all(s.generated_key is None for s in view.screens)
    assert view.judge is None and view.functional is None


def test_capture_keys_of_another_job_are_never_offered() -> None:
    view = artifacts_view.build(
        JOB,
        screen_map=_screen_map(),
        screenshot_keys=[f"jobs/{OTHER}/screenshots/0001.png"],
        report=None,
        names={},
    )
    assert all(s.original_key is None for s in view.screens)


def test_crafted_render_path_cannot_leave_the_job_prefix() -> None:
    report = {"screens": [{"id": "0001", "generated": "../../x/..", "score": 1}]}
    view = artifacts_view.build(JOB, screen_map=None, screenshot_keys=[], report=report, names={})
    assert view.screens[0].generated_key is None


def test_screen_names_tolerate_a_missing_spec() -> None:
    class _Missing:
        def get(self, key: str) -> bytes:
            raise KeyError(key)

    assert artifacts_view.screen_names(_Missing(), JOB) == {}  # type: ignore[arg-type]


def test_screen_names_are_cached_between_page_reloads() -> None:
    calls: list[str] = []

    class _Spec:
        def get(self, key: str) -> bytes:
            calls.append(key)
            return json.dumps({"screens": [{"id": "0001", "name": "Home"}]}).encode()

    storage: Any = _Spec()
    assert artifacts_view.screen_names(storage, JOB) == {"0001": "Home"}
    assert artifacts_view.screen_names(storage, JOB) == {"0001": "Home"}
    assert len(calls) == 1
    artifacts_view._NAMES[JOB] = (0.0, {})
    assert artifacts_view.screen_names(storage, JOB) == {"0001": "Home"}
    assert len(calls) == 2


def test_download_header_survives_any_file_name() -> None:
    header = artifacts_view.content_disposition("attachment", 'экран "1".png')
    header.encode("latin-1")
    assert 'filename="_______1_.png"' in header
    assert "filename*=UTF-8''%D1%8D%D0%BA%D1%80%D0%B0%D0%BD%20%221%22.png" in header


def _render(**ctx: object) -> str:
    job = NS(
        id=JOB,
        source_app_ref="Speaker Cleaner",
        state=NS(value="done"),
        created_at=datetime(2026, 10, 1, 12, 0, 0),
        source_app_metadata={},
        result_version=2,
    )
    signing = {"configured": False, "own": False, "key_name": "", "key_id": "", "issuer_id": ""}
    base: dict[str, object] = {
        "request": NS(url=NS(path=f"/jobs/{JOB}")),
        "user": _USER,
        "job": job,
        "gen": None,
        "build": None,
        "can_ios_build": False,
        "store_slides": [],
        "signing": signing,
        "signed_ok": False,
        "sign_error": "",
        "timeline": [],
        "codegen_tasks": [],
    }
    base.update(ctx)
    return templates.env.get_template("job_detail.html").render(**base)


def test_tab_renders_grid_scores_and_functional_checks() -> None:
    html = _render(artifacts=_view(_report()))
    assert 'class="tab-radio" type="radio" name="jobtab" id="t-artifacts"' in html
    assert "panel-artifacts" in html
    original = f"/jobs/{JOB}/artifacts/object?key=jobs/{JOB}/screenshots/0001.png&w=160"
    generated = f"/jobs/{JOB}/artifacts/object?key=jobs/{JOB}/generated_screenshots/0001.png"
    assert original in html and generated in html
    assert 'loading="lazy"' in html
    assert "screen not generated" in html
    assert "81.2%" in html and "soft_pass" in html
    assert "audio_playback" in html and "the module never recorded: stop" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_tab_shows_the_empty_states() -> None:
    html = _render(artifacts=_view(None))
    assert "No build yet" in html
    assert "screen not generated" not in html
    bare = _render(
        artifacts=artifacts_view.build(
            JOB, screen_map=None, screenshot_keys=[], report=None, names={}
        )
    )
    assert "No screens captured yet" in bare


def test_detail_context_reads_the_crawl_and_the_report() -> None:
    walk = NS(screen_map=_screen_map(), screenshot_keys=_keys())
    gen = NS(selftest_report=_report())

    class _Db:
        def scalar(self, stmt: object) -> object:
            return walk

    class _Storage:
        def get(self, key: str) -> bytes:
            assert key == f"jobs/{JOB}/app_spec/app_spec.json"
            return json.dumps({"screens": [{"id": "0001", "name": "Home"}]}).encode()

    view = artifacts_view.load(_Db(), _Storage(), uuid.UUID(JOB), gen)  # type: ignore[arg-type]
    assert [s.name for s in view.screens] == ["Home", ""]
    assert view.screens[0].generated_key is not None


class _ObjectStorage:
    def __init__(self) -> None:
        self.objects = {
            f"jobs/{JOB}/generated_screenshots/0001.png": b"MINE",
            f"jobs/{JOB}/sources/экран.zip": b"ZIP",
            f"jobs/{OTHER}/generated_screenshots/0001.png": b"THEIRS",
        }

    def get(self, key: str) -> bytes:
        return self.objects[key]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[require_user] = lambda: _USER
    app.dependency_overrides[get_storage] = _ObjectStorage
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_object_route_serves_this_jobs_render(client: TestClient) -> None:
    r = client.get(
        f"/jobs/{JOB}/artifacts/object",
        params={"key": f"jobs/{JOB}/generated_screenshots/0001.png"},
    )
    assert r.status_code == 200 and r.content == b"MINE"


@pytest.mark.parametrize(
    "key",
    [
        f"jobs/{OTHER}/generated_screenshots/0001.png",
        f"jobs/{JOB}/../{OTHER}/generated_screenshots/0001.png",
        f"jobs/{JOB}//generated_screenshots/0001.png",
        f"jobs/{JOB}/generated_screenshots/..\\0001.png",
        f"jobs/{JOB}/",
    ],
)
def test_object_route_hides_keys_outside_the_job(client: TestClient, key: str) -> None:
    r = client.get(f"/jobs/{JOB}/artifacts/object", params={"key": key})
    assert r.status_code == 404
    assert b"THEIRS" not in r.content


def test_object_route_downloads_a_non_ascii_name(client: TestClient) -> None:
    r = client.get(f"/jobs/{JOB}/artifacts/object", params={"key": f"jobs/{JOB}/sources/экран.zip"})
    assert r.status_code == 200 and r.content == b"ZIP"
    assert "filename*=UTF-8''%D1%8D%D0%BA%D1%80%D0%B0%D0%BD.zip" in r.headers["content-disposition"]
