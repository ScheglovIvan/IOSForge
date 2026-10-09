"""Phase 4 review fixes: ad-free media slots, captured manifest, states, audits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import compliance, ios_pipeline, swiftui_gen, swiftui_media, xcode
from iosforge.mvp.frida_ingest import ingest_archive
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_scaffold import build_plan
from iosforge.mvp.swiftui_templates import MEDIA
from tests.test_feasibility import _spec_three_screens
from tests.test_frida_ingest import _build_archive


def test_ingest_keeps_the_capture_manifest_for_locale_resolution(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path / "runs")
    ingest_archive(_build_archive(tmp_path / "a.zip"), paths)
    assert json.loads(paths.capture_manifest_json.read_text())["schema_version"]
    paths.app_spec_json.write_text(json.dumps({"screens": []}))
    assert ios_pipeline._resolve_locale(paths, "us") == "en-US"
    paths.app_spec_json.write_text(json.dumps({"screens": [], "source_locale": "ru-RU"}))
    assert ios_pipeline._resolve_locale(paths, "us") == "ru-RU"


def test_media_slots_come_from_the_ad_free_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = {
        "app_name": "Demo",
        "screens": [
            {
                "id": "0011",
                "name": "Home",
                "route": "/",
                "components": [
                    {"type": "image", "role": "ad_banner", "data": "sponsored creative"},
                    {"type": "image", "role": "header_photo", "data": "hands cleaning a phone"},
                ],
            }
        ],
        "navigation": {"type": "stack", "map": []},
    }
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", lambda *a, **k: 0)
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    swiftui_gen.generate(paths, app_name="Demo", bundle_id="com.ex.d", settings=Settings())
    recorded = json.loads((paths.claude_ws / swiftui_media.GENERATED_MEDIA_JSON).read_text())
    assert [r["slot"] for r in recorded] == ["header_photo"]


@pytest.mark.parametrize(
    "role", ["feature_icons_banner", "brand_logos_header", "user_avatars_header", "profile_photo"]
)
def test_plural_and_profile_images_are_not_decorative(role: str) -> None:
    spec = {"screens": [{"id": "a", "components": [{"type": "image", "role": role}]}]}
    assert swiftui_media.image_slots(spec) == []


def test_failed_prediction_falls_back_to_a_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swiftui_media.image_slides, "is_configured", lambda s: True)
    monkeypatch.setattr(swiftui_media.image_slides, "load_token", lambda s: "tok")

    def boom(*a: Any, **k: Any) -> bytes:
        raise RuntimeError("prediction failed")

    monkeypatch.setattr(swiftui_media.image_slides, "generate_image", boom)
    spec = {
        "screens": [{"id": "a", "components": [{"type": "image", "role": "hero", "data": "x"}]}]
    }
    images = swiftui_media.generate_images(
        tmp_path / "app", tmp_path, spec, settings=Settings(codegen_generate_images=True)
    )
    assert [i.source for i in images] == ["placeholder"]
    assert (tmp_path / "app/Resources/Media/generated/a_hero.png").exists()


def test_states_of_stack_screens_are_plain_pushes() -> None:
    spec = {
        "screens": [
            {"id": "0011", "name": "Home", "route": "/", "navigates_to": ["0008"]},
            {"id": "0008", "name": "Instructions"},
            {"id": "s1", "name": "Welcome back", "state_of": "0011"},
            {"id": "s2", "name": "Upgrade sheet", "state_of": "0008"},
        ],
        "navigation": {"type": "stack", "map": []},
    }
    entries = {e.screen_id: e for e in build_plan(spec).entries}
    assert (entries["s1"].presentation, entries["s1"].onboarding) == ("push", False)
    assert (entries["s2"].presentation, entries["s2"].tab_root) == ("push", "0011")


def test_media_asset_resolves_nested_files() -> None:
    assert "subdirectory: folder" in MEDIA and '(["Media"] + parts.dropLast())' in MEDIA


def test_scope_to_prunes_the_judge_originals(tmp_path: Path) -> None:
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(_spec_three_screens()))
    paths.screens_json.write_text(
        json.dumps({"screens": [{"id": "0000"}, {"id": "0001"}, {"id": "0002"}]})
    )
    swiftui_gen.scope_to(paths, ["0000", "0001"])
    assert [s["id"] for s in json.loads(paths.screens_json.read_text())["screens"]] == [
        "0000",
        "0001",
    ]
    assert len(json.loads((paths.run_dir / "screens_full.json").read_text())["screens"]) == 3


def test_onboarding_chain_closes_the_edge_to_home(tmp_path: Path) -> None:
    spec = {
        "screens": [
            {"id": "0000", "name": "Splash / Launch", "navigates_to": ["0011"]},
            {"id": "0005", "name": "Loading Transition"},
            {"id": "0011", "name": "Home", "route": "/"},
        ],
        "navigation": {"type": "stack", "map": []},
    }
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    swiftui_gen.write_scaffold(paths.xcode_app, spec, app_name="Demo", bundle_id="com.ex.d")
    for sid, body in (("0000", "router.show(.s0005)"), ("0005", "router.finishOnboarding()")):
        (paths.xcode_app / f"App/Features/{sid}/Screen{sid}View.swift").write_text(
            f"struct Screen{sid}View: View {{ func go() {{ {body} }} }}\n"
        )
    paths.generated_screens_json.write_text(
        json.dumps({"screens": [{"id": i} for i in ("0000", "0005", "0011")]})
    )
    assert compliance.nav_audit_ios(paths)["missing_edges"] == []
