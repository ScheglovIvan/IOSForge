"""Divergent replacement images for decorative photos (Replicate mocked / placeholders)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from iosforge.common.config import Settings
from iosforge.mvp import swiftui_media
from iosforge.mvp.swiftui_prompts import screen_prompt
from iosforge.mvp.swiftui_scaffold import build_plan

SPEC: dict[str, Any] = {
    "screens": [
        {
            "id": "0006",
            "route": "/",
            "components": [
                {
                    "type": "image",
                    "role": "header_photo",
                    "data": "hand wiping a phone with a cloth",
                },
                {"type": "image", "role": "app_icon_hero", "data": "Speaker Cleaner icon"},
                {"type": "button", "role": "cta", "data": "Start"},
            ],
        },
        {
            "id": "0001",
            "name": "Paywall",
            "components": [{"type": "media", "role": "hero", "data": "phone in water"}],
        },
    ],
    "navigation": {"type": "stack", "map": []},
    "design_tokens": {"color": {"primary": {"$value": "#FF0B00"}, "accent": {"$value": "#DB1D62"}}},
}


def test_image_slots_pick_decorative_images_only() -> None:
    slots = swiftui_media.image_slots(SPEC)
    assert [(s.screen_id, s.slot, s.description) for s in slots] == [
        ("0006", "header_photo", "hand wiping a phone with a cloth"),
        ("0001", "hero", "phone in water"),
    ]


def test_prompt_is_text_only_and_divergent() -> None:
    slot = swiftui_media.image_slots(SPEC)[0]
    prompt = swiftui_media.generation_prompt(slot, ["#FF0B00"])
    assert "hand wiping a phone" in prompt and "#FF0B00" in prompt and "no logos" in prompt


def test_without_spend_flag_placeholders_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []
    monkeypatch.setattr(
        swiftui_media.image_slides, "generate_image", lambda *a, **k: called.append(a)
    )
    images = swiftui_media.generate_images(
        tmp_path / "app", tmp_path, SPEC, settings=Settings(codegen_generate_images=False)
    )
    assert called == [] and [i.source for i in images] == ["placeholder", "placeholder"]
    png = tmp_path / "app/Resources/Media/generated/0006_header_photo.png"
    assert Image.open(png).size == swiftui_media.PLACEHOLDER_SIZE
    recorded = json.loads((tmp_path / swiftui_media.GENERATED_MEDIA_JSON).read_text())
    assert recorded[0]["file"] == "generated/0006_header_photo.png"


def test_with_spend_flag_and_token_replicate_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(swiftui_media.image_slides, "is_configured", lambda s: True)
    monkeypatch.setattr(swiftui_media.image_slides, "load_token", lambda s: "tok")
    monkeypatch.setattr(swiftui_media.image_slides, "to_png", lambda data: data)

    def fake(prompt: str, urls: list[str], **kw: Any) -> bytes:
        calls.append({"prompt": prompt, "urls": urls, **kw})
        return b"\\x89PNG fake"

    monkeypatch.setattr(swiftui_media.image_slides, "generate_image", fake)
    images = swiftui_media.generate_images(
        tmp_path / "app", tmp_path, SPEC, settings=Settings(codegen_generate_images=True)
    )
    assert [i.source for i in images] == ["replicate", "replicate"]
    assert calls[0]["urls"] == [] and calls[0]["token"] == "tok"


def test_screen_prompt_lists_replacement_images() -> None:
    plan = build_plan(SPEC)
    entry = next(e for e in plan.entries if e.screen_id == "0006")
    text = screen_prompt(
        entry, plan, targets=[], prompters=[],
        images=[("generated/0006_header_photo.png", "hand wiping a phone with a cloth")],
    )  # fmt: skip
    assert '`MediaAsset.image("generated/0006_header_photo.png")` — hand wiping' in text
    assert "Replacement images" not in screen_prompt(entry, plan, targets=[], prompters=[])


def test_only_replicate_images_are_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(swiftui_media.image_slides, "is_configured", lambda s: True)
    monkeypatch.setattr(swiftui_media.image_slides, "load_token", lambda s: "tok")
    monkeypatch.setattr(swiftui_media.image_slides, "to_png", lambda data: data)
    monkeypatch.setattr(
        swiftui_media.image_slides, "generate_image", lambda p, u, **k: calls.append(p) or b"png"
    )
    target = tmp_path / "app/Resources/Media/generated"
    target.mkdir(parents=True)
    (target / "0006_header_photo.png").write_bytes(b"old placeholder")
    settings = Settings(codegen_generate_images=True)
    first = swiftui_media.generate_images(tmp_path / "app", tmp_path, SPEC, settings=settings)
    assert [i.source for i in first] == ["replicate", "replicate"] and len(calls) == 2
    second = swiftui_media.generate_images(tmp_path / "app", tmp_path, SPEC, settings=settings)
    assert [i.source for i in second] == ["cached", "cached"] and len(calls) == 2
