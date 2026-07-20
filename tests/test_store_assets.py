"""App Store listing rendering: canvas, palette, backdrops and the authoring contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iosforge.mvp import store_assets as sa

_TOKENS: dict[str, Any] = {
    "color": {
        "primary": {"$value": "#DC0C5B"},
        "primary_gradient_start": {"$value": "#B80E74"},
        "primary_gradient_end": {"$value": "#E54629"},
    },
    "font": {"display": {"$value": "Poppins"}},
}


def test_canvas_is_an_accepted_app_store_size() -> None:
    # App Store Connect rejects anything outside the 6.7"/6.9" set
    assert sa.CANVAS in {(1290, 2796), (1320, 2868), (1260, 2736)}


def test_palette_reads_the_apps_own_tokens() -> None:
    pal = sa.palette(_TOKENS)
    assert pal["bg_start"] == "#B80E74"
    assert pal["bg_end"] == "#E54629"
    assert pal["font"] == "Poppins"


def test_palette_falls_back_when_tokens_missing() -> None:
    pal = sa.palette({})
    assert pal["bg_start"] and pal["bg_end"] and pal["font"]


def test_build_prompt_pins_the_exact_canvas() -> None:
    prompt = sa.BUILD_PROMPT.format(w=sa.CANVAS[0], h=sa.CANVAS[1])
    assert f"{sa.CANVAS[0]}x{sa.CANVAS[1]}" in prompt
    assert f"exactly {sa.CANVAS[0]}px by {sa.CANVAS[1]}px" in prompt


def test_build_prompt_works_from_contracts_not_images() -> None:
    # the whole point of the split: authoring must not re-open the source screenshots
    p = sa.BUILD_PROMPT
    assert "contracts/NN.json" in p
    assert "Do NOT open `original/`" in p


def test_build_prompt_requires_self_contained_pages() -> None:
    # a CDN font or remote image silently renders broken in headless Chromium
    p = sa.BUILD_PROMPT
    assert "NO external fonts, CDNs or network" in p
    assert "inline `<style>` only" in p


def test_build_prompt_keeps_real_screens_and_true_claims() -> None:
    p = sa.BUILD_PROMPT
    assert "put OUR real screen image" in p
    assert "Never draw a fake app UI" in p
    assert "no install counts, no ratings, no awards" in p


def test_build_prompt_demands_the_composition_be_reproduced() -> None:
    # fixed templates lost angled devices and overhanging cards; authoring must not
    p = sa.BUILD_PROMPT
    assert "REPRODUCE THE COMPOSITION" in p
    assert "key_elements" in p and "depth_devices" in p
    assert "overhang" in p
    assert "perspective" in p  # the "3D" the source leans on is CSS-expressible


def test_build_prompt_requires_one_page_per_contract() -> None:
    assert "one per contract" in sa.BUILD_PROMPT
    assert "EVERY contract" in sa.BUILD_PROMPT


def test_prefetch_backgrounds_only_for_photographic_slides(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_fetch(query: str, out: Path, *, api_key: str, timeout: float = 25.0) -> Path | None:
        calls.append(query)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return out

    original = sa.fetch_background
    sa.fetch_background = fake_fetch  # type: ignore[assignment]
    try:
        fetched = sa.prefetch_backgrounds(
            tmp_path,
            [
                {"index": 1, "background": {"kind": "photo", "description": "car interior"}},
                {"index": 2, "background": {"kind": "gradient", "description": "dark teal"}},
                {"index": 3, "background": {"kind": "photo", "description": ""}},
            ],
            api_key="k",
        )
    finally:
        sa.fetch_background = original  # type: ignore[assignment]

    assert fetched == 1
    assert calls == ["car interior"]  # gradients are CSS; empty queries are skipped
    assert (tmp_path / "backgrounds" / "01.jpg").is_file()


def test_render_pages_is_a_noop_without_pages(tmp_path: Path) -> None:
    assert sa.render_pages(tmp_path, [], tmp_path / "out") == []
